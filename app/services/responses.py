"""Predefined response templates (shared repository for both architectures)."""
from __future__ import annotations

from app.core.state import ISSUE_KO, Action, CallState, IssueType


def fmt_minutes(m: int | None) -> str:
    if m is None:
        return "미확인"
    if m == 0:
        return "도착"
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h}시간 {mm}분"
    if h:
        return f"{h}시간"
    return f"{mm}분"


CLARIFY = {
    "eta": "죄송하지만 대략적인 시간으로 말씀해 주시겠어요? 예를 들어 30분, 1시간처럼요.",
    "issue": "운행 중 교통 체증이나 차량 문제 같은 특이사항이 있으신지 다시 한번 말씀해 주시겠어요?",
    "delay": "그로 인한 지연 시간은 대략 몇 분 정도로 보면 될까요?",
    None: "다시 한번 말씀해 주시겠어요?",
}

ASK = {
    "eta": "목적지까지 앞으로 얼마나 걸릴 것 같으세요?",
    "issue": "운행 중 특이사항은 없으신가요?",
    "delay": "그로 인해 예상되는 지연 시간은 어느 정도인가요?",
}


def ack(state: CallState, filled: set[str]) -> str:
    s = state.slots
    parts = []
    if "eta" in filled and s.eta_minutes is not None:
        parts.append("도착하셨다고 확인했습니다." if s.eta_minutes == 0 else f"네, {fmt_minutes(s.eta_minutes)} 후 도착 예정으로 확인했습니다.")
    if "issue" in filled and s.issue is not None:
        if s.issue == IssueType.NONE:
            parts.append("특이사항 없으신 것으로 확인했습니다.")
        else:
            parts.append(f"{ISSUE_KO[s.issue]} 상황이시군요.")
    if "delay" in filled and s.delay_minutes is not None:
        parts.append(f"지연 시간 {fmt_minutes(s.delay_minutes)}으로 확인했습니다.")
    return " ".join(parts)


def render(action: Action, state: CallState, filled: set[str] | None = None, reason: str | None = None) -> str:
    filled = filled or set()
    s, sh = state.slots, state.shipment
    prefix = ack(state, filled)
    if action == Action.GREET_ASK_ETA:
        return (f"안녕하세요, {sh.driver_name} 기사님. {sh.destination}행 운송 건으로 연락드렸습니다. "
                "목적지까지 도착 예정 시간이 얼마나 될까요?")
    if action == Action.ASK_ETA:
        body = ASK["eta"]
    elif action == Action.ASK_ISSUE:
        body = ASK["issue"]
    elif action == Action.ASK_DELAY:
        body = ASK["delay"]
    elif action == Action.CLARIFY:
        body = CLARIFY.get(state.pending_slot(), CLARIFY[None])
    elif action == Action.ANSWER_OTHER:
        pending = state.pending_slot()
        body = "그 부분은 담당자가 따로 안내드리겠습니다. " + (ASK[pending] if pending else "")
    elif action == Action.COMPLETE:
        issue = ISSUE_KO[s.issue] if s.issue else "특이사항 없음"
        delay = f", 지연 {fmt_minutes(s.delay_minutes)}" if s.delay_minutes else ""
        body = f"도착 예정 {fmt_minutes(s.eta_minutes)}, {issue}{delay}으로 시스템에 등록하겠습니다. 감사합니다, 안전 운행하세요."
    elif action == Action.ESCALATE:
        body = "담당자에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요."
        if reason == "severe_issue":
            body = "안전이 가장 중요합니다. 확인된 내용은 등록하고, 담당자에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요."
    elif action == Action.END_CALL:
        body = "알겠습니다. 확인된 내용까지만 등록해 두겠습니다. 안전 운행하세요."
    else:
        body = CLARIFY[None]
    return f"{prefix} {body}".strip()


TMS_FAILED = "다만 지금 시스템 등록이 지연되고 있어 담당자가 다시 확인드리겠습니다."
