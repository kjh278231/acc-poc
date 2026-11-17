"""Wiring: build LLM client, engines, TMS mock and orchestrator from settings."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.core.orchestrator import Orchestrator
from app.engines.haiku_only import HaikuOnlyEngine
from app.engines.jev_haiku import JevHaikuEngine
from app.services.tms_mock import TmsMock


def build_llm(settings: Settings | None = None):
    s = settings or get_settings()
    if s.is_mock:
        from app.services.llm_mock import MockLLM
        return MockLLM()
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (put it in .env) or run with LLM_MODE=mock")
    from app.services.llm import OpenRouterClient
    return OpenRouterClient(s)


def build_engines(llm, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    haiku = HaikuOnlyEngine(llm)
    return {"haiku_only": haiku, "jev_haiku": JevHaikuEngine(llm, haiku, threshold=s.jev_confidence_threshold)}


def build_orchestrator(llm=None, tms: TmsMock | None = None, settings: Settings | None = None) -> Orchestrator:
    s = settings or get_settings()
    llm = llm or build_llm(s)
    return Orchestrator(build_engines(llm, s), tms or TmsMock(latency_ms=s.tms_latency_ms, failure_rate=s.tms_failure_rate))
