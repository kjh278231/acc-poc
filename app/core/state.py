"""Per-call conversation state shared by both engines."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Action(str, Enum):
    GREET_ASK_ETA = "GREET_ASK_ETA"  # opening line
    ASK_ETA = "ASK_ETA"
    ASK_ISSUE = "ASK_ISSUE"
    ASK_DELAY = "ASK_DELAY"
    CLARIFY = "CLARIFY"              # re-ask the current slot (ambiguous answer)
    ANSWER_OTHER = "ANSWER_OTHER"    # unrelated question: short answer + steer back
    COMPLETE = "COMPLETE"            # all slots collected -> TMS update -> goodbye
    ESCALATE = "ESCALATE"            # connect a human dispatcher
    END_CALL = "END_CALL"            # driver wants to hang up before completion
    FALLBACK = "FALLBACK"            # Jev only: hand the turn to Haiku


class IssueType(str, Enum):
    NONE = "none"
    TRAFFIC = "traffic"
    BREAKDOWN = "breakdown"
    ACCIDENT = "accident"
    WEATHER = "weather"
    OTHER = "other"


SEVERE_ISSUES = {IssueType.BREAKDOWN, IssueType.ACCIDENT}

ISSUE_KO = {
    IssueType.NONE: "특이사항 없음",
    IssueType.TRAFFIC: "교통 체증",
    IssueType.BREAKDOWN: "차량 고장",
    IssueType.ACCIDENT: "사고",
    IssueType.WEATHER: "기상 악화",
    IssueType.OTHER: "기타 특이사항",
}


class Shipment(BaseModel):
    shipment_id: str = "SHP-20261117-001"
    driver_name: str = "김민수"
    destination: str = "이천 물류센터"


class Slots(BaseModel):
    eta_minutes: int | None = None
    issue: IssueType | None = None
    delay_minutes: int | None = None


class Turn(BaseModel):
    role: str  # "agent" | "driver"
    text: str
    action: str | None = None


class CallState(BaseModel):
    session_id: str
    shipment: Shipment = Field(default_factory=Shipment)
    slots: Slots = Field(default_factory=Slots)
    last_action: Action | None = None
    reprompts: dict[str, int] = Field(default_factory=dict)
    off_topic_count: int = 0
    history: list[Turn] = Field(default_factory=list)
    tms_updated: bool = False
    tms_record: dict[str, Any] | None = None
    finished: str | None = None  # COMPLETE | ESCALATE | END_CALL
    finish_reason: str | None = None
    turn_count: int = 0

    def pending_slot(self) -> str | None:
        """Next required slot in order, or None when everything needed is known."""
        s = self.slots
        if s.eta_minutes is None:
            return "eta"
        if s.issue is None:
            return "issue"
        if s.issue != IssueType.NONE and s.delay_minutes is None:
            return "delay"
        return None

    def transcript(self, last_n: int = 12) -> str:
        return "\n".join(f"{'상담원' if t.role == 'agent' else '기사'}: {t.text}" for t in self.history[-last_n:])

    def slots_summary(self) -> dict[str, Any]:
        s = self.slots
        return {
            "eta_minutes": s.eta_minutes,
            "issue": s.issue.value if s.issue else None,
            "delay_minutes": s.delay_minutes,
            "pending": self.pending_slot(),
        }
