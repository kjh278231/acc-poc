"""Engine output types and the per-call usage record."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.state import Action, IssueType

INTENTS = ["provide_info", "no_issue", "ambiguous", "request_human", "end_call", "off_topic"]


class ModelCall(BaseModel):
    provider: str              # openrouter | mock
    model: str
    purpose: str               # decide | compose | jev_decide
    latency_ms: float
    ttft_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_actual: bool = False  # True = provider-reported usage.cost; False = list-price estimate
    ok: bool = True
    error: str | None = None
    retries: int = 0
    start_ns: int = 0
    end_ns: int = 0


class Decision(BaseModel):
    intent: str | None = None
    action: Action
    eta_minutes: int | None = None
    issue: IssueType | None = None
    delay_minutes: int | None = None
    reply: str | None = None          # generated reply (Haiku); None -> template
    confidence: float | None = None
    source: str = ""                  # haiku | jev | jev>haiku
    fallback_reason: str | None = None
    calls: list[ModelCall] = Field(default_factory=list)
    decision_ms: float = 0.0
    ttft_ms: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
