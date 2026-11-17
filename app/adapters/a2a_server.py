"""HTTPS and WebSocket transports for the A2A protocol layer.

  POST /a2a                 Talk to the customer, HTTPS JSON-RPC (one response per request; turns return a Task)
  POST /a2a/delegate        Behind the scenes, HTTPS JSON-RPC
  WS   /a2a/ws              Talk to the customer, WebSocket (frames pushed as produced; required for audio later)
  WS   /a2a/delegate/ws     Behind the scenes, WebSocket
  GET  /.well-known/agent-card.json

Every request / upgrade must carry `Authorization: Bearer <A2A_API_KEY>`. The Connect extension header is echoed.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from app.adapters.a2a import EXT, A2AProtocol, RpcError, agent_card, parse_traceparent
from app.config import get_settings

log = logging.getLogger("a2a.server")


def _authorized(auth: str | None) -> bool:
    key = get_settings().a2a_api_key
    return bool(auth) and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), key)


def build_router(proto: A2AProtocol) -> APIRouter:
    r = APIRouter()

    @r.get("/.well-known/agent-card.json")
    async def card(request: Request) -> dict[str, Any]:
        return agent_card(str(request.base_url).rstrip("/"))

    async def http_entry(request: Request, mode: str, authorization: str | None, traceparent: str | None):
        headers = {"A2A-Extensions": EXT}
        if not _authorized(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers)
        try:
            req = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(RpcError(-32700, "Parse error", "PARSE_ERROR").frame(None), headers=headers)
        trace = parse_traceparent(traceparent)

        if req.get("method") in ("SendStreamingMessage", "message/stream"):  # SSE variant (standard A2A streaming)
            queue: asyncio.Queue = asyncio.Queue()

            async def emit_q(frame: dict[str, Any]) -> None:
                await queue.put(frame)

            async def run() -> None:
                try:
                    await proto.handle(req, emit_q, mode=mode, trace=trace)
                finally:
                    await queue.put(None)

            async def gen():
                task = asyncio.create_task(run())
                while (frame := await queue.get()) is not None:
                    yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
                await task

            return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

        frames: list[dict[str, Any]] = []

        async def emit(frame: dict[str, Any]) -> None:
            frames.append(frame)

        await proto.handle(req, emit, mode=mode, trace=trace)
        return JSONResponse(collapse(proto, req, frames), headers=headers)

    @r.post("/a2a")
    async def a2a_handoff(request: Request, authorization: str | None = Header(None), traceparent: str | None = Header(None)):
        return await http_entry(request, "handoff", authorization, traceparent)

    @r.post("/a2a/delegate")
    async def a2a_delegate(request: Request, authorization: str | None = Header(None), traceparent: str | None = Header(None)):
        return await http_entry(request, "delegate", authorization, traceparent)

    async def ws_entry(ws: WebSocket, mode: str) -> None:
        if not _authorized(ws.headers.get("authorization")):
            await ws.close(code=1008)  # policy violation (HTTP 401 is not expressible after accept in Starlette)
            return
        await ws.accept(headers=[(b"a2a-extensions", EXT.encode())])
        trace = parse_traceparent(ws.headers.get("traceparent"))
        send_lock = asyncio.Lock()
        inflight: set[asyncio.Task] = set()

        async def emit(frame: dict[str, Any]) -> None:
            async with send_lock:
                await ws.send_text(json.dumps(frame, ensure_ascii=False))

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    await emit(RpcError(-32700, "Parse error", "PARSE_ERROR").frame(None))
                    continue
                # frames are processed concurrently so an INTERRUPTION can cancel a turn that is still running
                t = asyncio.create_task(proto.handle(req, emit, mode=mode, trace=trace))
                inflight.add(t)
                t.add_done_callback(inflight.discard)
        except WebSocketDisconnect:
            for t in inflight:
                t.cancel()

    @r.websocket("/a2a/ws")
    async def ws_handoff(ws: WebSocket):
        await ws_entry(ws, "handoff")

    @r.websocket("/a2a/delegate/ws")
    async def ws_delegate(ws: WebSocket):
        await ws_entry(ws, "delegate")

    return r


def collapse(proto: A2AProtocol, req: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    """HTTPS: fold streamed frames into one JSON-RPC response (a Message, or the turn's Task)."""
    rid = req.get("id")
    for f in frames:
        if "error" in f:
            return f
    direct = [f for f in frames if "message" in f.get("result", {}) or "task" in f.get("result", {})]
    if direct and len(frames) == 1:
        return direct[0]
    task_id = next((f["result"][k]["taskId"] for f in frames for k in ("statusUpdate", "artifactUpdate") if k in f.get("result", {})), None)
    if task_id:
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": {"task": proto._find_task(task_id)}}
        except RpcError as e:
            return e.frame(rid)
    # CHANNEL_STATE / INTERRUPTION / ERROR without output: acknowledge with an empty agent message
    ctx = ((req.get("params") or {}).get("message") or {}).get("contextId")
    return {"jsonrpc": "2.0", "id": rid, "result": {"message": {
        "messageId": f"ack-{rid}", "contextId": ctx, "role": "ROLE_AGENT", "parts": [], "extensions": [EXT]}}}
