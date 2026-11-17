from datetime import datetime

import pytest

from app.core.decision import Decision
from app.core.rules import resolve
from app.core.state import Action, CallState, IssueType
from app.services.parser import count_time_mentions, parse_duration

NOW = datetime(2026, 11, 17, 14, 0)


@pytest.mark.parametrize("text,minutes", [
    ("30분이요", 30), ("한 삼십 분 정도요", 30), ("한 시간 반", 90), ("1시간 20분 걸려요", 80), ("두 시간쯤", 120),
    ("20~30분", 30), ("이십에서 삼십 분", 30), ("3시 반쯤", 90), ("오후 4시", 120), ("4시 20분쯤", 140),
    ("사십오 분", 45), ("한시간", 60), ("도착했어요", 0), ("한 30분 늦어질 것 같아요", 30),
])
def test_parse_duration(text, minutes):
    assert parse_duration(text, NOW).minutes == minutes


@pytest.mark.parametrize("text", ["곧 도착해요", "잘 모르겠어요", "거의 다 왔어요"])
def test_vague(text):
    p = parse_duration(text, NOW)
    assert p.minutes is None and p.vague


def test_no_false_numbers():
    assert parse_duration("이 분이 누구세요").minutes is None
    assert count_time_mentions("1시간 걸리는데 사고 나서 30분 더 늦어요") == 2
    assert count_time_mentions("1시간 20분") == 1


def _st(**slots):
    st = CallState(session_id="s")
    for k, v in slots.items():
        setattr(st.slots, k, v)
    st.last_action = Action.ASK_ETA
    return st


def test_premature_complete_blocked():
    st = _st()
    r = resolve(st, Decision(action=Action.COMPLETE, eta_minutes=30), "30분이요")
    assert r.action == Action.ASK_ISSUE and r.overridden and r.finish is None


def test_complete_when_all_known():
    st = _st(eta_minutes=30)
    st.last_action = Action.ASK_ISSUE
    r = resolve(st, Decision(action=Action.ASK_DELAY, issue=IssueType.NONE), "없어요")
    assert r.action == Action.COMPLETE and r.finish == "COMPLETE" and r.needs_tms


def test_parser_wins_over_model_number():
    st = _st()
    r = resolve(st, Decision(action=Action.ASK_ISSUE, eta_minutes=60), "한 시간 반이요")
    assert st.slots.eta_minutes == 90 and any("parser_override" in x for x in r.reasons)


def test_severe_issue_escalates_after_delay():
    st = _st(eta_minutes=60)
    st.last_action = Action.ASK_ISSUE
    r = resolve(st, Decision(action=Action.ASK_DELAY, issue=IssueType.BREAKDOWN), "타이어가 펑크 났어요")
    assert r.action == Action.ASK_DELAY
    st.last_action = r.action
    r = resolve(st, Decision(action=Action.COMPLETE, delay_minutes=40), "40분 정도요")
    assert r.action == Action.ESCALATE and r.finish_reason == "severe_issue" and r.needs_tms and r.needs_escalation_ticket


def test_reprompt_limit_escalates():
    st = _st()
    for _ in range(2):
        r = resolve(st, Decision(action=Action.CLARIFY), "글쎄요")
        assert r.action == Action.CLARIFY
    r = resolve(st, Decision(action=Action.CLARIFY), "글쎄요")
    assert r.action == Action.ESCALATE and r.finish_reason == "unresolved_eta"


def test_end_call_partial():
    st = _st(eta_minutes=30)
    r = resolve(st, Decision(action=Action.END_CALL), "바빠서 끊을게요")
    assert r.action == Action.END_CALL and r.finish == "END_CALL" and r.needs_tms


def test_out_of_range_rejected():
    st = _st()
    resolve(st, Decision(action=Action.ASK_ISSUE, eta_minutes=99999), "아주 오래요")
    assert st.slots.eta_minutes is None
