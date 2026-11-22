# SPDX-License-Identifier: MIT-0
"""The server: the shared engine both transports use, and the CLI entry point.

The engine turns a parsed inbound frame into outbound frames, independent of
transport. ``handle_control`` answers INIT_SESSION and CHANNEL_STATE;
``run_turn`` drives a turn as an async generator (WebSocket streams each frame);
``run_turn_http`` collects a turn into an A2A Task; ``handle_get_task`` returns a
stored task.

The CLI builds one aiohttp app serving the WebSocket upgrade and HTTP JSON-RPC
POST on ``/a2a`` and ``GET /health``. With any ``--fault`` it refuses to start
unless bound to a loopback host, logs each active fault, and exits non-zero on
an unknown fault name.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import AsyncIterator, Optional

from . import protocol as P
from .auth import (Authenticator, PassThroughAuthenticator, key_from_env,
                   key_from_file, key_from_secret_arn)
from .backends.base import Turn
from .faults import FAULTS, Faults, UnknownFault
from .logredact import redact
from .session import SessionStore
from .tracing import new_trace_id, parse_traceparent, parse_tracestate_parent
from .turn import TurnRunner

log = logging.getLogger("a2a_connect.server")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _merge_parts(parts: list, more: list) -> None:
    """Append streamed parts to a collected artifact, joining adjacent text."""
    for p in more:
        if (parts and "text" in p and "text" in parts[-1]
                and p.get("mediaType") == parts[-1].get("mediaType")):
            parts[-1]["text"] = parts[-1]["text"] + p["text"]
        else:
            parts.append(dict(p))


class Engine:
    """Transport-independent protocol dispatch."""

    def __init__(self, backend, auth: Authenticator, faults: Faults,
                 *, agent_name: str = "starter-kit",
                 service_name: str = "a2a-starter-kit",
                 max_frame_bytes: int = 1024 * 1024,
                 stream_delay_ms: int = 0) -> None:
        self.backend = backend
        self.auth = auth
        self.faults = faults
        self.agent_name = agent_name
        self.service_name = service_name
        self.max_frame_bytes = max_frame_bytes
        self.stream_delay_ms = stream_delay_ms
        self.sessions = SessionStore()
        self._tasks: dict[str, dict] = {}   # taskId -> stored Task (HTTP)
        self.runner = TurnRunner(backend, faults, agent_name, service_name,
                                 stream_delay_ms=stream_delay_ms)

    # ---- Control events --------------------------------------------------

    def handle_control(self, inbound: P.Inbound, *, transport: str) -> list[dict]:
        # Requirement 5.8: log every control message and its handling, with the
        # API key redacted. Control frames carry no key, but the whole frame is
        # passed through the redactor so any key material (a bearer token, an
        # apiKey field) that ever appears is masked rather than logged.
        self._log_control(inbound, transport)
        if inbound.kind == "init_session":
            frames = self._handle_init(inbound, transport)
            self._log_control_handling(inbound, f"INIT_SESSION -> {len(frames)} frame(s)")
            return frames
        if inbound.kind == "channel_state":
            frames = self._handle_channel_state(inbound)
            self._log_control_handling(inbound, "CHANNEL_STATE acknowledged")
            return frames
        if inbound.kind == "error":
            self._log_control_handling(
                inbound, "received informational ERROR from Connect; continuing")
            return []
        self._log_control_handling(inbound, f"ignored control kind {inbound.kind!r}")
        return []

    def _log_control(self, inbound: P.Inbound, transport: str) -> None:
        """Log one received control message (Requirement 5.8).

        Only the frame's type and IDs and the names of its data fields are
        logged, never their values: INIT_SESSION carries contact attributes and
        customer details, which do not belong in an endpoint's logs. The line is
        still passed through redaction in case an ID ever carries key material.
        """
        fields = ",".join(sorted(str(k) for k in (inbound.data or {}))) or "-"
        line = (f"control recv [{transport}] {inbound.kind} "
                f"event={inbound.event_type} ctx={inbound.context_id} "
                f"id={inbound.rpc_id} fields={fields}")
        log.info("%s", redact(line, self._log_secret()))

    def _log_control_handling(self, inbound: P.Inbound, note: str) -> None:
        """Log how a control message was handled (Requirement 5.8)."""
        log.info("control handled [%s] %s: %s",
                 inbound.kind, inbound.context_id, redact(note, self._log_secret()))

    def _log_secret(self) -> Optional[str]:
        """The configured key for log masking, only if it is already cached so
        logging never forces a remote read. Returns None otherwise: the
        structural redaction already masks bearer tokens and apiKey fields, so
        a bare secret value never appears in a control frame regardless."""
        getter = getattr(self.auth, "cached_secret", None)
        if callable(getter):
            try:
                return getter() or None
            except Exception:  # pragma: no cover - defensive
                return None
        return None

    def _handle_init(self, inbound: P.Inbound, transport: str) -> list[dict]:
        context_id = inbound.context_id or P.new_id()
        init = (inbound.data or {}).get("initSession") or {}
        session, created = self.sessions.get_or_create(context_id)

        if self.faults.has("init-no-response"):
            log.info("fault init-no-response: dropping INIT_SESSION")
            return []

        # Audio configs proposed: accept (no counter-proposal), warn text-only.
        if init.get("audioInputConfiguration") or init.get("audioOutputConfiguration"):
            log.warning("audio config proposed but this backend is text only; "
                        "accepting without a counter-proposal")

        if not created and self.faults.has("reject-repeat-init"):
            log.info("fault reject-repeat-init: answering repeat with 409")
            return [P.init_session_rejected(inbound.rpc_id, context_id)]

        if created:
            session.apply_init(init)
        else:
            # Repeated INIT_SESSION for a known contextId: answer like the
            # first (refresh history), do not reset ready state.
            session.apply_init(init)

        rpc_id = inbound.rpc_id
        if self.faults.has("init-wrong-id"):
            rpc_id = P.new_id()
        include_event_type = not self.faults.has("init-bad-shape")
        frame = P.init_session_response(rpc_id, context_id,
                                        include_event_type=include_event_type)

        frames = [frame]
        # greet-before-ready: send a greeting turn immediately after the init
        # response (a protocol violation before READY).
        if self.faults.has("greet-before-ready"):
            task_id = P.new_id()
            art = P.new_id()
            frames.append(P.text_chunk_artifact(
                P.new_id(), task_id, context_id, art,
                "Hello", append=False, last_chunk=True))
            frames.append(P.terminal_status(
                P.new_id(), task_id, context_id, P.TASK_STATE_COMPLETED))
        return frames

    def _handle_channel_state(self, inbound: P.Inbound) -> list[dict]:
        context_id = inbound.context_id
        cs = (inbound.data or {}).get("channelState") or {}
        session = self.sessions.get(context_id) if context_id else None
        if session is not None and cs.get("state") == "READY":
            session.mark_ready()
            log.info("session %s now READY (talk mode)", context_id)
        # No response body required; transports supply an acknowledgement.
        return []

    # ---- Turns -----------------------------------------------------------

    def _build_turn(self, inbound: P.Inbound) -> tuple[Turn, object, str, Optional[str]]:
        context_id = inbound.context_id or P.new_id()
        session, _ = self.sessions.get_or_create(context_id)
        task_id = P.new_id()

        is_dtmf = inbound.kind == "dtmf"
        if is_dtmf:
            dtmf = (inbound.data or {}).get("dtmfInputComplete") or {}
            input_text = dtmf.get("inputString", "")
        else:
            input_text = inbound.text or ""

        is_empty = input_text == P.EMPTY_USER_INPUT

        turn = Turn(
            context_id=context_id,
            task_id=task_id,
            mode=session.mode,
            channel=session.channel,
            input_text=input_text,
            history=session.history,
            contact_context=session.contact_context,
            today=datetime.now(timezone.utc).date(),
            is_dtmf=is_dtmf,
            is_empty_input=is_empty,
        )
        return turn, session, context_id, task_id

    def _trace_context(self, headers: dict, message: dict) -> tuple[str, Optional[str]]:
        tp = headers.get("traceparent")
        trace_id, parent = parse_traceparent(tp)
        if trace_id is None:
            # Fallback to <EXT>/traceparent in message.metadata.
            md = (message or {}).get("metadata") or {}
            trace_id, parent = parse_traceparent(md.get("<EXT>/traceparent"))
            ts_parent = parse_tracestate_parent(md.get("<EXT>/tracestate"))
            if ts_parent:
                parent = ts_parent
        else:
            ts_parent = parse_tracestate_parent(headers.get("tracestate"))
            if ts_parent:
                parent = ts_parent
        if trace_id is None:
            trace_id = new_trace_id()
        return trace_id, parent

    def _delegate_wait_guard(self, session) -> bool:
        """wait-ready-in-delegate fault: refuse to answer until READY seen."""
        return self.faults.has("wait-ready-in-delegate") and not session.ready_seen

    async def run_turn(self, inbound: P.Inbound, *, transport: str,
                       headers: dict,
                       interrupt_event=None) -> AsyncIterator[tuple]:
        turn, session, context_id, task_id = self._build_turn(inbound)
        if self._delegate_wait_guard(session):
            log.info("fault wait-ready-in-delegate: not answering turn")
            return
        if self.faults.has("ignore-dtmf") and turn.is_dtmf:
            log.info("fault ignore-dtmf: not answering DTMF")
            return
        trace_id, parent = self._trace_context(headers, inbound.message)
        async for rpc_id, frame in self.runner.run(
                turn, session, trace_id=trace_id, parent_span_id=parent,
                rpc_id=inbound.rpc_id, interrupt_event=interrupt_event,
                emit_trace=True):
            yield rpc_id, frame

    async def run_turn_http(self, inbound: P.Inbound, rpc_id) -> dict:
        turn, session, context_id, task_id = self._build_turn(inbound)

        if self.faults.has("http-task-never-completes"):
            task = P.task_envelope(
                task_id, context_id,
                {"state": P.TASK_STATE_WORKING}, [])
            self._tasks[task_id] = task
            return task

        if self._delegate_wait_guard(session) or (
                self.faults.has("ignore-dtmf") and turn.is_dtmf):
            task = P.task_envelope(task_id, context_id,
                                   {"state": P.TASK_STATE_WORKING}, [])
            self._tasks[task_id] = task
            return task

        headers: dict = {}  # HTTP trace context read from request headers by
        #                     the transport; passed via inbound.message metadata
        #                     fallback. The transport sets them on inbound.
        trace_id, parent = self._trace_context(inbound.raw.get("_headers", {}),
                                               inbound.message)

        artifacts: list[dict] = []
        by_id: dict = {}
        terminal_status: Optional[dict] = None
        async for _rid, frame in self.runner.run(
                turn, session, trace_id=trace_id, parent_span_id=parent,
                rpc_id=rpc_id, interrupt_event=None, emit_trace=True):
            result = frame.get("result", {})
            if "artifactUpdate" in result:
                # A Task holds complete artifacts: merge the streamed chunks of
                # one artifactId into a single artifact.
                art = result["artifactUpdate"]["artifact"]
                aid = art.get("artifactId")
                existing = by_id.get(aid)
                if existing is None:
                    merged = dict(art)
                    merged["parts"] = [dict(p) for p in art.get("parts", [])]
                    by_id[aid] = merged
                    artifacts.append(merged)
                else:
                    _merge_parts(existing["parts"], art.get("parts", []))
            elif "statusUpdate" in result:
                terminal_status = result["statusUpdate"]["status"]
            elif "message" in result:
                # finish-as-artifact on HTTP: attach as an artifact-like entry.
                artifacts.append({"artifactId": P.new_id(),
                                  "parts": result["message"]["parts"],
                                  "metadata": result["message"].get("metadata", {})})

        if terminal_status is None:
            # no-terminal fault on HTTP: leave the task working.
            terminal_status = {"state": P.TASK_STATE_WORKING}

        task = P.task_envelope(task_id, context_id, terminal_status, artifacts)
        self._tasks[task_id] = task
        return task

    def handle_get_task(self, inbound: P.Inbound, rpc_id) -> dict:
        task_id = (inbound.data or {}).get("id")
        task = self._tasks.get(task_id)
        if task is None:
            if self.faults.has("gettask-unknown-ok"):
                empty = P.task_envelope(
                    task_id or P.new_id(), P.new_id(),
                    {"state": P.TASK_STATE_COMPLETED}, [])
                return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": empty}}
            return P.error_frame(rpc_id, -32001, "Task not found", "unknown_task")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task}}


# ---- aiohttp app + CLI -------------------------------------------------------

def build_app(engine: Engine) -> "web.Application":
    # aiohttp is imported here, not at module load, so the Lambda handler
    # (deploy/lambda_handler.py) can reuse Engine without an aiohttp layer.
    from aiohttp import web
    from .transport_http import health_handler, http_handler
    from .transport_ws import websocket_handler

    app = web.Application()
    app["engine"] = engine

    async def a2a_dispatch(request: web.Request):
        # One path serves both: upgrade -> WS, POST -> HTTP JSON-RPC.
        if request.method == "GET" and \
                request.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket_handler(request)
        if request.method == "POST":
            return await http_handler(request)
        return web.Response(status=405, text="method not allowed")

    app.router.add_route("GET", "/a2a", a2a_dispatch)
    app.router.add_route("POST", "/a2a", a2a_dispatch)
    app.router.add_route("GET", "/health", health_handler)
    return app


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="a2a_connect.server",
        description="Amazon Connect Customer A2A extension reference server "
                    "(text only).")
    parser.add_argument("--host", default=None,
                        help="bind address (default 127.0.0.1; 0.0.0.0 to "
                             "listen on all interfaces). In AgentCore mode "
                             "the default is 0.0.0.0 inside a container and "
                             "127.0.0.1 otherwise")
    parser.add_argument("--port", type=int, default=None,
                        help="port (default 8080; 9000 with --agentcore a2a)")
    parser.add_argument("--backend", default="echo",
                        help="backend to serve: echo, bedrock, strands, or the name "
                             "of your own module in a2a_connect/backends/")
    parser.add_argument("--api-key-env", metavar="NAME",
                        help="read the API key from this environment variable")
    parser.add_argument("--api-key-file", metavar="PATH",
                        help="read the API key from this file")
    parser.add_argument("--api-key-secret-arn", metavar="ARN",
                        help="read the API key from this Secrets Manager secret")
    parser.add_argument("--fault", action="append", default=[], metavar="NAME",
                        help="enable a fault (repeatable); see --list-faults")
    parser.add_argument("--list-faults", action="store_true",
                        help="print the fault names and exit")
    parser.add_argument("--max-frame-bytes", type=int, default=1024 * 1024,
                        help="fragment outbound WS frames larger than this "
                             "(default 1 MiB)")
    parser.add_argument("--stream-delay-ms", type=int, default=0,
                        help="delay between response chunks")
    parser.add_argument("--agentcore", choices=["http", "a2a"], default=None,
                        help="serve an Amazon Bedrock AgentCore Runtime "
                             "contract: 'http' (POST /invocations, GET /ping "
                             "on 8080) or 'a2a' (POST /, GET /ping, "
                             "GET /.well-known/agent-card.json on 9000)")
    parser.add_argument("--auth", choices=["bearer", "agentcore"],
                        default="bearer",
                        help="'bearer' (the kit checks the API key; default) "
                             "or 'agentcore' (AgentCore's inbound auth guards "
                             "the runtime, so the kit's bearer check is off). "
                             "'agentcore' is only valid with --agentcore")
    parser.add_argument("--agent-name", default="starter-kit")
    # Bedrock backend options.
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--system-prompt-file", default=None)
    parser.add_argument("--reasoning-effort", default=None,
                        choices=["low", "medium", "high"],
                        help="reasoning effort for the Bedrock backend "
                             "(low|medium|high), mapped to each model family's "
                             "Converse reasoning shape. Overrides the "
                             "A2A_REASONING_EFFORT environment variable. "
                             "Default: none")
    return parser.parse_args(argv)


def _in_container() -> bool:
    """True inside a container image (the AgentCore Python SDK's own test)."""
    return os.path.exists("/.dockerenv") or bool(os.environ.get("DOCKER_CONTAINER"))


def _resolve_bind(args, in_container: bool) -> None:
    """Fill in the --host and --port defaults.

    Outside AgentCore mode the default is 127.0.0.1:8080. In AgentCore mode the
    port is the contract's (8080 for http, 9000 for a2a) and the host follows
    the AgentCore Python SDK: all interfaces inside a container, which
    AgentCore's container contract requires, and loopback otherwise. AgentCore
    reaches a direct code deployment over loopback.
    """
    if args.port is None:
        args.port = 9000 if args.agentcore == "a2a" else 8080
    if args.host is None:
        args.host = "0.0.0.0" if (args.agentcore and in_container) else "127.0.0.1"


def _make_auth(args, faults: Faults) -> Authenticator:
    if getattr(args, "auth", "bearer") == "agentcore":
        # AgentCore's inbound authorizer (IAM or JWT) is the gate; the kit's
        # own bearer check is off. No API key source is required.
        return PassThroughAuthenticator()
    fault = None
    for name in ("auth-accept-any", "auth-reject-raw", "auth-reject-json"):
        if faults.has(name):
            fault = name
            break
    if args.api_key_env:
        provider = key_from_env(args.api_key_env)
    elif args.api_key_file:
        provider = key_from_file(args.api_key_file)
    elif args.api_key_secret_arn:
        provider = key_from_secret_arn(args.api_key_secret_arn, args.region)
    else:
        raise SystemExit("one of --api-key-env, --api-key-file or "
                         "--api-key-secret-arn is required")
    return Authenticator(provider, fault=fault)


def _make_backend(args):
    if args.backend == "echo":
        from .backends.echo import make_backend
        return make_backend()
    if args.backend == "bedrock":
        from .backends.bedrock import make_backend
        return make_backend(model_id=args.model_id, region=args.region,
                            system_prompt_file=args.system_prompt_file,
                            reasoning_effort=args.reasoning_effort)
    if args.backend == "strands":
        from .backends.strands import make_backend
        return make_backend(model_id=args.model_id, region=args.region,
                            system_prompt_file=args.system_prompt_file)
    from .backends import load_custom_backend
    try:
        return load_custom_backend(args.backend)
    except ValueError as e:
        raise SystemExit(str(e)) from None


def main(argv: Optional[list[str]] = None) -> int:
    # aiohttp is imported here, not at module load, so the Lambda handler
    # (deploy/lambda_handler.py) can reuse Engine without an aiohttp layer.
    from aiohttp import web
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.list_faults:
        for name, desc in FAULTS.items():
            print(f"{name}: {desc}")
        return 0

    try:
        faults = Faults.from_names(args.fault)
    except UnknownFault as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # --auth agentcore is only meaningful in AgentCore mode.
    if args.auth == "agentcore" and not args.agentcore:
        print("--auth agentcore is only valid with --agentcore http|a2a",
              file=sys.stderr)
        return 2

    in_container = _in_container()
    _resolve_bind(args, in_container)

    # --auth agentcore turns the kit's own API key check off. That is safe only
    # where nothing but AgentCore can reach the port: loopback, or a container
    # whose port AgentCore fronts.
    if args.auth == "agentcore" and args.host not in LOOPBACK_HOSTS \
            and not in_container:
        print(f"refusing to start with --auth agentcore on non-loopback host "
              f"{args.host!r} outside a container: the API key check is off "
              f"in this mode, so bind 127.0.0.1, ::1 or localhost",
              file=sys.stderr)
        return 2

    # Faults are a local conformance-testing tool; they have no place in an
    # AgentCore deployment and the loopback guard below would not apply there.
    if args.agentcore and faults:
        print("--fault is not allowed in AgentCore mode (--agentcore)",
              file=sys.stderr)
        return 2

    # With any fault, refuse to start unless bound to a loopback host.
    if faults and args.host not in LOOPBACK_HOSTS:
        print(f"refusing to start with faults on non-loopback host "
              f"{args.host!r}; bind 127.0.0.1, ::1 or localhost",
              file=sys.stderr)
        return 2

    if faults:
        for name in sorted(faults.active):
            log.warning("FAULT ACTIVE: %s - %s", name, FAULTS[name])

    auth = _make_auth(args, faults)
    backend = _make_backend(args)
    engine = Engine(backend, auth, faults,
                    agent_name=args.agent_name,
                    max_frame_bytes=args.max_frame_bytes,
                    stream_delay_ms=args.stream_delay_ms)

    if args.agentcore:
        from .transport_agentcore import build_agentcore_app
        host, port = args.host, args.port
        app = build_agentcore_app(engine, args.agentcore)
        if args.agentcore == "http":
            log.info("AgentCore HTTP contract: POST http://%s:%d/invocations, "
                     "GET /ping (backend=%s, auth=%s)",
                     host, port, args.backend, args.auth)
        else:
            log.info("AgentCore A2A contract: POST http://%s:%d/, GET /ping, "
                     "GET /.well-known/agent-card.json (backend=%s, auth=%s)",
                     host, port, args.backend, args.auth)
        web.run_app(app, host=host, port=port, print=None)
        return 0

    app = build_app(engine)

    log.info("serving ws://%s:%d/a2a, http://%s:%d/a2a, GET /health "
             "(backend=%s)", args.host, args.port, args.host, args.port,
             args.backend)
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
