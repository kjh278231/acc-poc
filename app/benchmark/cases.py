"""Benchmark data.

DECISION_CASES — single-turn judgment from a fixed state (pure model/decision benchmark).
SCENARIOS      — whole calls with a reactive simulated driver (architecture benchmark).

Utterances avoid wall-clock times ("4시 반") so expected minutes do not depend on when the benchmark runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.state import Action, CallState, IssueType

PRESETS: dict[str, dict[str, Any]] = {
    "start": {"slots": {}, "last": Action.GREET_ASK_ETA,
              "history": [("agent", "안녕하세요, 김민수 기사님. 이천 물류센터행 운송 건으로 연락드렸습니다. 목적지까지 도착 예정 시간이 얼마나 될까요?")]},
    "eta_known": {"slots": {"eta_minutes": 40}, "last": Action.ASK_ISSUE,
                  "history": [("agent", "목적지까지 도착 예정 시간이 얼마나 될까요?"), ("driver", "40분 정도요"),
                              ("agent", "네, 40분 후 도착 예정으로 확인했습니다. 운행 중 특이사항은 없으신가요?")]},
    "traffic": {"slots": {"eta_minutes": 50, "issue": IssueType.TRAFFIC}, "last": Action.ASK_DELAY,
                "history": [("agent", "운행 중 특이사항은 없으신가요?"), ("driver", "길이 좀 막혀요"),
                            ("agent", "교통 체증 상황이시군요. 그로 인해 예상되는 지연 시간은 어느 정도인가요?")]},
    "breakdown": {"slots": {"eta_minutes": 60, "issue": IssueType.BREAKDOWN}, "last": Action.ASK_DELAY,
                  "history": [("agent", "운행 중 특이사항은 없으신가요?"), ("driver", "차가 고장 났어요"),
                              ("agent", "차량 고장 상황이시군요. 그로 인해 예상되는 지연 시간은 어느 정도인가요?")]},
}


def make_state(preset: str, session_id: str = "bench") -> CallState:
    from app.core.state import Turn
    p = PRESETS[preset]
    st = CallState(session_id=session_id)
    for k, v in p["slots"].items():
        setattr(st.slots, k, v)
    st.last_action = p["last"]
    st.history = [Turn(role=r, text=t) for r, t in p["history"]]
    return st


@dataclass
class DecisionCase:
    id: str
    category: str
    preset: str
    text: str
    intents: set[str]                 # accepted intent labels
    actions: set[str]                 # accepted final actions (after the rule engine)
    slots: dict[str, Any] = field(default_factory=dict)  # expected slot values after the turn


def _d(id, cat, preset, text, intents, actions, **slots) -> DecisionCase:
    if isinstance(intents, str):
        intents = {intents}
    if isinstance(actions, str):
        actions = {actions}
    return DecisionCase(id, cat, preset, text, set(intents), set(actions), slots)


DECISION_CASES: list[DecisionCase] = [
    _d("d01", "normal", "start", "한 40분 정도 걸릴 것 같아요", "provide_info", "ASK_ISSUE", eta_minutes=40),
    _d("d02", "normal", "start", "한 시간 반이면 도착합니다", "provide_info", "ASK_ISSUE", eta_minutes=90),
    _d("d03", "normal", "start", "1시간 정도 남았고 특이사항은 없습니다", {"provide_info", "no_issue"}, "COMPLETE", eta_minutes=60, issue="none"),
    _d("d04", "normal", "start", "20분에서 30분 사이요", "provide_info", "ASK_ISSUE", eta_minutes=30),
    _d("d05", "eta_unclear", "start", "글쎄요, 가봐야 알 것 같은데요", "ambiguous", "CLARIFY", eta_minutes=None),
    _d("d06", "eta_unclear", "start", "곧 도착해요", "ambiguous", "CLARIFY", eta_minutes=None),
    _d("d07", "off_topic", "start", "오늘 날씨 진짜 춥네요, 거기도 추워요?", "off_topic", "ANSWER_OTHER"),
    _d("d08", "human", "start", "AI 말고 사람 담당자랑 얘기할게요", "request_human", "ESCALATE"),
    _d("d09", "end_call", "start", "지금 운전 중이라 통화 어려워요, 끊을게요", "end_call", "END_CALL"),
    _d("d10", "normal", "start", "두 시간 좀 넘게 걸릴 것 같아요", "provide_info", "ASK_ISSUE", eta_minutes=120),
    _d("d11", "normal", "start", "삼십 분이면 충분해요", "provide_info", "ASK_ISSUE", eta_minutes=30),
    _d("d12", "complex", "start", "도착까지 40분이고요, 상차 대기 때문에 이미 15분 정도 늦어졌어요", "provide_info", "COMPLETE",
       eta_minutes=40, issue="other", delay_minutes=15),
    _d("d13", "ambiguous", "start", "여보세요? 잘 안 들려요", {"ambiguous", "off_topic"}, {"CLARIFY", "ANSWER_OTHER"}, eta_minutes=None),
    _d("d14", "normal", "eta_known", "아니요 특별한 건 없어요", "no_issue", "COMPLETE", issue="none"),
    _d("d15", "traffic", "eta_known", "고속도로가 많이 막히네요", "provide_info", "ASK_DELAY", issue="traffic"),
    _d("d16", "traffic", "eta_known", "공사 구간 정체로 30분 정도 지연될 것 같아요", "provide_info", "COMPLETE", issue="traffic", delay_minutes=30),
    _d("d17", "breakdown", "eta_known", "타이어가 펑크 나서 갓길에 서 있어요", "provide_info", "ASK_DELAY", issue="breakdown"),
    _d("d18", "accident", "eta_known", "접촉 사고가 나서 지금 사고 처리 중이에요", "provide_info", "ASK_DELAY", issue="accident"),
    _d("d19", "weather", "eta_known", "눈이 많이 와서 천천히 가고 있어요", "provide_info", "ASK_DELAY", issue="weather"),
    _d("d20", "ambiguous", "eta_known", "뭐 그냥 그렇죠", "ambiguous", "CLARIFY", issue=None),
    _d("d21", "off_topic", "eta_known", "근데 이번 달 운임 정산은 언제 되나요?", "off_topic", "ANSWER_OTHER"),
    _d("d22", "human", "eta_known", "배차 담당자 바꿔 주세요", "request_human", "ESCALATE"),
    _d("d23", "end_call", "eta_known", "바빠서 이만 끊겠습니다", "end_call", "END_CALL"),
    _d("d24", "normal", "eta_known", "순조롭게 잘 가고 있습니다", "no_issue", "COMPLETE", issue="none"),
    _d("d25", "traffic", "traffic", "한 20분 정도 늦어질 것 같아요", "provide_info", "COMPLETE", delay_minutes=20),
    _d("d26", "ambiguous", "traffic", "얼마나 늦어질지는 잘 모르겠네요", "ambiguous", "CLARIFY", delay_minutes=None),
    _d("d27", "traffic", "traffic", "한 시간은 더 걸릴 것 같아요", "provide_info", "COMPLETE", delay_minutes=60),
    _d("d28", "breakdown", "breakdown", "수리하는 데 한 40분은 걸릴 것 같아요", "provide_info", "ESCALATE", delay_minutes=40),
    _d("d29", "breakdown", "breakdown", "견인차 기다리는 중이라 모르겠어요", "ambiguous", "ESCALATE"),
    _d("d30", "normal", "start", "네 네, 30분", "provide_info", "ASK_ISSUE", eta_minutes=30),
]


# ---------------------------------------------------------------------- conversations
U = tuple[str, str]  # (utterance, expected intent)


@dataclass
class Scenario:
    id: str
    category: str
    description: str
    replies: dict[str, list[U]]           # agent action -> driver replies (consumed in order, last one repeats)
    expect_outcome: str                   # COMPLETE | ESCALATE | END_CALL
    expect_slots: dict[str, Any]          # values recorded in TMS
    max_turns: int = 6
    tms_fail: int = 0                     # inject N TMS failures


def _s(id, cat, desc, outcome, slots, max_turns=6, tms_fail=0, **replies) -> Scenario:
    return Scenario(id, cat, desc, {k: v for k, v in replies.items()}, outcome, slots, max_turns, tms_fail)


NONE_ISSUE = [("아니요 특별한 건 없어요", "no_issue")]

SCENARIOS: list[Scenario] = [
    _s("c01", "normal", "정상 운송", "COMPLETE", {"eta_minutes": 40, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("한 40분 정도 걸릴 것 같아요", "provide_info")], ASK_ISSUE=NONE_ISSUE),
    _s("c02", "normal", "정상 운송 (시간 반)", "COMPLETE", {"eta_minutes": 90, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("한 시간 반이면 도착합니다", "provide_info")], ASK_ISSUE=[("네 문제 없어요", "no_issue")]),
    _s("c03", "normal", "한 번에 ETA+특이사항 답변", "COMPLETE", {"eta_minutes": 60, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("1시간 정도 남았고 특이사항은 없습니다", "provide_info")], ASK_ISSUE=NONE_ISSUE),
    _s("c04", "traffic", "교통 체증 → 지연시간", "COMPLETE", {"eta_minutes": 50, "issue": "traffic", "delay_minutes": 20},
       ASK_ETA=[("50분이요", "provide_info")], ASK_ISSUE=[("고속도로가 많이 막히네요", "provide_info")],
       ASK_DELAY=[("한 20분 정도 늦어질 것 같아요", "provide_info")]),
    _s("c05", "traffic", "정체 + 지연시간 한 번에", "COMPLETE", {"eta_minutes": 120, "issue": "traffic", "delay_minutes": 30},
       ASK_ETA=[("두 시간이요", "provide_info")], ASK_ISSUE=[("공사 구간 정체로 30분 정도 지연될 것 같아요", "provide_info")],
       ASK_DELAY=[("30분이요", "provide_info")]),
    _s("c06", "breakdown", "차량 고장 → 담당자 연결", "ESCALATE", {"eta_minutes": 60, "issue": "breakdown", "delay_minutes": 40},
       ASK_ETA=[("한 시간쯤요", "provide_info")], ASK_ISSUE=[("타이어가 펑크 나서 갓길에 서 있어요", "provide_info")],
       ASK_DELAY=[("수리하는 데 한 40분은 걸릴 것 같아요", "provide_info")]),
    _s("c07", "accident", "사고, 지연 불명 → 담당자 연결", "ESCALATE", {"eta_minutes": 30, "issue": "accident", "delay_minutes": None},
       ASK_ETA=[("30분 남았는데요", "provide_info")], ASK_ISSUE=[("접촉 사고가 나서 지금 사고 처리 중이에요", "provide_info")],
       ASK_DELAY=[("얼마나 걸릴지 모르겠어요", "ambiguous")]),
    _s("c08", "eta_unclear", "ETA 불명확 → 재질문 → 답변", "COMPLETE", {"eta_minutes": 60, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("글쎄요, 가봐야 알 것 같은데요", "ambiguous")], CLARIFY=[("음 대충 한 시간 정도요", "provide_info")],
       ASK_ISSUE=[("없습니다", "no_issue")]),
    _s("c09", "eta_unclear", "모호 2회 후 답변", "COMPLETE", {"eta_minutes": 20, "issue": "none", "delay_minutes": None}, 7,
       ASK_ETA=[("곧 도착해요", "ambiguous")], CLARIFY=[("금방이에요", "ambiguous"), ("20분이면 될 거예요", "provide_info")],
       ASK_ISSUE=[("없어요", "no_issue")]),
    _s("c10", "eta_unclear", "끝내 ETA 불명 → 담당자", "ESCALATE", {"eta_minutes": None, "issue": None, "delay_minutes": None}, 6,
       ASK_ETA=[("모르겠어요", "ambiguous")], CLARIFY=[("잘 모르겠네요", "ambiguous"), ("저도 모르죠", "ambiguous")]),
    _s("c11", "ambiguous", "특이사항 답변 모호 → 재질문", "COMPLETE", {"eta_minutes": 40, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("40분이요", "provide_info")], ASK_ISSUE=[("뭐 그냥 그렇죠", "ambiguous")],
       CLARIFY=[("아 네 특별한 문제는 없어요", "no_issue")]),
    _s("c12", "off_topic", "무관한 잡담 후 답변", "COMPLETE", {"eta_minutes": 30, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("오늘 날씨 진짜 춥네요, 거기도 추워요?", "off_topic"), ("아 네, 한 30분 걸려요", "provide_info")],
       ANSWER_OTHER=[("아 네, 한 30분 걸려요", "provide_info")], ASK_ISSUE=[("없어요", "no_issue")]),
    _s("c13", "off_topic", "업무 외 질문 후 답변", "COMPLETE", {"eta_minutes": 45, "issue": "none", "delay_minutes": None},
       ASK_ETA=[("사십오 분이요", "provide_info")], ASK_ISSUE=[("근데 이번 달 운임 정산은 언제 되나요?", "off_topic")],
       ANSWER_OTHER=[("아 네 특이사항은 없어요", "no_issue")], CLARIFY=[("특이사항 없어요", "no_issue")]),
    _s("c14", "human", "처음부터 담당자 요청", "ESCALATE", {"eta_minutes": None, "issue": None, "delay_minutes": None},
       ASK_ETA=[("AI 말고 사람 담당자랑 얘기할게요", "request_human")]),
    _s("c15", "human", "중간에 담당자 요청", "ESCALATE", {"eta_minutes": 40, "issue": None, "delay_minutes": None},
       ASK_ETA=[("40분이요", "provide_info")], ASK_ISSUE=[("배차 담당자 바꿔 주세요", "request_human")]),
    _s("c16", "end_call", "즉시 통화 종료 요청", "END_CALL", {"eta_minutes": None, "issue": None, "delay_minutes": None},
       ASK_ETA=[("지금 운전 중이라 통화 어려워요, 끊을게요", "end_call")]),
    _s("c17", "end_call", "ETA 후 종료 요청", "END_CALL", {"eta_minutes": 60, "issue": None, "delay_minutes": None},
       ASK_ETA=[("1시간이요", "provide_info")], ASK_ISSUE=[("바빠서 이만 끊겠습니다", "end_call")]),
    _s("c18", "weather", "기상 악화 지연", "COMPLETE", {"eta_minutes": 80, "issue": "weather", "delay_minutes": 30},
       ASK_ETA=[("한 시간 이십 분요", "provide_info")], ASK_ISSUE=[("눈이 많이 와서 천천히 가고 있어요", "provide_info")],
       ASK_DELAY=[("30분 정도 늦을 것 같아요", "provide_info")]),
    _s("c19", "complex", "ETA·사유·지연 한 문장", "COMPLETE", {"eta_minutes": 40, "issue": "other", "delay_minutes": 15},
       ASK_ETA=[("도착까지 40분이고요, 상차 대기 때문에 이미 15분 정도 늦어졌어요", "provide_info")],
       ASK_ISSUE=[("상차 대기 때문이에요", "provide_info")], ASK_DELAY=[("15분이요", "provide_info")]),
    _s("c20", "tool_failure", "정상 답변, TMS 장애", "COMPLETE", {"eta_minutes": 30, "issue": "none", "delay_minutes": None}, 6, 1,
       ASK_ETA=[("20분에서 30분 사이요", "provide_info")], ASK_ISSUE=[("순조롭게 잘 가고 있습니다", "no_issue")]),
]


class SimDriver:
    """Answers according to what the agent just did. Clarify/answer-other fall back to the pending slot's replies."""

    def __init__(self, sc: Scenario):
        self.sc = sc
        self.used: dict[str, int] = {}

    def reply(self, action: str, pending: str | None) -> U:
        keys = [action]
        if action in ("CLARIFY", "ANSWER_OTHER") and pending:
            keys.append({"eta": "ASK_ETA", "issue": "ASK_ISSUE", "delay": "ASK_DELAY"}[pending])
        if action == "GREET_ASK_ETA":
            keys = ["ASK_ETA"]
        for k in keys:
            lst = self.sc.replies.get(k)
            if lst:
                i = self.used.get(k, 0)
                self.used[k] = i + 1
                return lst[min(i, len(lst) - 1)]
        return ("네?", "ambiguous")
