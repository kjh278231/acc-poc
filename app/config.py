"""Settings from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api"
    llm_mode: str = "live"  # live | mock

    haiku_model: str = "anthropic/claude-haiku-5.5"
    haiku_reasoning: str = "none"  # none | low | medium
    haiku_max_tokens: int = 400
    # OpenRouter list prices (USD / 1M tokens, checked 2026-11-17). Used only when the response has no usage.cost.
    haiku_price_in: float = 0.10
    haiku_price_out: float = 0.50

    jev_model: str = "typesafe/jev-1.14"
    jev_fallback_model: str = "typesafe/jev-1.13"
    jev_confidence_threshold: float = 0.8
    jev_price_in: float = 0.042  # docs.typesafe.ai/models: $0.042 / 1M input tokens, output free

    http_timeout_s: float = 15.0
    http_max_retries: int = 2

    tms_latency_ms: int = 150
    tms_failure_rate: float = 0.0

    turn_timeout_s: float = 10.0
    max_reprompts_per_slot: int = 2
    max_off_topic: int = 3

    a2a_api_key: str = "local-dev-key"
    a2a_agent_name: str = "eta-check-agent"
    a2a_engine: str = "jev_haiku"  # engine used for Connect sessions

    bench_max_cost_usd: float = 2.0

    @property
    def is_mock(self) -> bool:
        return self.llm_mode == "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
