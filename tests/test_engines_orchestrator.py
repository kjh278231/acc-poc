import pytest

from app.core.events import EventType, InputKind, InputMessage
from app.core.state import Action, CallState, IssueType
from app.engines.haiku_only import HaikuOnlyEngine
from app.engines.jev_haiku import JevHaikuEngine
from app.services.llm_mock import MockLLM


def ans(intent, ic, issue="not_mentioned", jc=0.95):
    return {"intent": {"choice": intent, "confidence": ic}, "issue": {"choice": issue, "confidence": jc}}


@pytest.fixture
def jev():
    llm = MockLLM()
    return JevHaikuEngine(llm, HaikuOnlyEngine(llm), threshold=0.8)


def test_jev_low_confidence_falls_back(jev):
    d = jev.map_answers(CallState(session_id="s"), "음...", ans("provide_info", 0.6))
    assert d.action == Action.FALLBACK and d.fallback_reason == "low_confidence_intent"


def test_jev_threshold_configurable():
    llm = MockLLM()
    e = JevHaikuEngine(llm, HaikuOnlyEngine(llm), threshold=0.5)
    d = e.map_answers(CallState(session_id="s"), "30분", ans("provide_info", 0.6))
    assert d.action == Action.ASK_ISSUE and d.eta_minutes == 30


def test_jev_off_topic_and_multi_time_fall_back(jev):
    st = CallState(session_id="s")
    assert jev.map_answers(st, "점심 먹었어요?", ans("off_topic", 0.95)).fallback_reason == "off_topic"
    d = jev.map_answers(st, "1시간 걸리는데 사고로 30분 늦어요", ans("provide_info", 0.95, "accident"))
    assert d.fallback_reason == "multiple_time_expressions"


def test_jev_issue_and_delay_in_one_answer(jev):
    st = CallState(session_id="s")
    st.slots.eta_minutes = 60
    d = jev.map_answers(st, "길이 막혀서 20분 늦어요", ans("provide_info", 0.95, "traffic"))
    assert d.issue == IssueType.TRAFFIC and d.delay_minutes == 20 and d.action == Action.COMPLETE


def test_jev_request_human(jev):
    d = jev.map_answers(CallState(session_id="s"), "담당자 바꿔주세요", ans("request_human", 0.97))
    assert d.action == Action.ESCALATE


async def _talk(orch, engine, lines):
    s = orch.create_session(engine)
    sid = s.state.session_id
    evs = await orch.run_turn(InputMessage(session_id=sid, kind=InputKind.START))
    for line in lines:
        evs += await orch.run_turn(InputMessage(session_id=sid, text=line))
        if s.state.finished:
            break
    return s, evs


@pytest.mark.parametrize("engine", ["haiku_only", "jev_haiku"])
async def test_full_call_updates_tms(orch, engine):
    s, evs = await _talk(orch, engine, ["한 시간 반 정도요", "길이 좀 막혀요", "한 20분 늦어질 것 같아요"])
    assert s.state.finished == "COMPLETE"
    rec = orch.tms.records[s.state.shipment.shipment_id]
    assert (rec["eta_minutes"], rec["issue"], rec["delay_minutes"], rec["status"]) == (90, "traffic", 20, "confirmed")
    assert any(e.type == EventType.FINISH and e.data["type"] == "COMPLETE" for e in evs)
    # every event carries the correlation ids
    assert all(e.session_id and e.turn_id and e.trace_id for e in evs)


@pytest.mark.parametrize("engine", ["haiku_only", "jev_haiku"])
async def test_escalation_request(orch, engine):
    s, evs = await _talk(orch, engine, ["담당자 연결해 주세요"])
    assert s.state.finished == "ESCALATE" and orch.tms.tickets


async def test_parallel_tool_failure_is_reported(orch):
    orch.tms.fail_next = 1
    s, evs = await _talk(orch, "jev_haiku", ["30분이요", "특이사항 없어요"])
    texts = [e.text for e in evs if e.type == EventType.TEXT_DONE]
    assert "지연되고 있어" in texts[-1]
    assert not s.state.tms_updated
    m = s.turn_metrics[-1]
    assert m["tool_ok"] is False
    # the spoken reply was ready before the tool finished
    assert m["first_response_ready_ms"] < m["first_response_ready_ms"] + m["tool_ms"]


async def test_sequential_tool_for_haiku_only(orch):
    s, _ = await _talk(orch, "haiku_only", ["30분이요", "특이사항 없어요"])
    m = s.turn_metrics[-1]
    assert m["action"] == "COMPLETE"
    assert m["first_response_ready_ms"] >= m["tool_ms"]  # reply waits for the tool
    assert m["calls_by_model"].get("mock/haiku") == 2   # decide + compose


async def test_idempotent_tms(orch):
    st = orch.create_session("jev_haiku").state
    for _ in range(2):
        await orch.tms.update_shipment("X", session_id=st.session_id, eta_minutes=1, issue="none", delay_minutes=None, status="confirmed")
    assert len(orch.tms.updates) == 1


async def test_finished_session_rejects_turns(orch):
    s, _ = await _talk(orch, "jev_haiku", ["담당자 바꿔줘요"])
    evs = await orch.run_turn(InputMessage(session_id=s.state.session_id, text="여보세요"))
    assert evs[0].type == EventType.ERROR
