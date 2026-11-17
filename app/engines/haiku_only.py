"""Architecture A: Haiku interprets every turn, picks the action and writes the reply.

Driver text -> Haiku (intent + slots + action + reply, one streamed JSON call)
            -> rule engine validates
            -> tool (TMS) runs -> Haiku writes the final reply with the tool result (agentic tool loop)
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.core.decision import INTENTS, Decision, ModelCall
from app.core.state import Action, CallState, IssueType
from app.services.llm import LLMError

ACTIONS = ["ASK_ETA", "ASK_ISSUE", "ASK_DELAY", "CLARIFY", "ANSWER_OTHER", "COMPLETE", "ESCALATE", "END_CALL"]
ISSUES = [i.value for i in IssueType]

SYSTEM = """당신은 물류 운송관리센터의 AI 상담원입니다. 운송 중인 화물 기사에게 전화를 걸어 다음을 순서대로 확인합니다.
1) 목적지까지 예상 소요시간(ETA, 분 단위)
2) 운송 중 특이사항 유무와 유형: none(없음), traffic(교통 체증), breakdown(차량 고장), accident(사고), weather(기상 악화), other(기타)
3) 특이사항이 있으면 그로 인한 예상 지연시간(분 단위)
모두 확인되면 TMS에 등록하고 정중히 통화를 마칩니다.

매 턴 기사의 마지막 발화를 해석해 JSON 하나로만 답합니다.
- intent: provide_info(물은 정보를 답함) | no_issue(특이사항 없다고 답함) | ambiguous(답하려 하나 시간·내용이 불명확) | request_human(사람 담당자 연결 요청) | end_call(통화를 끝내려 함) | off_topic(업무와 무관한 말·질문)
- eta_minutes / delay_minutes: 이번 발화에서 새로 말한 시간을 지금부터의 분으로 환산 (예: 한 시간 반 -> 90). 범위는 큰 값. 말하지 않았거나 불명확하면 null. 추측 금지.
- issue: 이번 발화에서 확인된 특이사항 유형, 언급 없으면 null. 특이사항 없다고 하면 "none".
- action: 다음 행동. ASK_ETA | ASK_ISSUE | ASK_DELAY | CLARIFY(불명확해서 같은 항목 재질문) | ANSWER_OTHER(무관한 질문에 짧게 답하고 원래 질문으로 복귀) | COMPLETE(필요 정보 모두 확보) | ESCALATE(담당자 연결 요청, 사고·고장 등 사람 판단 필요) | END_CALL(기사가 통화 종료 원함)
- reply: 기사에게 할 다음 말. 전화 음성으로 읽힐 존댓말 1~2문장, 60자 내외. 확인한 값을 짧게 복창하고 다음 질문을 한다. 시스템에 등록했다고 미리 말하지 않는다.
"""

COMPOSE_SYSTEM = """당신은 물류 운송관리센터의 AI 상담원입니다. 방금 도구(TMS 등록/담당자 연결)를 실행했습니다.
도구 결과를 반영해 통화를 마무리하는 말을 전화 음성용 존댓말 1~2문장(60자 내외)으로 JSON {"reply": "..."} 로만 답하세요.
도구가 실패했다면 등록했다고 말하지 말고 담당자가 다시 확인할 것이라고 안내하세요."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "eta_minutes", "issue", "delay_minutes", "action", "reply"],
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "eta_minutes": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "issue": {"anyOf": [{"type": "string", "enum": ISSUES}, {"type": "null"}]},
        "delay_minutes": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "action": {"type": "string", "enum": ACTIONS},
        "reply": {"type": "string"},
    },
}
COMPOSE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply"],
                  "properties": {"reply": {"type": "string"}}}


def user_prompt(state: CallState, text: str) -> str:
    ctx = {
        "배송": state.shipment.model_dump(),
        "현재까지_확인된_정보": state.slots_summary(),
        "상담원_직전_행동": state.last_action.value if state.last_action else None,
    }
    return (f"[상태]\n{json.dumps(ctx, ensure_ascii=False)}\n\n[최근 대화]\n{state.transcript()}\n\n"
            f"[기사 마지막 발화]\n{text}")


class HaikuOnlyEngine:
    name = "haiku_only"
    parallel_tools = False

    def __init__(self, llm):
        self.llm = llm

    async def decide(self, state: CallState, text: str, *, purpose: str = "decide") -> Decision:
        t0 = time.perf_counter()
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt(state, text)}]
        out, call = await self.llm.chat_json(messages, SCHEMA, purpose=purpose)
        try:
            action = Action(out.get("action"))
        except ValueError:
            action = Action.CLARIFY
        issue = out.get("issue")
        d = Decision(
            intent=out.get("intent") if out.get("intent") in INTENTS else None,
            action=action,
            eta_minutes=out.get("eta_minutes"),
            issue=IssueType(issue) if issue in ISSUES else None,
            delay_minutes=out.get("delay_minutes"),
            reply=(out.get("reply") or "").strip() or None,
            source="haiku",
            calls=[call],
            ttft_ms=call.ttft_ms,
            raw={"haiku": out},
        )
        if d.intent == "request_human":
            d.raw["escalate_reason"] = "driver_request"
        d.decision_ms = (time.perf_counter() - t0) * 1000
        return d

    async def compose_after_tool(self, state: CallState, action: Action, tool_result: dict[str, Any],
                                 draft: str | None) -> tuple[str | None, list[ModelCall]]:
        ctx = {"행동": action.value, "확인된_정보": state.slots_summary(), "도구_결과": tool_result, "초안": draft}
        messages = [{"role": "system", "content": COMPOSE_SYSTEM},
                    {"role": "user", "content": json.dumps(ctx, ensure_ascii=False, default=str)}]
        try:
            out, call = await self.llm.chat_json(messages, COMPOSE_SCHEMA, purpose="compose", max_tokens=200)
        except LLMError as e:
            return None, [e.call] if e.call else []
        return (out.get("reply") or "").strip() or None, [call]
