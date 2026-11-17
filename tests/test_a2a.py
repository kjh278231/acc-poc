"""A2A adapter against a real local server, driven by the mock Connect client."""
import json

import httpx
import pytest
import websockets

from app.adapters.a2a import ET, EXT
from app.adapters.mock_connect import MockConnectHTTP, MockConnectWS, init_session, text_turn

KEY = "test-key"


async def test_ws_talk_to_customer_full_call(server):
    res = await MockConnectWS(server["ws"] + "/a2a/ws", KEY).run(["한 시간 정도 걸려요", "특이사항 없어요"])
    assert res.violations == []
    assert "도착 예정 시간" in res.greeting.text
    assert all(t.traced for t in res.turns)
    assert res.turns[-1].finish["type"] == "COMPLETE"
    rec = server["orch"].tms.records["SHP-20261117-001"]
    assert rec["eta_minutes"] == 60 and rec["issue"] == "none"


async def test_https_talk_to_customer_escalate(server):
    res = await MockConnectHTTP(server["base"] + "/a2a", KEY).run(["사람이랑 통화하고 싶어요, 담당자 연결해 주세요"])
    assert res.violations == []
    assert res.turns[-1].finish == {"type": "ESCALATE", "reason": "Driver asked to speak with a human dispatcher."}
    assert res.turns[-1].metrics["action"] == "ESCALATE"


async def test_finish_type_limited_to_supported(server):
    res = await MockConnectWS(server["ws"] + "/a2a/ws", KEY, finish_types=("COMPLETE",)).run(["담당자 연결해 주세요"])
    assert res.violations == []
    assert res.turns[-1].finish["type"] == "COMPLETE"


async def test_reinit_same_context_keeps_state(server):
    ctx = "ctx-reconnect"
    c = MockConnectWS(server["ws"] + "/a2a/ws", KEY)
    await c.run(["30분이요"], ctx=ctx)
    res = await c.run(["없어요"], ctx=ctx, expect_greeting=False)  # reconnect: new connection, new INIT_SESSION, same contextId
    assert res.turns[-1].finish["type"] == "COMPLETE"


async def test_interruption_closes_turn_without_ending_collaboration(server):
    server["orch"].engines["jev_haiku"].llm.jev_latency_s = 0.0
    res = await MockConnectWS(server["ws"] + "/a2a/ws", KEY).run(["30분이요", "특이사항 없어요"], interrupt_after_first_chunk_on=0)
    assert res.violations == []
    assert res.turns[0].state == "TASK_STATE_COMPLETED"


async def test_auth_required(server):
    async with httpx.AsyncClient() as c:
        r = await c.post(server["base"] + "/a2a", json=init_session("x"))
        assert r.status_code == 401
        r = await c.post(server["base"] + "/a2a", json=init_session("x"), headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
    with pytest.raises(Exception):
        async with websockets.connect(server["ws"] + "/a2a/ws") as ws:
            await ws.recv()


async def test_jsonrpc_errors(server):
    h = {"Authorization": f"Bearer {KEY}"}
    async with httpx.AsyncClient() as c:
        r = (await c.post(server["base"] + "/a2a", json={"jsonrpc": "2.0", "id": 7, "method": "Nope"}, headers=h)).json()
        assert r["error"]["code"] == -32601 and r["error"]["data"][0]["domain"] == "a2a-protocol.org"
        r = (await c.post(server["base"] + "/a2a", json=text_turn("never-init", "hi", "1"), headers=h)).json()
        assert r["error"]["data"][0]["reason"] == "SESSION_NOT_INITIALIZED"


async def test_delegate_mode_returns_result(server):
    h = {"Authorization": f"Bearer {KEY}"}
    ctx = "ctx-delegate"
    async with httpx.AsyncClient() as c:
        await c.post(server["base"] + "/a2a/delegate", json=init_session(ctx), headers=h)
        r = (await c.post(server["base"] + "/a2a/delegate", json=text_turn(ctx, "기사님이 40분 걸린다고 합니다", "d1"), headers=h)).json()
    task = r["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "message" not in task["status"]  # no FINISH in behind-the-scenes mode
    data = next(p["data"] for a in task["artifacts"] for p in a["parts"] if "data" in p and "collected" in p.get("data", {}))
    assert data["collected"]["eta_minutes"] == 40 and data["next_action"] == "ASK_ISSUE"


async def test_audio_negotiation_counter_proposal(server):
    from app.adapters.mock_connect import init_session as mk
    req = mk("ctx-audio", audio=True)
    req["params"]["message"]["parts"][0]["data"]["initSession"]["audioOutputConfiguration"]["sampleRateHertz"] = 44100
    async with websockets.connect(server["ws"] + "/a2a/ws", additional_headers={"Authorization": f"Bearer {KEY}"}) as ws:
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
    r = resp["result"]["message"]["parts"][0]["data"]["initSessionResponse"]
    assert r["audioOutputConfiguration"]["sampleRateHertz"] == 16000
    assert resp["result"]["message"]["metadata"][ET] == "INIT_SESSION_RESPONSE"


async def test_agent_card(server):
    async with httpx.AsyncClient() as c:
        card = (await c.get(server["base"] + "/.well-known/agent-card.json")).json()
    assert card["capabilities"]["extensions"][0]["uri"] == EXT
