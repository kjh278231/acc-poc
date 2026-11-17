"""FastAPI app: web UI + REST/WS adapter + A2A adapter, all on one orchestrator.

Run:  uv run uvicorn app.main:create_app --factory --port 8080
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.adapters import a2a_server, rest
from app.adapters.a2a import A2AProtocol
from app.factory import build_orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

WEB = Path(__file__).parent / "web"


def create_app(orch=None) -> FastAPI:
    orch = orch or build_orchestrator()
    app = FastAPI(title="acc-poc: Haiku Only vs Jev+Haiku")
    app.state.orch = orch
    app.state.a2a = A2AProtocol(orch)
    app.include_router(rest.build_router(orch))
    app.include_router(a2a_server.build_router(app.state.a2a))

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/healthz")
    async def health():
        return {"ok": True, "engines": list(orch.engines)}

    return app

