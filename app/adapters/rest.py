"""REST + WebSocket adapter for the PoC web UI and simple integrations."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.events import InputKind, InputMessage, OutputEvent
from app.core.orchestrator import Orchestrator


class NewSession(BaseModel):
    engine: str = "jev_haiku"


class TurnIn(BaseModel):
    text: str


def ev_json(e: OutputEvent) -> dict[str, Any]:
    d = e.model_dump(mode="json", exclude={"t"})
    if e.type.value == "turn_end":
        d["data"] = {"metrics": e.data.get("metrics")}  # spans are for A2A tracing; keep UI payload small
    return d


def build_router(orch: Orchestrator) -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/engines")
    async def engines() -> list[str]:
        return list(orch.engines)

    @r.post("/sessions")
    async def create(body: NewSession) -> dict[str, Any]:
        try:
            s = orch.create_session(body.engine)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e
        evs = await orch.run_turn(InputMessage(session_id=s.state.session_id, kind=InputKind.START))
        return {"session_id": s.state.session_id, "events": [ev_json(e) for e in evs]}

    @r.post("/sessions/{sid}/turns")
    async def turn(sid: str, body: TurnIn) -> dict[str, Any]:
        if not orch.get(sid):
            raise HTTPException(404, "no such session")
        evs = await orch.run_turn(InputMessage(session_id=sid, text=body.text))
        return {"events": [ev_json(e) for e in evs], "state": orch.get(sid).state.model_dump(mode="json")}

    @r.get("/sessions/{sid}")
    async def state(sid: str) -> dict[str, Any]:
        s = orch.get(sid)
        if not s:
            raise HTTPException(404, "no such session")
        return {"state": s.state.model_dump(mode="json"), "metrics": s.turn_metrics}

    @r.get("/tms")
    async def tms() -> dict[str, Any]:
        return {"records": orch.tms.records, "tickets": orch.tms.tickets}

    @r.websocket("/ws")
    async def ws(sock: WebSocket):
        """Streaming chat: {"type":"start","engine":...} then {"type":"text","text":...}; events pushed as they happen."""
        await sock.accept()
        sid = None
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                if msg.get("type") == "start":
                    sid = orch.create_session(msg.get("engine", "jev_haiku")).state.session_id
                    inp = InputMessage(session_id=sid, kind=InputKind.START)
                elif sid:
                    inp = InputMessage(session_id=sid, text=msg.get("text", ""))
                else:
                    continue
                async for e in orch.handle(inp):
                    await sock.send_text(json.dumps(ev_json(e), ensure_ascii=False))
                await sock.send_text(json.dumps({"type": "state", "data": orch.get(sid).state.model_dump(mode="json")},
                                                ensure_ascii=False))
        except WebSocketDisconnect:
            return

    return r
