"""Offline stand-in for OpenRouter (LLM_MODE=mock). Keyword heuristics, fixed latencies.

Used by tests and for UI/protocol work without an API key. Benchmark results produced with it are labelled
mode=mock and must never be reported as model performance.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.core.decision import ModelCall
from app.services.parser import is_negative_issue, parse_duration

ISSUE_KW = [("accident", ["사고", "접촉"]), ("breakdown", ["고장", "펑크", "엔진", "퍼졌"]),
            ("traffic", ["막혀", "정체", "체증", "밀려"]), ("weather", ["눈", "비가", "안개"]),
            ("other", ["대기", "검문", "휴게"])]


def classify(text: str) -> tuple[str, float, str, float]:
    t = text
    if any(k in t for k in ["담당자", "사람", "상담원 바꿔", "관리자", "연결해"]):
        return "request_human", 0.95, "not_mentioned", 0.9
    if any(k in t for k in ["끊을", "끊어", "나중에", "바빠", "운전 중이라"]):
        return "end_call", 0.92, "not_mentioned", 0.9
    if any(k in t for k in ["날씨", "점심", "몇 시예요", "누구세요", "월급", "?"]) and not parse_duration(t).ok:
        return "off_topic", 0.9, "not_mentioned", 0.9
    issue = next((name for name, kws in ISSUE_KW if any(k in t for k in kws)), None)
    if is_negative_issue(t):
        return "no_issue", 0.93, "none", 0.93
    p = parse_duration(t)
    if p.ok or issue:
        return "provide_info", 0.9, issue or "not_mentioned", 0.9
    if p.vague:
        return "ambiguous", 0.88, "not_mentioned", 0.9
    return "ambiguous", 0.55, "not_mentioned", 0.6


class MockLLM:
    def __init__(self, haiku_latency_s: float = 0.0, jev_latency_s: float = 0.0):
        self.haiku_latency_s = haiku_latency_s
        self.jev_latency_s = jev_latency_s
        self.calls: list[str] = []

    def _call(self, model: str, purpose: str, start_ns: int, t0: float, tin: int, tout: int, cost: float) -> ModelCall:
        lat = (time.perf_counter() - t0) * 1000
        return ModelCall(provider="mock", model=model, purpose=purpose, latency_ms=lat, ttft_ms=lat, input_tokens=tin,
                         output_tokens=tout, cost_usd=cost, start_ns=start_ns, end_ns=time.time_ns())

    async def chat_json(self, messages, schema, *, purpose: str, max_tokens: int | None = None):
        start_ns, t0 = time.time_ns(), time.perf_counter()
        await asyncio.sleep(self.haiku_latency_s)
        self.calls.append(f"haiku:{purpose}")
        user = messages[-1]["content"]
        if purpose == "compose":
            return {"reply": "확인 감사합니다. 처리했습니다. 안전 운행하세요."}, self._call("mock/haiku", purpose, start_ns, t0, 300, 30, 0.00005)
        text = user.split("[기사 마지막 발화]\n", 1)[-1]
        pending = json.loads(user.split("[상태]\n", 1)[1].split("\n\n", 1)[0])["현재까지_확인된_정보"]["pending"]
        intent, _, issue, _ = classify(text)
        p = parse_duration(text)
        out: dict[str, Any] = {"intent": intent, "eta_minutes": None, "issue": None, "delay_minutes": None}
        if issue != "not_mentioned":
            out["issue"] = issue
        if p.ok:
            if pending == "eta":
                out["eta_minutes"] = p.minutes
            else:
                out["delay_minutes"] = p.minutes
        out["action"] = {"request_human": "ESCALATE", "end_call": "END_CALL", "off_topic": "ANSWER_OTHER",
                         "ambiguous": "CLARIFY"}.get(intent, "ASK_ISSUE")
        out["reply"] = "네, 확인했습니다. 다음 내용을 여쭤봐도 될까요?" if intent != "off_topic" else "그건 제가 확인이 어렵습니다. 도착까지 얼마나 걸리세요?"
        return out, self._call("mock/haiku", purpose, start_ns, t0, 900, 80, 0.00013)

    async def systemone(self, state, questions, *, purpose: str = "jev_decide"):
        start_ns, t0 = time.time_ns(), time.perf_counter()
        await asyncio.sleep(self.jev_latency_s)
        self.calls.append("jev")
        intent, ic, issue, jc = classify(state["기사 발화"])
        answers = {
            "intent": {"type": "choice", "choice": intent, "confidence": ic, "probabilities": {intent: ic}},
            "issue": {"type": "choice", "choice": issue, "confidence": jc, "probabilities": {issue: jc}},
        }
        return answers, self._call("mock/jev", purpose, start_ns, t0, 450, 0, 450 * 0.042 / 1e6)
