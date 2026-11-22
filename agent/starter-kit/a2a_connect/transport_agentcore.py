# SPDX-License-Identifier: MIT-0
"""Amazon Bedrock AgentCore Runtime transport.

AgentCore Runtime hosts an agent in a microVM and reaches it over one of two
contracts, both of which this module serves by reusing the existing engine:

* ``--agentcore http`` -- the HTTP service contract. ``POST /invocations``
  carries one JSON-RPC frame (the same body the ``/a2a`` HTTP transport
  accepts) and returns one JSON-RPC response; ``GET /ping`` is the health
  probe. This is what ``serverProtocol=HTTP`` runtimes call.
* ``--agentcore a2a`` -- the A2A service contract. ``POST /`` carries the
  JSON-RPC frame, ``GET /ping`` is the health probe, and
  ``GET /.well-known/agent-card.json`` returns a minimal agent card so an A2A
  client can discover the server. This is what ``serverProtocol=A2A`` runtimes
  call (port 9000, path ``/``).

Inbound auth is AgentCore's job in this deployment: the runtime validates SigV4
(IAM inbound) or a JWT (CUSTOM_JWT authorizer) before the request reaches this
code, so ``--auth agentcore`` runs the kit with its own bearer check disabled.
The bind follows the AgentCore Python SDK: all interfaces inside a container
(AgentCore's container contract), loopback otherwise. AgentCore's request proxy
reaches a direct code deployment over loopback, and outside a container the kit
refuses ``--auth agentcore`` on any non-loopback host.

A microVM that handles a turn for a ``contextId`` it has not seen (a fresh
microVM, or a session that landed on another instance) has no stored session;
the engine's ``get_or_create`` makes one on the fly. This module logs that case
so it is visible in the runtime logs.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

from . import EXT
from . import protocol as P

log = logging.getLogger("a2a_connect.agentcore")

# The two AgentCore service contracts this transport can serve.
MODE_HTTP = "http"
MODE_A2A = "a2a"

# Canonical ports for each contract (AgentCore's own defaults).
PORT_HTTP = 8080
PORT_A2A = 9000


def _ext_headers(engine) -> dict:
    if engine.faults.has("no-ext-echo"):
        return {}
    return {"A2A-Extensions": EXT}


async def _dispatch_frame(engine, request: web.Request) -> web.Response:
    """Run one JSON-RPC frame through the engine and return one response.

    This mirrors the ``/a2a`` HTTP transport exactly -- the AgentCore HTTP and
    A2A contracts both carry a single JSON-RPC request per POST -- except that
    the bearer check is owned by AgentCore's inbound auth when ``--auth
    agentcore`` is set (``engine.auth`` is then a pass-through authenticator).
    """
    if not engine.auth.check(request.headers.get("Authorization")):
        return web.json_response(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": 401, "message": "unauthorized"}},
            status=401, headers=_ext_headers(engine))

    try:
        frame = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response(
            P.error_frame(None, -32700, "Parse error", "parse_error"),
            headers=_ext_headers(engine))

    inbound = P.parse(frame)
    inbound.raw["_headers"] = {
        "traceparent": request.headers.get("traceparent"),
        "tracestate": request.headers.get("tracestate"),
    }
    rpc_id = inbound.rpc_id

    # A turn for a contextId with no stored session: a fresh microVM. The
    # engine creates the session on the fly; log it so it is visible in the
    # runtime logs.
    if inbound.kind in ("text_turn", "dtmf") and inbound.context_id and \
            engine.sessions.get(inbound.context_id) is None:
        log.info("turn for unknown contextId %s on this microVM; creating "
                 "session on the fly (a fresh microVM has no prior state)",
                 inbound.context_id)

    if inbound.kind == "get_task":
        return web.json_response(engine.handle_get_task(inbound, rpc_id),
                                 headers=_ext_headers(engine))

    if inbound.kind in ("init_session", "channel_state", "error"):
        frames = engine.handle_control(inbound, transport="agentcore")
        if frames:
            out = frames[0]
            if "id" in out:
                out["id"] = rpc_id
            return web.json_response(out, headers=_ext_headers(engine))
        return web.json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": {"acknowledged": True}},
            headers=_ext_headers(engine))

    if inbound.kind == "unknown":
        return web.json_response(
            P.error_frame(rpc_id, -32601, "Method not found", "unknown_method"),
            headers=_ext_headers(engine))

    if inbound.kind in ("text_turn", "dtmf"):
        task = await engine.run_turn_http(inbound, rpc_id)
        return web.json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task}},
            headers=_ext_headers(engine))

    # interruption/fragment are WebSocket-only; acknowledge.
    return web.json_response(
        {"jsonrpc": "2.0", "id": rpc_id, "result": {}},
        headers=_ext_headers(engine))


async def _ping_handler(request: web.Request) -> web.Response:
    """AgentCore health probe. 200 with a small JSON body once ready."""
    return web.json_response({"status": "Healthy"})


def _agent_card(engine) -> dict:
    """A minimal A2A agent card for the ``a2a`` contract's well-known path."""
    return {
        "protocolVersion": "1.0",
        "name": engine.agent_name,
        "description": "A2A starter kit on Amazon Bedrock AgentCore Runtime.",
        "url": "/",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


def build_agentcore_app(engine, mode: str) -> web.Application:
    """Build the aiohttp app for an AgentCore contract.

    ``mode`` is ``http`` (POST /invocations, GET /ping) or ``a2a`` (POST /,
    GET /ping, GET /.well-known/agent-card.json).
    """
    app = web.Application()
    app["engine"] = engine

    async def invocations(request: web.Request) -> web.Response:
        return await _dispatch_frame(engine, request)

    app.router.add_route("GET", "/ping", _ping_handler)

    if mode == MODE_HTTP:
        app.router.add_route("POST", "/invocations", invocations)
    elif mode == MODE_A2A:
        app.router.add_route("POST", "/", invocations)

        async def agent_card(request: web.Request) -> web.Response:
            return web.json_response(_agent_card(engine))

        app.router.add_route("GET", "/.well-known/agent-card.json", agent_card)
    else:
        raise ValueError(f"unknown agentcore mode {mode!r}")

    return app
