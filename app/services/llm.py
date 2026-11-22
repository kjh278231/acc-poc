"""OpenRouter client: Haiku (streaming chat completions, JSON output) and Jev (POST /api/v1/systemone).

Both return a ModelCall record with latency, time-to-first-token, tokens and cost. Cost is the provider-reported
`usage.cost` when OpenRouter returns it (cost_actual=True), otherwise an estimate from list prices.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from app.config import Settings
from app.core.decision import ModelCall
from app.core.progress import tracked

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}


class LLMError(Exception):
    def __init__(self, msg: str, call: ModelCall | None = None, status: int | None = None):
        super().__init__(msg)
        self.call = call
        self.status = status


def extract_json(text: str) -> dict[str, Any]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class OpenRouterClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(
            base_url=settings.openrouter_base_url,
            timeout=httpx.Timeout(settings.http_timeout_s, connect=5.0),
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}", "X-Title": "acc-poc"},
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60),
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ Haiku
    def _reasoning(self) -> dict[str, Any]:
        r = self.s.haiku_reasoning
        return {"enabled": False} if r == "none" else {"effort": r}

    async def chat_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], *, purpose: str,
                        max_tokens: int | None = None) -> tuple[dict[str, Any], ModelCall]:
        body = {
            "model": self.s.haiku_model,
            "messages": messages,
            "max_tokens": max_tokens or self.s.haiku_max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "usage": {"include": True},
            "reasoning": self._reasoning(),
            "response_format": {"type": "json_schema", "json_schema": {"name": "turn", "strict": True, "schema": schema}},
        }
        text, call = await tracked(self.s.haiku_model, purpose, self._stream_chat(body, purpose))
        try:
            return extract_json(text), call
        except (json.JSONDecodeError, ValueError) as e:
            call.ok, call.error = False, f"bad_json: {text[:120]!r}"
            raise LLMError(f"Haiku returned non-JSON: {e}", call) from e

    async def _stream_chat(self, body: dict[str, Any], purpose: str) -> tuple[str, ModelCall]:
        attempt = 0
        while True:
            start_ns, t0 = time.time_ns(), time.perf_counter()
            ttft = None
            parts: list[str] = []
            usage: dict[str, Any] = {}
            try:
                async with self.http.stream("POST", "/v1/chat/completions", json=body) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode(errors="replace")[:300]
                        raise LLMError(f"Haiku HTTP {r.status_code}: {detail}", status=r.status_code)
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        ev = json.loads(data)
                        if ev.get("error"):
                            raise LLMError(f"Haiku stream error: {ev['error']}")
                        for ch in ev.get("choices") or []:
                            piece = (ch.get("delta") or {}).get("content")
                            if piece:
                                if ttft is None:
                                    ttft = (time.perf_counter() - t0) * 1000
                                parts.append(piece)
                        if ev.get("usage"):
                            usage = ev["usage"]
                break
            except (LLMError, httpx.TransportError) as e:
                status = getattr(e, "status", None)
                retryable = isinstance(e, httpx.TransportError) or status in RETRY_STATUS
                if retryable and attempt < self.s.http_max_retries:
                    attempt += 1
                    await asyncio.sleep(0.4 * 2 ** attempt)
                    continue
                call = ModelCall(provider="openrouter", model=self.s.haiku_model, purpose=purpose, ok=False,
                                 error=str(e)[:300], latency_ms=(time.perf_counter() - t0) * 1000, retries=attempt,
                                 start_ns=start_ns, end_ns=time.time_ns())
                raise LLMError(str(e), call, status) from e
        latency = (time.perf_counter() - t0) * 1000
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        actual = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        if not actual:
            cost = (in_tok * self.s.haiku_price_in + out_tok * self.s.haiku_price_out) / 1e6
        call = ModelCall(provider="openrouter", model=self.s.haiku_model, purpose=purpose, latency_ms=latency, ttft_ms=ttft,
                         input_tokens=in_tok, output_tokens=out_tok, cost_usd=float(cost), cost_actual=actual,
                         retries=attempt, start_ns=start_ns, end_ns=time.time_ns())
        return "".join(parts), call

    # ------------------------------------------------------------------ Jev
    async def systemone(self, state: Any, questions: dict[str, Any], *, purpose: str = "jev_decide") -> tuple[dict[str, Any], ModelCall]:
        models = [self.s.jev_model] + ([self.s.jev_fallback_model] if self.s.jev_fallback_model else [])
        last: Exception | None = None
        for model in models:
            try:
                return await tracked(model, purpose, self._systemone(model, state, questions, purpose))
            except LLMError as e:
                last = e
                if e.status not in (400, 404):  # only an unknown/unsupported model id moves to the fallback model
                    raise
        assert last is not None
        raise last

    async def _systemone(self, model: str, state: Any, questions: dict[str, Any], purpose: str) -> tuple[dict[str, Any], ModelCall]:
        body = {"model": model, "state": state, "questions": questions}
        attempt = 0
        while True:
            start_ns, t0 = time.time_ns(), time.perf_counter()
            try:
                r = await self.http.post("/v1/systemone", json=body)
                if r.status_code in RETRY_STATUS and attempt < self.s.http_max_retries:
                    attempt += 1
                    await asyncio.sleep(0.4 * 2 ** attempt)
                    continue
                if r.status_code != 200:
                    raise LLMError(f"Jev HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
                data = r.json()
                break
            except httpx.TransportError as e:
                if attempt < self.s.http_max_retries:
                    attempt += 1
                    await asyncio.sleep(0.4 * 2 ** attempt)
                    continue
                raise LLMError(f"Jev transport error: {e}") from e
            except LLMError as e:
                e.call = ModelCall(provider="openrouter", model=model, purpose=purpose, ok=False, error=str(e)[:300],
                                   latency_ms=(time.perf_counter() - t0) * 1000, retries=attempt,
                                   start_ns=start_ns, end_ns=time.time_ns())
                raise
        latency = (time.perf_counter() - t0) * 1000
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise LLMError(f"Jev response without answers: {str(data)[:200]}")
        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        cost = usage.get("cost")
        actual = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        if not actual:
            cost = in_tok * self.s.jev_price_in / 1e6
        call = ModelCall(provider="openrouter", model=str(data.get("model") or model), purpose=purpose, latency_ms=latency,
                         ttft_ms=latency, input_tokens=in_tok, output_tokens=int(usage.get("output_tokens") or 0),
                         cost_usd=float(cost), cost_actual=actual, retries=attempt, start_ns=start_ns, end_ns=time.time_ns())
        return answers, call
