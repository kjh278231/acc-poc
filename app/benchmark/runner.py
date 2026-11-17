"""Benchmark runner.

  uv run python -m app.benchmark.runner --reps 5                 # all three parts, real APIs (needs OPENROUTER_API_KEY)
  uv run python -m app.benchmark.runner --reps 1 --mock          # offline dry run (labelled mock; not model results)
  uv run python -m app.benchmark.runner --parts decisions --reps 5

Parts
  decisions      pure decision benchmark: engine.decide() + rule engine on a fixed state, no tools, no I/O adapter
  conversations  full calls with a simulated driver, direct orchestrator calls (TMS mock, tools, templates/generation)
  a2a            a subset of calls through the A2A WebSocket adapter of a real local server (mock Connect client)

Fairness: identical inputs and states for both engines; the (case, engine, rep) units are shuffled together with a
fixed seed so both engines see the same API conditions; bounded concurrency.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.benchmark.cases import DECISION_CASES, SCENARIOS, DecisionCase, Scenario, SimDriver, make_state
from app.config import ROOT, get_settings
from app.core.events import EventType, InputKind, InputMessage
from app.core.orchestrator import Orchestrator
from app.core.rules import resolve
from app.core.state import Shipment
from app.factory import build_engines, build_llm
from app.services.tms_mock import TmsMock

ENGINES = ["haiku_only", "jev_haiku"]


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, limit: float):
        self.limit, self.spent = limit, 0.0

    def add(self, usd: float) -> None:
        self.spent += usd
        if self.spent > self.limit:
            raise BudgetExceeded(f"benchmark cost cap reached: ${self.spent:.4f} > ${self.limit:.2f}")


# ---------------------------------------------------------------------- part 1: decisions
async def run_decision(engines: dict, case: DecisionCase, engine: str, rep: int) -> dict[str, Any]:
    st = make_state(case.preset, session_id=f"dec-{case.id}-{rep}")
    t0 = time.perf_counter()
    err = None
    try:
        d = await engines[engine].decide(st, case.text)
    except Exception as e:  # noqa: BLE001
        err = str(e)[:200]
        return {"part": "decisions", "case": case.id, "category": case.category, "engine": engine, "rep": rep,
                "error": err, "decision_ms": (time.perf_counter() - t0) * 1000, "cost_usd": 0.0, "calls": []}
    wall = (time.perf_counter() - t0) * 1000
    after = copy.deepcopy(st)
    res = resolve(after, d, case.text)
    slots = after.slots
    got = {"eta_minutes": slots.eta_minutes, "issue": slots.issue.value if slots.issue else None, "delay_minutes": slots.delay_minutes}
    slot_ok = all(got[k] == v for k, v in case.slots.items())
    return {
        "part": "decisions", "case": case.id, "category": case.category, "engine": engine, "rep": rep, "text": case.text,
        "intent": d.intent, "intent_ok": d.intent in case.intents, "action": res.action.value,
        "action_ok": res.action.value in case.actions, "slots": got, "slot_ok": slot_ok,
        "correct": d.intent in case.intents and res.action.value in case.actions and slot_ok,
        "proposed_action": d.action.value, "overridden": res.overridden, "source": d.source,
        "fallback": d.source.startswith("jev>"), "fallback_reason": d.fallback_reason, "confidence": d.confidence,
        "decision_ms": wall, "ttft_ms": d.ttft_ms,
        "input_tokens": sum(c.input_tokens for c in d.calls), "output_tokens": sum(c.output_tokens for c in d.calls),
        "cost_usd": sum(c.cost_usd for c in d.calls), "cost_actual": all(c.cost_actual for c in d.calls),
        "calls": [c.model_dump(exclude={"start_ns", "end_ns"}) for c in d.calls], "error": None,
    }


# ---------------------------------------------------------------------- part 2: conversations
async def run_conversation(engines: dict, sc: Scenario, engine: str, rep: int, tms_latency_ms: int) -> dict[str, Any]:
    tms = TmsMock(latency_ms=tms_latency_ms)
    tms.fail_next = sc.tms_fail
    orch = Orchestrator(engines, tms)
    sid = f"conv-{sc.id}-{engine}-{rep}-{uuid.uuid4().hex[:6]}"
    sess = orch.create_session(engine, session_id=sid, shipment=Shipment(shipment_id=f"SHP-{sid}"))
    st = sess.state
    driver = SimDriver(sc)
    t_start = time.perf_counter()
    await orch.run_turn(InputMessage(session_id=sid, kind=InputKind.START))
    turns, intents_ok, intents_n, replies = [], 0, 0, []
    action = "GREET_ASK_ETA"
    while not st.finished and len(turns) < sc.max_turns:
        text, exp_intent = driver.reply(action, st.pending_slot())
        evs = await orch.run_turn(InputMessage(session_id=sid, text=text))
        m = next(e.data["metrics"] for e in evs if e.type == EventType.TURN_END)
        intents_n += 1
        intents_ok += int(m["intent"] == exp_intent or (exp_intent == "provide_info" and m["intent"] == "no_issue" and "없" in text))
        action = m["action"]
        replies.append(m["reply"])
        turns.append({k: m[k] for k in ("turn", "input", "reply", "intent", "action", "source", "fallback", "fallback_reason",
                                        "decision_ms", "ttft_ms", "first_response_ready_ms", "tool_ms", "turn_total_ms",
                                        "input_tokens", "output_tokens", "cost_usd", "cost_actual", "calls_by_model",
                                        "overridden", "model_errors", "tool_ok")} | {"expected_intent": exp_intent})
    total_ms = (time.perf_counter() - t_start) * 1000
    rec = tms.records.get(st.shipment.shipment_id)
    got = {"eta_minutes": st.slots.eta_minutes, "issue": st.slots.issue.value if st.slots.issue else None,
           "delay_minutes": st.slots.delay_minutes}
    slots_ok = all(got[k] == v for k, v in sc.expect_slots.items())
    outcome_ok = st.finished == sc.expect_outcome
    if any(v is not None for v in sc.expect_slots.values()):
        tms_ok = rec is not None and all(rec.get(k) == v for k, v in sc.expect_slots.items())
    else:  # nothing collected -> nothing to record
        tms_ok = rec is None
    if sc.tms_fail:  # failure must be reported, never claimed as success
        last = replies[-1] if replies else ""
        tool_failure_handled = "지연" in last and "등록했습니다" not in last
        tms_ok = True
    else:
        tool_failure_handled = None
    return {
        "part": "conversations", "scenario": sc.id, "category": sc.category, "engine": engine, "rep": rep,
        "outcome": st.finished, "expected_outcome": sc.expect_outcome, "outcome_ok": outcome_ok,
        "slots": got, "slots_ok": slots_ok, "tms_ok": tms_ok, "tool_failure_handled": tool_failure_handled,
        "success": outcome_ok and slots_ok and tms_ok and (tool_failure_handled in (None, True)),
        "wrong_termination": bool(st.finished) and not (outcome_ok and slots_ok),
        "stuck": not st.finished,
        "reprompts": sum(1 for t in turns if t["action"] == "CLARIFY"),
        "n_turns": len(turns), "intent_ok": intents_ok, "intent_n": intents_n,
        "scenario_ms": total_ms, "turns": turns,
        "cost_usd": sum(t["cost_usd"] for t in turns), "input_tokens": sum(t["input_tokens"] for t in turns),
        "output_tokens": sum(t["output_tokens"] for t in turns),
    }


# ---------------------------------------------------------------------- part 3: a2a
def _start_server(orch: Orchestrator) -> tuple[Any, threading.Thread, int]:
    import uvicorn

    from app.main import create_app
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(orch), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, th, port


async def run_a2a_conversation(port: int, sc: Scenario, engine: str, rep: int, proto) -> dict[str, Any]:
    """One talk-to-the-customer call over WebSocket with a reactive driver (action read from the turn's trace span)."""
    import websockets

    from app.adapters.mock_connect import TurnResult, TurnValidator, channel_ready, init_session, text_turn
    proto.engine = engine
    ctx = f"a2a-{sc.id}-{engine}-{rep}-{uuid.uuid4().hex[:6]}"
    key = get_settings().a2a_api_key
    headers = {"Authorization": f"Bearer {key}", "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"}
    driver = SimDriver(sc)
    turns, violations = [], []
    t_conn = time.perf_counter()
    async with websockets.connect(f"ws://127.0.0.1:{port}/a2a/ws", additional_headers=headers, max_size=None) as ws:
        connect_ms = (time.perf_counter() - t_conn) * 1000
        t0 = time.perf_counter()
        await ws.send(json.dumps(init_session(ctx)))
        await ws.recv()
        init_ms = (time.perf_counter() - t0) * 1000
        action, pending, finish = "GREET_ASK_ETA", "eta", None

        async def collect(label: str, t0: float) -> tuple[TurnResult, dict]:
            tr, val, root = TurnResult(input=label), TurnValidator(ctx, ("COMPLETE", "ESCALATE")), {}
            while True:
                f = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                au = (f.get("result") or {}).get("artifactUpdate")
                if au and (au["artifact"].get("metadata") or {}).get("https://docs.aws.amazon.com/connect/a2a/ext/v1/eventType") == "TRACING_SPAN":
                    sp = au["artifact"]["parts"][0]["data"]["tracingSpan"]["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
                    root = {"server_ms": (int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6,
                            **{a["key"]: list(a["value"].values())[0] for a in sp["attributes"]}}
                if val.frame(f, tr, t0):
                    break
            tr.turn_ms = (time.perf_counter() - t0) * 1000
            violations.extend(val.violations)
            return tr, root

        t0 = time.perf_counter()
        await ws.send(json.dumps(channel_ready(ctx)))
        greet, _ = await collect("<READY>", t0)
        for i in range(sc.max_turns):
            text, _ = driver.reply(action, pending or None)
            t0 = time.perf_counter()
            await ws.send(json.dumps(text_turn(ctx, text, f"r{i}")))
            tr, root = await collect(text, t0)
            action, pending = root.get("acc.action", "CLARIFY"), root.get("acc.pending") or None
            server_ms = root.get("server_ms")
            turns.append({"input": text, "reply": tr.text, "action": action, "first_chunk_ms": tr.first_chunk_ms,
                          "turn_ms": tr.turn_ms, "server_ms": server_ms,
                          "overhead_ms": (tr.turn_ms - server_ms) if server_ms is not None else None,
                          "traced": tr.traced, "state": tr.state})
            if tr.finish or tr.state != "TASK_STATE_COMPLETED":
                finish = tr.finish
                break
    return {"part": "a2a", "scenario": sc.id, "engine": engine, "rep": rep, "connect_ms": connect_ms, "init_ms": init_ms,
            "greeting_ms": greet.turn_ms, "turns": turns, "finish": finish, "violations": violations,
            "outcome_ok": (finish or {}).get("type") == ("ESCALATE" if sc.expect_outcome == "ESCALATE" else "COMPLETE")}


# ---------------------------------------------------------------------- driver
async def _gather_bounded(units: list, concurrency: int, budget: Budget, out_path: Path, label: str) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    done = 0
    stop = False

    async def one(fn):
        nonlocal done, stop
        if stop:
            return
        async with sem:
            if stop:
                return
            r = await fn()
            results.append(r)
            done += 1
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            try:
                budget.add(r.get("cost_usd") or 0.0)
            except BudgetExceeded as e:
                print(f"\n!! {e} — stopping {label}")
                stop = True
            if done % 10 == 0 or done == len(units):
                print(f"  {label}: {done}/{len(units)}  spent ${budget.spent:.4f}", flush=True)

    await asyncio.gather(*(one(u) for u in units))
    return results


async def main_async(args) -> Path:
    s = get_settings()
    if args.mock:
        os.environ["LLM_MODE"] = "mock"
        get_settings.cache_clear()
        s = get_settings()
    llm = build_llm(s)
    engines = build_engines(llm, s)
    run_id = time.strftime("%Y%m%d-%H%M%S") + ("-mock" if s.is_mock else "")
    raw = ROOT / "results" / "raw" / run_id
    raw.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "mode": "mock" if s.is_mock else "live", "reps": args.reps, "seed": args.seed,
            "concurrency": args.concurrency, "haiku_model": s.haiku_model, "haiku_reasoning": s.haiku_reasoning,
            "jev_model": s.jev_model, "jev_threshold": s.jev_confidence_threshold, "tms_latency_ms": s.tms_latency_ms,
            "started": time.strftime("%Y-%m-%d %H:%M:%S %z"), "parts": args.parts}
    (raw / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    budget = Budget(args.max_cost if args.max_cost is not None else s.bench_max_cost_usd)
    rng = random.Random(args.seed)
    print(f"run {run_id}  mode={meta['mode']}  haiku={s.haiku_model}  jev={s.jev_model}  cap=${budget.limit}")

    if "decisions" in args.parts:
        units = [(c, e, r) for c in DECISION_CASES for e in ENGINES for r in range(args.reps)]
        rng.shuffle(units)
        await _gather_bounded([lambda c=c, e=e, r=r: run_decision(engines, c, e, r) for c, e, r in units],
                              args.concurrency, budget, raw / "decisions.jsonl", "decisions")
    if "conversations" in args.parts:
        units = [(sc, e, r) for sc in SCENARIOS for e in ENGINES for r in range(args.reps)]
        rng.shuffle(units)
        await _gather_bounded([lambda sc=sc, e=e, r=r: run_conversation(engines, sc, e, r, s.tms_latency_ms) for sc, e, r in units],
                              args.concurrency, budget, raw / "conversations.jsonl", "conversations")
    if "a2a" in args.parts:
        from app.adapters.a2a import A2AProtocol
        orch = Orchestrator(engines, TmsMock(latency_ms=s.tms_latency_ms))
        srv, th, port = _start_server(orch)
        try:
            # the server's A2A protocol object picks the engine per session; run sequentially per engine switch
            proto: A2AProtocol = srv.config.app.state.a2a
            subset = [sc for sc in SCENARIOS if sc.id in {"c01", "c04", "c06", "c08", "c12", "c14"}]
            units = [(sc, e, r) for sc in subset for e in ENGINES for r in range(args.a2a_reps)]
            rng.shuffle(units)
            out = raw / "a2a.jsonl"
            for i, (sc, e, r) in enumerate(units):
                rec = await run_a2a_conversation(port, sc, e, r, proto)
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                if (i + 1) % 6 == 0 or i + 1 == len(units):
                    print(f"  a2a: {i + 1}/{len(units)}", flush=True)
        finally:
            srv.should_exit = True
            th.join(timeout=5)
    if hasattr(llm, "aclose"):
        await llm.aclose()
    meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
    meta["spent_usd"] = budget.spent
    (raw / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(f"raw results: {raw}  (spent ${budget.spent:.4f})")
    return raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--a2a-reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261117)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--parts", nargs="+", default=["decisions", "conversations", "a2a"])
    ap.add_argument("--max-cost", type=float, default=None)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()
    raw = asyncio.run(main_async(args))
    if not args.no_report:
        from app.benchmark.metrics import build_report
        print(build_report(raw))


if __name__ == "__main__":
    main()
