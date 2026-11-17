"""Architecture B: Jev classifies the turn, the common parser extracts numbers, templates answer.

Driver text -> Jev (intent + issue type, with probabilities) + parser (minutes)
            -> confident & simple: Decision with template reply (no generation)
            -> low confidence / off-topic / several time expressions / Jev error: fall back to Haiku for this turn
Tools run in parallel with the spoken reply (parallel_tools=True).
"""
from __future__ import annotations

import time
from typing import Any

from app.core.decision import Decision, ModelCall
from app.core.state import SEVERE_ISSUES, Action, CallState, IssueType
from app.engines.haiku_only import HaikuOnlyEngine
from app.services.llm import LLMError
from app.services.parser import count_time_mentions, parse_duration

INSTRUCTIONS = ("화물 기사와 운송관리센터 AI 상담원의 전화 통화입니다. 상담원의 직전 질문과 수집 상태를 고려해 "
                "'기사 발화'만 판정하세요. 발화는 음성 인식 결과라 오탈자가 있을 수 있습니다.")

QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": INSTRUCTIONS + " 기사 발화의 의도는?",
        "criteria": {
            "provide_info": "상담원이 물은 정보(도착 예정 시간, 특이사항 내용, 지연 시간)를 구체적으로 답함",
            "no_issue": "운행 중 특이사항이나 문제가 없다고 답함",
            "ambiguous": "답하려 하지만 시간이나 내용이 불명확함 (예: 곧 가요, 잘 모르겠어요, 가봐야 알아요)",
            "request_human": "AI 말고 사람 담당자, 관리자, 배차 담당과 통화하고 싶어 함",
            "end_call": "바쁘거나 운전 중이라 통화를 끝내고 싶어 함",
            "off_topic": "업무와 무관한 말을 하거나 상담원에게 다른 질문을 함",
        },
    },
    "issue": {
        "type": "choice",
        "instructions": INSTRUCTIONS + " 기사 발화에서 확인되는 운송 중 특이사항 유형은?",
        "criteria": {
            "not_mentioned": "이 발화에는 특이사항에 대한 내용이 없음",
            "none": "특이사항 없음, 정상 운행 중",
            "traffic": "교통 체증, 정체, 길이 막힘",
            "breakdown": "차량 고장, 타이어 펑크, 엔진 문제",
            "accident": "교통사고, 접촉 사고, 사고 처리 중",
            "weather": "눈, 비, 안개 등 기상 악화",
            "other": "그 밖의 지연 사유 (상하차 대기, 검문, 휴게 등)",
        },
    },
}


def jev_state(state: CallState, text: str) -> dict[str, Any]:
    last_q = next((t.text for t in reversed(state.history) if t.role == "agent"), None)
    return {
        "상담원 직전 질문": last_q,
        "현재 수집 상태": state.slots_summary(),
        "기사 발화": text,
    }


def next_action(state: CallState, d: Decision) -> Action:
    """Next ask after applying this turn's values (same order the rule engine enforces)."""
    eta = d.eta_minutes if d.eta_minutes is not None else state.slots.eta_minutes
    issue = d.issue if d.issue is not None else state.slots.issue
    delay = d.delay_minutes if d.delay_minutes is not None else state.slots.delay_minutes
    if eta is None:
        return Action.ASK_ETA
    if issue is None:
        return Action.ASK_ISSUE
    if issue != IssueType.NONE and delay is None:
        return Action.ASK_DELAY
    return Action.ESCALATE if issue in SEVERE_ISSUES else Action.COMPLETE


class JevHaikuEngine:
    name = "jev_haiku"
    parallel_tools = True

    def __init__(self, llm, haiku: HaikuOnlyEngine, threshold: float = 0.8):
        self.llm = llm
        self.haiku = haiku
        self.threshold = threshold

    async def decide(self, state: CallState, text: str) -> Decision:
        t0 = time.perf_counter()
        try:
            answers, call = await self.llm.systemone(jev_state(state, text), QUESTIONS)
        except LLMError as e:
            return await self._fallback(state, text, t0, "jev_error", [e.call] if e.call else [], {"error": str(e)[:200]})
        d = self.map_answers(state, text, answers)
        d.calls = [call]
        d.ttft_ms = call.latency_ms
        if d.action == Action.FALLBACK:
            return await self._fallback(state, text, t0, d.fallback_reason or "complex", [call], d.raw)
        d.decision_ms = (time.perf_counter() - t0) * 1000
        return d

    def map_answers(self, state: CallState, text: str, answers: dict[str, Any]) -> Decision:
        """Pure mapping from Jev probabilities + parser output to a proposed action (unit-testable)."""
        ia, ja = answers.get("intent") or {}, answers.get("issue") or {}
        intent, iconf = ia.get("choice"), float(ia.get("confidence") or 0.0)
        issue_label, jconf = ja.get("choice"), float(ja.get("confidence") or 0.0)
        parsed = parse_duration(text)
        mentions = count_time_mentions(text)
        pending = state.pending_slot()
        raw = {"jev": {"intent": [intent, round(iconf, 3)], "issue": [issue_label, round(jconf, 3)]},
               "parsed_minutes": parsed.minutes, "time_mentions": mentions}
        d = Decision(intent=intent, action=Action.FALLBACK, confidence=iconf, source="jev", raw=raw)

        def fb(reason: str) -> Decision:
            d.action, d.fallback_reason = Action.FALLBACK, reason
            return d

        if iconf < self.threshold:
            return fb("low_confidence_intent")
        if intent == "request_human":
            d.action = Action.ESCALATE
            d.raw["escalate_reason"] = "driver_request"
            return d
        if intent == "end_call":
            d.action = Action.END_CALL
            return d
        if intent == "off_topic":
            return fb("off_topic")  # needs a generated answer
        if mentions > 1:
            return fb("multiple_time_expressions")

        issue: IssueType | None = None
        if issue_label and issue_label != "not_mentioned":
            if jconf < self.threshold:
                return fb("low_confidence_issue")
            issue = IssueType(issue_label)
        if intent == "no_issue":
            issue = IssueType.NONE
        d.issue = issue

        if parsed.ok:
            target = pending
            if pending == "issue" and issue not in (None, IssueType.NONE):
                target = "delay"  # "사고가 나서 30분 늦어요" answers issue and delay together
            if target == "eta":
                d.eta_minutes = parsed.minutes
            elif target == "delay":
                d.delay_minutes = parsed.minutes
            elif target is None:
                return fb("number_without_pending_slot")
        if intent == "ambiguous" and not parsed.ok:
            d.action = Action.CLARIFY
            return d
        if intent in ("provide_info", "no_issue", "ambiguous"):
            progressed = d.eta_minutes is not None or d.delay_minutes is not None or d.issue is not None
            d.action = next_action(state, d) if progressed else Action.CLARIFY
            if d.action == Action.ESCALATE:
                d.raw["escalate_reason"] = "severe_issue"
            return d
        return fb("unmapped_intent")

    async def _fallback(self, state: CallState, text: str, t0: float, reason: str, calls: list[ModelCall],
                        raw: dict[str, Any]) -> Decision:
        try:
            d = await self.haiku.decide(state, text, purpose="fallback")
        except LLMError as e:
            d = Decision(action=Action.CLARIFY, source="jev>template", raw={"haiku_error": str(e)[:200]})
            if e.call:
                d.calls = [e.call]
        d.calls = calls + d.calls
        d.source = "jev>haiku" if d.source == "haiku" else d.source
        d.fallback_reason = reason
        d.raw = {**raw, **d.raw}
        d.ttft_ms = (calls[0].latency_ms if calls else 0) + (d.ttft_ms or 0)
        d.decision_ms = (time.perf_counter() - t0) * 1000
        return d

    async def compose_after_tool(self, state: CallState, action: Action, tool_result: dict[str, Any],
                                 draft: str | None) -> tuple[str | None, list[ModelCall]]:
        return None, []
