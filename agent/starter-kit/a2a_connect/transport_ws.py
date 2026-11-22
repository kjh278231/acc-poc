# SPDX-License-Identifier: MIT-0
"""WebSocket transport.

Serves the upgrade on ``GET /a2a`` with ``Upgrade: websocket``. Checks the
bearer on the upgrade, echoes ``A2A-Extensions`` on the upgrade response, then
runs the per-connection message loop: each inbound JSON-RPC frame is handed to
the shared engine, and every outbound frame is pushed back over the socket
(fragmented when it exceeds ``--max-frame-bytes``).

One task at a time per connection. An INTERRUPTION mid-turn sets the running
turn's interrupt event.

In talk to the customer mode a turn is streamed: response artifacts, the trace,
then the terminal status. In handle actions behind the scenes mode (no READY on
the session) a turn is answered with one complete Task, as on HTTPS, because
Connect reads that reply as the collaborator's result.
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from . import EXT
from . import protocol as P
from .fragments import FragmentReassembler, fragment_frame

log = logging.getLogger("a2a_connect.ws")


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    engine = request.app["engine"]

    # Bearer check on the upgrade.
    if not engine.auth.check(request.headers.get("Authorization")):
        log.info("ws upgrade refused: bearer check failed")
        return web.Response(status=401, text="unauthorized")

    # Extension echo on the upgrade response (unless the no-ext-echo fault).
    ws = web.WebSocketResponse()
    if not engine.faults.has("no-ext-echo"):
        ws.headers["A2A-Extensions"] = EXT
    await ws.prepare(request)
    log.info("ws connection opened")

    reassembler = FragmentReassembler()
    conn = _WSConnection(engine, ws, request.headers)

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            # Inbound fragment reassembly.
            inbound = P.parse(frame)
            if inbound.kind == "fragment":
                if engine.faults.has("no-fragment-reassembly"):
                    continue
                fragment = inbound.data.get("fragment") or {}
                completed = reassembler.add(fragment)
                if completed is None:
                    continue
                frame = completed
                inbound = P.parse(frame)
            await conn.handle(inbound)
        elif msg.type == WSMsgType.ERROR:
            log.info("ws error: %s", ws.exception())
            break

    await conn.drain()
    log.info("ws connection closed")
    return ws


class _WSConnection:
    """One WebSocket connection. Runs each turn as a background task so an
    INTERRUPTION read off the socket can set the running turn's interrupt
    event. Turns are serialized (one task at a time): a new turn waits for the
    previous one to finish."""

    def __init__(self, engine, ws: web.WebSocketResponse, headers) -> None:
        self.engine = engine
        self.ws = ws
        self.headers = headers
        self._interrupt: asyncio.Event | None = None
        self._turn_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()

    async def _send(self, frame: dict, rpc_id) -> None:
        parts = fragment_frame(
            frame, self.engine.max_frame_bytes, rpc_id,
            start_part=1 if self.engine.faults.has("bad-outbound-fragment") else 0)
        async with self._send_lock:
            if parts is None:
                await self.ws.send_str(json.dumps(frame))
            else:
                for p in parts:
                    await self.ws.send_str(json.dumps(p))

    async def drain(self) -> None:
        if self._turn_task is not None:
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass

    async def handle(self, inbound: P.Inbound) -> None:
        engine = self.engine

        if inbound.kind == "interruption":
            if self._interrupt is not None:
                self._interrupt.set()
            return

        if inbound.kind in ("init_session", "channel_state", "error"):
            # Wait for any running turn so one task at a time holds.
            await self.drain()
            for frame in engine.handle_control(inbound, transport="ws"):
                await self._send(frame, inbound.rpc_id)
            return

        if inbound.kind == "unknown":
            if not engine.faults.has("ignore-unknown-method"):
                await self._send(
                    P.error_frame(inbound.rpc_id, -32601, "Method not found",
                                  "unknown_method"),
                    inbound.rpc_id)
            return

        if inbound.kind in ("text_turn", "dtmf"):
            # Serialize: finish the previous turn before starting this one.
            await self.drain()
            self._interrupt = asyncio.Event()
            self._turn_task = asyncio.ensure_future(self._run_turn(inbound))
            return

    async def _run_turn(self, inbound: P.Inbound) -> None:
        engine = self.engine
        headers = {
            "traceparent": self.headers.get("traceparent"),
            "tracestate": self.headers.get("tracestate"),
        }
        try:
            session = engine.sessions.get(inbound.context_id) if inbound.context_id else None
            if (session is not None and session.mode == "delegate"
                    and not engine.faults.has("ws-delegate-stream")):
                # Handle actions behind the scenes: Connect reads the reply to its
                # SendMessage as the result, so it must be a Task with an id. A
                # streamed artifactUpdate first gets the session closed with "A2A
                # WS SendMessage response missing task id" (Connect chat contact,
                # 2026-10-18). Answer with one complete Task, the HTTPS shape.
                if engine._delegate_wait_guard(session):
                    log.info("fault wait-ready-in-delegate: not answering turn")
                    return
                inbound.raw["_headers"] = headers
                task = await engine.run_turn_http(inbound, inbound.rpc_id)
                await self._send(P.result_frame(inbound.rpc_id, "task", task), inbound.rpc_id)
                return
            async for rpc_id, frame in engine.run_turn(
                    inbound, transport="ws", headers=headers,
                    interrupt_event=self._interrupt):
                await self._send(frame, rpc_id)
        except Exception:  # pragma: no cover - defensive
            log.exception("turn task failed")
        finally:
            self._interrupt = None
