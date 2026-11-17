"""A2A v1.0 (JSON-RPC binding) + Amazon Connect Customer A2A extensions — transport-independent protocol layer.

Implements the contract in
  https://docs.aws.amazon.com/connect/latest/devguide/a2a-developer-guide.html
  https://docs.aws.amazon.com/connect/latest/devguide/a2a-sample-events.html
  https://docs.aws.amazon.com/connect/latest/adminguide/a2a-external-server.html

The protocol object turns one inbound JSON-RPC request into zero or more outbound JSON-RPC frames via `emit`.
`a2a_server.py` binds it to HTTPS (frames collected into a Task) and WebSocket (frames pushed as they happen).

Modes:
  handoff  ("Talk to the customer")  — INIT_SESSION, CHANNEL_STATE READY, then turns; FINISH ends the collaboration
  delegate ("Handle actions behind the scenes") — INIT_SESSION, then one message built by the Connect AI agent;
            we return a text result (+ structured data part). No CHANNEL_STATE, no FINISH.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.core.events import EventType, InputKind, InputMessage, OutputEvent, new_id
from app.core.orchestrator import Orchestrator
from app.core.state import Turn

log = logging.getLogger("a2a")

EXT = "https://docs.aws.amazon.com/connect/a2a/ext/v1"
ET = f"{EXT}/eventType"
EMPTY_INPUT = "<EMPTY_USER_INPUT>"
SUPPORTED_RATES = {8000, 16000, 24000}
TERMINAL_OK = "TASK_STATE_COMPLETED"

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class RpcError(Exception):
    def __init__(self, code: int, message: str, reason: str):
        super().__init__(message)
        self.code, self.message, self.reason = code, message, reason

    def frame(self, req_id: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {
            "code": self.code, "message": self.message,
            "data": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": self.reason, "domain": "a2a-protocol.org"}]}}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_traceparent(tp: str | None) -> tuple[str | None, str | None]:
    """W3C traceparent '00-<32hex trace>-<16hex span>-<flags>' -> (trace_id, parent_span_id)."""
    if not tp:
        return None, None
    parts = tp.strip().split("-")
    if len(parts) >= 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        return parts[1], parts[2]
    return None, None


# ---------------------------------------------------------------------- OTLP tracing
def _otlp_value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": "" if v is None else str(v)}


def _msg_event(t_ns: int, role: str, participant: str, content: str, msg_type: str = "TEXT_MESSAGE") -> dict[str, Any]:
    return {"name": "aws.connect.span.message", "timeUnixNano": str(t_ns), "attributes": [
        {"key": "aws.connect.span.message.role", "value": {"stringValue": role}},
        {"key": "aws.connect.span.message.type", "value": {"stringValue": msg_type}},
        {"key": "aws.connect.span.message.participant", "value": {"stringValue": participant}},
        {"key": "aws.connect.span.message.content", "value": {"stringValue": content}},
    ]}


def to_otlp(spans: list[dict[str, Any]], trace_id: str, parent_span_id: str | None, service: str) -> dict[str, Any]:
    """Orchestrator span dicts -> OTLP/JSON TracesData. spans[0] is the turn's invoke_agent root."""
    out = []
    root_id = secrets.token_hex(8)
    for i, sp in enumerate(spans):
        attrs = dict(sp.get("attrs") or {})
        inp, outp = attrs.pop("_input", None), attrs.pop("_output", None)
        span_id = root_id if i == 0 else secrets.token_hex(8)
        events = []
        if i == 0:
            if inp is not None:
                events.append(_msg_event(sp["start_ns"], "input", "user", inp))
            if outp is not None:
                events.append(_msg_event(sp["end_ns"], "output", "assistant", outp))
        elif sp["name"].startswith("execute_tool") and outp is not None:
            attrs["aws.connect.span.message.tool_type"] = "FUNCTION"
            events.append(_msg_event(sp["end_ns"], "output", "tool", outp, "TOOL_RESULT"))
        span = {
            "traceId": trace_id, "spanId": span_id, "name": sp["name"], "kind": sp["kind"],
            "startTimeUnixNano": str(sp["start_ns"]), "endTimeUnixNano": str(max(sp["end_ns"], sp["start_ns"])),
            "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items() if v is not None],
            "events": events,
            "status": {"code": 2, "message": sp["error"]} if sp.get("error") else {"code": 1},
        }
        parent = parent_span_id if i == 0 else root_id
        if parent:
            span["parentSpanId"] = parent
        out.append(span)
    return {"otlp": {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"scope": {"name": f"acc-poc.{service}", "version": "0.1.0"}, "spans": out}],
    }]}}


# ---------------------------------------------------------------------- per-connection / per-context state
@dataclass
class A2ASession:
    context_id: str
    mode: str                                     # handoff | delegate
    supported_finish: list[str] = field(default_factory=lambda: ["COMPLETE", "ESCALATE"])
    subscribe_tracing: bool = True
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    audio_in: dict[str, Any] | None = None
    audio_out: dict[str, Any] | None = None
    channel: str | None = None
    finished: bool = False
    current: asyncio.Task | None = None           # in-flight turn
    current_task_id: str | None = None
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)  # taskId -> A2A Task (for GetTask)
    timings: dict[str, float] = field(default_factory=dict)


class A2AProtocol:
    def __init__(self, orch: Orchestrator, engine: str | None = None, speak_first: bool = True):
        self.orch = orch
        self.cfg = get_settings()
        self.engine = engine or self.cfg.a2a_engine
        self.speak_first = speak_first
        self.sessions: dict[str, A2ASession] = {}  # contextId -> session (survives reconnects / per-message chat connections)
        self.fragments: dict[str, dict[int, str]] = {}

    # ------------------------------------------------------------------ entry point
    async def handle(self, req: dict[str, Any], emit: Emit, *, mode: str, trace: tuple[str | None, str | None] = (None, None)) -> None:
        req_id = req.get("id")
        try:
            if req.get("jsonrpc") != "2.0" or "method" not in req:
                raise RpcError(-32600, "Invalid Request", "INVALID_REQUEST")
            method = req["method"]
            params = req.get("params") or {}
            if method in ("SendMessage", "message/send", "SendStreamingMessage", "message/stream"):
                await self._send_message(req_id, params.get("message") or {}, emit, mode, trace)
            elif method in ("GetTask", "tasks/get"):
                await emit(self._result(req_id, {"task": self._find_task(params.get("id") or params.get("name", "").split("/")[-1])}))
            elif method in ("CancelTask", "tasks/cancel"):
                await self._cancel(req_id, params.get("id") or params.get("name", "").split("/")[-1], emit)
            else:
                raise RpcError(-32601, f"Method not found: {method}", "METHOD_NOT_FOUND")
        except RpcError as e:
            await emit(e.frame(req_id))

    # ------------------------------------------------------------------ SendMessage dispatch
    async def _send_message(self, req_id: Any, message: dict[str, Any], emit: Emit, mode: str, trace) -> None:
        ctx = message.get("contextId")
        meta = message.get("metadata") or {}
        event_type = meta.get(ET)
        parts = message.get("parts") or []
        data = next((p["data"] for p in parts if isinstance(p.get("data"), dict)), None)
        if not ctx and event_type != "FRAGMENT":
            raise RpcError(-32602, "message.contextId is required", "MISSING_CONTEXT_ID")

        if event_type == "FRAGMENT" or (data and "fragment" in data):
            full = self._reassemble((data or {}).get("fragment") or {}, ctx or "_")
            if full is not None:
                await self.handle(full, emit, mode=mode, trace=trace)
            return
        if event_type == "INIT_SESSION" or (data and "initSession" in data):
            await self._init_session(req_id, ctx, data["initSession"], emit, mode)
            return
        sess = self.sessions.get(ctx)
        if sess is None:
            raise RpcError(-32602, "INIT_SESSION required before other events", "SESSION_NOT_INITIALIZED")
        if event_type == "CHANNEL_STATE" or (data and "channelState" in data):
            if (data or {}).get("channelState", {}).get("state") == "READY":
                sess.ready.set()
                sess.timings["ready_at"] = time.perf_counter()
                orch_sess = self.orch.get(ctx)
                if self.speak_first and orch_sess and not orch_sess.state.history and not sess.finished:
                    await self._run_turn(req_id, sess, InputMessage(session_id=ctx, kind=InputKind.START,
                                                                    trace_id=trace[0] or new_id().ljust(32, "0")[:32],
                                                                    parent_span_id=trace[1]), emit)
            return
        if event_type == "INTERRUPTION" or (data and "interruption" in data):
            await self._interrupt(sess)
            return
        if event_type == "ERROR" or (data and "error" in data):
            log.warning("Connect reported error on %s: %s", ctx, (data or {}).get("error"))
            return
        if sess.finished:
            raise RpcError(-32600, "collaboration already finished", "SESSION_FINISHED")

        # ---- a customer turn ----
        if event_type == "DTMF_INPUT_COMPLETE" or (data and "dtmfInputComplete" in data):
            digits = data["dtmfInputComplete"].get("inputString", "")
            msg = InputMessage(session_id=ctx, kind=InputKind.TEXT, text=f"{digits}분" if digits.isdigit() else digits)
        else:
            if any("raw" in p for p in parts):
                raise RpcError(-32602, "audio input is not supported by this agent (text only, Phase 1)", "AUDIO_NOT_SUPPORTED")
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
            kind = InputKind.SILENCE if text == EMPTY_INPUT else InputKind.TEXT
            msg = InputMessage(session_id=ctx, kind=kind, text="" if kind == InputKind.SILENCE else text)
        if trace[0]:
            msg.trace_id, msg.parent_span_id = trace
        if sess.mode == "handoff" and not sess.ready.is_set():
            try:  # output is gated on READY; wait briefly in case frames arrive out of order
                await asyncio.wait_for(sess.ready.wait(), timeout=5)
            except TimeoutError as e:
                raise RpcError(-32600, "CHANNEL_STATE READY not received", "CHANNEL_NOT_READY") from e
        if sess.current and not sess.current.done():  # a new customer turn while we are still answering = barge-in
            await self._interrupt(sess)
        await self._run_turn(req_id, sess, msg, emit)

    # ------------------------------------------------------------------ INIT_SESSION
    async def _init_session(self, req_id: Any, ctx: str, init: dict[str, Any], emit: Emit, mode: str) -> None:
        t0 = time.perf_counter()
        existing = self.sessions.get(ctx)
        sess = A2ASession(context_id=ctx, mode=mode)
        if existing:  # reconnect / per-message chat connection: keep tasks, re-init channel state
            sess.tasks, sess.finished = existing.tasks, existing.finished
        sess.supported_finish = list(init.get("supportedFinishTypes") or ["COMPLETE", "ESCALATE"])
        sess.subscribe_tracing = bool(init.get("subscribeToTracingEvents", True))
        sess.channel = ((init.get("contactContext") or {}).get("channel"))
        response: dict[str, Any] = {"statusCode": "200"}
        if init.get("audioInputConfiguration"):
            sess.audio_in = init["audioInputConfiguration"]
            sess.audio_out = dict(init.get("audioOutputConfiguration") or init["audioInputConfiguration"])
            if sess.audio_out.get("sampleRateHertz") not in SUPPORTED_RATES:  # counter-propose a full re-declaration
                sess.audio_out = {"encoding": "LINEAR_PCM", "sampleRateHertz": 16000, "sampleSizeBits": 16, "channelCount": 1}
                response["audioOutputConfiguration"] = sess.audio_out
        self.sessions[ctx] = sess

        if self.orch.get(ctx) is None:
            o = self.orch.create_session(self.engine, session_id=ctx)
            for h in init.get("history") or []:  # Connect history is oldest-first A2A Messages
                text = " ".join(p.get("text", "") for p in h.get("parts") or [] if "text" in p).strip()
                if text:
                    o.state.history.append(Turn(role="driver" if h.get("role") == "ROLE_USER" else "agent", text=text))
        sess.timings["init_ms"] = (time.perf_counter() - t0) * 1000
        await emit(self._result(req_id, {"message": {
            "messageId": new_id("m-"), "contextId": ctx, "role": "ROLE_AGENT",
            "parts": [{"data": {"initSessionResponse": response}, "mediaType": "application/json"}],
            "extensions": [EXT], "metadata": {ET: "INIT_SESSION_RESPONSE"}}}))

    # ------------------------------------------------------------------ turns
    async def _run_turn(self, req_id: Any, sess: A2ASession, msg: InputMessage, emit: Emit) -> None:
        task_id = new_id("task-")
        task = {"id": task_id, "contextId": sess.context_id, "status": {"state": "TASK_STATE_WORKING", "timestamp": now_iso()},
                "artifacts": [], "metadata": {"acc.turnId": msg.turn_id, "acc.traceId": msg.trace_id}}
        sess.tasks[task_id] = task
        sess.current_task_id = task_id
        coro = self._turn_body(req_id, sess, msg, task, emit)
        sess.current = asyncio.ensure_future(asyncio.wait_for(coro, timeout=self.cfg.turn_timeout_s))
        try:
            await sess.current
        except asyncio.CancelledError:
            if not sess.current.cancelled():
                raise
            # interrupted: no lastChunk for the open artifact; close the task normally (FAILED/CANCELED would end the collaboration)
            await self._status(req_id, sess, task, TERMINAL_OK, emit, extra_meta={"acc.interrupted": True})
        except TimeoutError:
            await self._fail(req_id, sess, task, 408, "turn timed out", "TIMEOUT", emit)
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            await self._fail(req_id, sess, task, 500, str(e)[:200], "INTERNAL_ERROR", emit)
        finally:
            sess.current = None

    async def _turn_body(self, req_id: Any, sess: A2ASession, msg: InputMessage, task: dict[str, Any], emit: Emit) -> None:
        open_artifact: dict[str, Any] | None = None
        pending_chunk: str | None = None
        finish: dict[str, Any] | None = None
        spans: list[dict[str, Any]] = []
        t0 = time.perf_counter()
        first_chunk_ms: float | None = None
        reply_text = ""

        async def flush(last: bool) -> None:
            nonlocal pending_chunk, open_artifact, first_chunk_ms
            if pending_chunk is None or open_artifact is None:
                return
            if sess.mode == "delegate":
                return
            if first_chunk_ms is None:
                first_chunk_ms = (time.perf_counter() - t0) * 1000
            append = open_artifact["n"] > 0
            open_artifact["n"] += 1
            open_artifact["parts"].append({"text": pending_chunk, "mediaType": "text/plain"})
            await emit(self._result(req_id, {"artifactUpdate": {
                "taskId": task["id"], "contextId": sess.context_id,
                "artifact": {"artifactId": open_artifact["artifactId"], "parts": [{"text": pending_chunk, "mediaType": "text/plain"}],
                             "metadata": {ET: "TEXT_RESPONSE_CHUNK"}},
                "append": append, "lastChunk": last}}))
            pending_chunk = None

        async for ev in self.orch.handle(msg):
            if ev.type == EventType.TEXT_CHUNK:
                if open_artifact is None or open_artifact["mid"] != ev.message_id:
                    open_artifact = {"artifactId": new_id("resp-"), "mid": ev.message_id, "n": 0, "parts": []}
                    task["artifacts"].append(open_artifact)
                await flush(last=False)  # one-chunk lookahead so the final chunk can carry lastChunk=true
                pending_chunk = ev.text
            elif ev.type == EventType.TEXT_DONE:
                await flush(last=True)
                reply_text = (reply_text + " " + (ev.text or "")).strip()
                open_artifact = None
            elif ev.type == EventType.FINISH:
                finish = ev.data
            elif ev.type == EventType.TURN_END:
                spans = ev.data.get("spans") or []
                task["metadata"]["acc.metrics"] = ev.data.get("metrics")
            elif ev.type == EventType.ERROR:
                raise RpcError(-32600, ev.data.get("reason", "error"), "TURN_REJECTED")

        task["metadata"]["acc.firstChunkMs"] = first_chunk_ms
        task["artifacts"] = [{"artifactId": a["artifactId"], "parts": a["parts"], "metadata": {ET: "TEXT_RESPONSE_CHUNK"}}
                             if "mid" in a else a for a in task["artifacts"]]
        if sess.mode == "delegate":
            await self._delegate_result(req_id, sess, task, reply_text, emit)
        if sess.subscribe_tracing and spans:
            trace = to_otlp(spans, msg.trace_id, msg.parent_span_id, self.cfg.a2a_agent_name)
            trace_art = {"artifactId": new_id("trace-"), "parts": [{"data": {"tracingSpan": trace}, "mediaType": "application/json"}],
                         "metadata": {ET: "TRACING_SPAN"}}
            task["artifacts"].append(trace_art)
            await emit(self._result(req_id, {"artifactUpdate": {"taskId": task["id"], "contextId": sess.context_id,
                                                               "artifact": trace_art, "append": False, "lastChunk": True}}))
        finish_part = None
        if finish and sess.mode == "handoff":
            ftype = finish["type"] if finish["type"] in sess.supported_finish else None
            if ftype is None and "COMPLETE" in sess.supported_finish:
                ftype = "COMPLETE"
            if ftype:
                finish_part = {"type": ftype, "reason": self._finish_reason(finish)}
                sess.finished = True
        await self._status(req_id, sess, task, TERMINAL_OK, emit, finish=finish_part)

    async def _delegate_result(self, req_id: Any, sess: A2ASession, task: dict[str, Any], reply: str, emit: Emit) -> None:
        """Behind-the-scenes: return a text result the Connect AI agent can act on, plus structured data."""
        st = self.orch.get(sess.context_id).state
        m = task["metadata"].get("acc.metrics") or {}
        summary = {"next_action": m.get("action"), "suggested_reply": reply, "collected": st.slots_summary(),
                   "tms_updated": st.tms_updated, "finished": st.finished, "finish_reason": st.finish_reason}
        text = (f"next_action={summary['next_action']}; collected={json.dumps(summary['collected'], ensure_ascii=False)}; "
                f"tms_updated={st.tms_updated}; suggested_reply={reply}")
        art = {"artifactId": new_id("result-"), "parts": [{"text": text, "mediaType": "text/plain"},
                                                         {"data": summary, "mediaType": "application/json"}],
               "metadata": {ET: "TEXT_RESPONSE_CHUNK"}}
        task["artifacts"].append(art)
        await emit(self._result(req_id, {"artifactUpdate": {"taskId": task["id"], "contextId": sess.context_id,
                                                           "artifact": art, "append": False, "lastChunk": True}}))

    @staticmethod
    def _finish_reason(finish: dict[str, Any]) -> str:
        return {
            "all_slots_collected": "Driver ETA and transport issues collected and recorded in TMS.",
            "driver_request": "Driver asked to speak with a human dispatcher.",
            "severe_issue": "Vehicle breakdown or accident reported; recorded in TMS, needs a human dispatcher.",
        }.get(finish.get("reason") or "", f"{finish.get('outcome')}: {finish.get('reason')}")

    async def _status(self, req_id: Any, sess: A2ASession, task: dict[str, Any], state: str, emit: Emit,
                      finish: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
                      extra_meta: dict[str, Any] | None = None) -> None:
        status: dict[str, Any] = {"state": state, "timestamp": now_iso()}
        if finish:
            status["message"] = {"messageId": new_id("m-"), "role": "ROLE_AGENT",
                                 "parts": [{"data": {"finish": finish}, "mediaType": "application/json"}],
                                 "metadata": {ET: "FINISH"}}
        if error:
            status["message"] = {"messageId": new_id("m-"), "role": "ROLE_AGENT",
                                 "parts": [{"data": {"error": error}, "mediaType": "application/json"}],
                                 "metadata": {ET: "ERROR"}}
        task["status"] = status
        if extra_meta:
            task["metadata"].update(extra_meta)
        await emit(self._result(req_id, {"statusUpdate": {"taskId": task["id"], "contextId": sess.context_id, "status": status}}))

    async def _fail(self, req_id, sess, task, code: int, reason: str, category: str, emit: Emit) -> None:
        sess.finished = True  # FAILED ends the collaboration on the Connect side
        await self._status(req_id, sess, task, "TASK_STATE_FAILED", emit,
                           error={"errorCode": code, "errorReason": reason, "errorCategory": category})

    async def _interrupt(self, sess: A2ASession) -> None:
        cur = sess.current
        if cur and not cur.done():
            cur.cancel()
            try:
                await cur
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _cancel(self, req_id: Any, task_id: str, emit: Emit) -> None:
        for sess in self.sessions.values():
            if task_id in sess.tasks:
                if sess.current_task_id == task_id:
                    await self._interrupt(sess)
                task = sess.tasks[task_id]
                task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now_iso()}
                await emit(self._result(req_id, {"task": task}))
                return
        raise RpcError(-32001, "Task not found", "TASK_NOT_FOUND")

    def _find_task(self, task_id: str) -> dict[str, Any]:
        for sess in self.sessions.values():
            if task_id in sess.tasks:
                return sess.tasks[task_id]
        raise RpcError(-32001, "Task not found", "TASK_NOT_FOUND")

    def _reassemble(self, frag: dict[str, Any], key: str) -> dict[str, Any] | None:
        fid, n, total = frag.get("fragmentId"), int(frag.get("partNumber", 0)), int(frag.get("totalParts", 1))
        buf = self.fragments.setdefault(f"{key}:{fid}", {})
        buf[n] = frag.get("payload", "")
        if len(buf) < total:
            return None
        payload = "".join(buf[i] for i in range(total))
        del self.fragments[f"{key}:{fid}"]
        return json.loads(base64.b64decode(payload))

    @staticmethod
    def _result(req_id: Any, value: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": value}


def agent_card(base_url: str) -> dict[str, Any]:
    """A2A Agent Card (served at /.well-known/agent-card.json). Connect registration does not read it
    (it uses the AppIntegrations AccessUrl), but it lets other A2A clients discover the agent."""
    cfg = get_settings()
    return {
        "protocolVersion": "1.0",
        "name": cfg.a2a_agent_name,
        "description": "Calls truck drivers to collect ETA and transport issues, updates TMS, escalates to dispatch.",
        "version": "0.1.0",
        "supportedInterfaces": [
            {"url": f"{base_url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": f"{base_url.replace('http', 'ws', 1)}/a2a/ws", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        ],
        "capabilities": {"streaming": True, "pushNotifications": False,
                         "extensions": [{"uri": EXT, "description": "Amazon Connect Customer A2A extensions", "required": False}]},
        "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "eta-check", "name": "Driver ETA & issue check",
                    "description": "Collect ETA, transport issue type and delay from a driver; update TMS.",
                    "tags": ["logistics", "eta", "korean"], "examples": ["도착까지 30분 걸려요"]}],
    }
