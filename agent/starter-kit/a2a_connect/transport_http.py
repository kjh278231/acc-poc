# SPDX-License-Identifier: MIT-0
"""HTTPS JSON-RPC transport.

Serves ``POST /a2a``. Each request gets one JSON-RPC response echoing its id.
INIT_SESSION returns ``result.message``; CHANNEL_STATE returns a non-error
result; a turn returns ``result.task`` (an A2A Task with its artifacts and
terminal status, the trace artifact listed after the responses); GetTask returns
the stored task. The bearer is checked and ``A2A-Extensions`` echoed on every
response. Fragmentation never applies on HTTP.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

from . import EXT
from . import protocol as P

log = logging.getLogger("a2a_connect.http")


def _headers(engine) -> dict:
    if engine.faults.has("no-ext-echo"):
        return {}
    return {"A2A-Extensions": EXT}


async def http_handler(request: web.Request) -> web.Response:
    engine = request.app["engine"]

    if not engine.auth.check(request.headers.get("Authorization")):
        return web.json_response(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": 401, "message": "unauthorized"}},
            status=401, headers=_headers(engine))

    try:
        frame = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response(
            P.error_frame(None, -32700, "Parse error", "parse_error"),
            headers=_headers(engine))

    inbound = P.parse(frame)
    # Make request headers available to the engine for HTTP trace context.
    inbound.raw["_headers"] = {
        "traceparent": request.headers.get("traceparent"),
        "tracestate": request.headers.get("tracestate"),
    }
    rpc_id = inbound.rpc_id
    if engine.faults.has("http-wrong-id"):
        rpc_id = P.new_id()

    if inbound.kind == "get_task":
        response = engine.handle_get_task(inbound, rpc_id)
        return web.json_response(response, headers=_headers(engine))

    if inbound.kind in ("init_session", "channel_state", "error"):
        frames = engine.handle_control(inbound, transport="http")
        # Control replies are a single JSON-RPC response; take the first (there
        # is exactly one for init/channel_state).
        if frames:
            out = frames[0]
            if "id" in out:
                out["id"] = rpc_id
            return web.json_response(out, headers=_headers(engine))
        # CHANNEL_STATE with no explicit reply: a non-error acknowledgement.
        return web.json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": {"acknowledged": True}},
            headers=_headers(engine))

    if inbound.kind == "unknown":
        if engine.faults.has("ignore-unknown-method"):
            return web.json_response(
                {"jsonrpc": "2.0", "id": rpc_id, "result": {}},
                headers=_headers(engine))
        return web.json_response(
            P.error_frame(rpc_id, -32601, "Method not found", "unknown_method"),
            headers=_headers(engine))

    if inbound.kind in ("text_turn", "dtmf"):
        task = await engine.run_turn_http(inbound, rpc_id)
        return web.json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task}},
            headers=_headers(engine))

    # interruption/fragment are WebSocket-only; acknowledge on HTTP.
    return web.json_response(
        {"jsonrpc": "2.0", "id": rpc_id, "result": {}},
        headers=_headers(engine))


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")
