"""Conversation orchestrator: sessions, turn execution, tools, output events, per-turn metrics.

Transport-agnostic: adapters feed InputMessage and consume OutputEvent. The same code path serves the web UI,
the benchmark (direct calls) and the A2A adapter for Amazon Connect.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.core.decision import Decision, ModelCall
from app.core.events import EventType, InputKind, InputMessage, OutputEvent, new_id
from app.core.rules import Resolution, resolve
from app.core.state import Action, CallState, Shipment, Turn
from app.services import responses
from app.services.tms_mock import TmsError, TmsMock


@dataclass
class Session:
    state: CallState
    engine_name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_metrics: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.perf_counter)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.?!])\s+", text.strip()) if p.strip()]
    return parts or [text]


@dataclass
class _Span:
    name: str
    kind: int  # OTLP SpanKind: 2 SERVER, 3 CLIENT
    start_ns: int
    end_ns: int
    attrs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Orchestrator:
    def __init__(self, engines: dict[str, Any], tms: TmsMock):
        self.engines = engines
        self.tms = tms
        self.sessions: dict[str, Session] = {}
        self.cfg = get_settings()

    # ------------------------------------------------------------------ sessions
    def create_session(self, engine: str, session_id: str | None = None, shipment: Shipment | None = None) -> Session:
        if engine not in self.engines:
            raise KeyError(f"unknown engine {engine!r}; available: {list(self.engines)}")
        sid = session_id or new_id("s-")
        sess = Session(state=CallState(session_id=sid, shipment=shipment or Shipment()), engine_name=engine)
        self.sessions[sid] = sess
        return sess

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    # ------------------------------------------------------------------ turns
    async def handle(self, msg: InputMessage) -> AsyncIterator[OutputEvent]:
        sess = self.sessions[msg.session_id]
        async with sess.lock:
            if msg.kind == InputKind.START:
                async for ev in self._opening(sess, msg):
                    yield ev
                return
            if sess.state.finished:
                yield self._ev(EventType.ERROR, msg, data={"code": 409, "reason": "session already finished"})
                return
            async for ev in self._turn(sess, msg):
                yield ev

    async def run_turn(self, msg: InputMessage) -> list[OutputEvent]:
        return [ev async for ev in self.handle(msg)]

    async def _opening(self, sess: Session, msg: InputMessage) -> AsyncIterator[OutputEvent]:
        st = sess.state
        t0_ns = time.time_ns()
        text = responses.render(Action.GREET_ASK_ETA, st)
        st.history.append(Turn(role="agent", text=text, action=Action.GREET_ASK_ETA.value))
        st.last_action = Action.GREET_ASK_ETA
        mid = new_id("msg-")
        for chunk in split_sentences(text):
            yield self._ev(EventType.TEXT_CHUNK, msg, text=chunk, message_id=mid)
        yield self._ev(EventType.TEXT_DONE, msg, text=text, message_id=mid)
        root = _Span(name=f"invoke_agent {self.cfg.a2a_agent_name}", kind=2, start_ns=t0_ns, end_ns=time.time_ns(),
                     attrs={"gen_ai.agent.name": self.cfg.a2a_agent_name, "gen_ai.operation.name": "invoke_agent",
                            "acc.engine": sess.engine_name, "acc.action": "GREET_ASK_ETA", "acc.source": "template",
                            "acc.pending": st.pending_slot() or "", "_input": "<CHANNEL_READY>", "_output": text})
        yield self._ev(EventType.TURN_END, msg, data={"metrics": {"action": "GREET_ASK_ETA", "opening": True},
                                                       "spans": [root.__dict__]})

    async def _turn(self, sess: Session, msg: InputMessage) -> AsyncIterator[OutputEvent]:
        st, engine = sess.state, self.engines[sess.engine_name]
        t0, t0_ns = msg.received_at, time.time_ns()
        text = msg.text if msg.kind == InputKind.TEXT else ("" if msg.kind == InputKind.SILENCE else msg.text)
        st.turn_count += 1
        spans: list[_Span] = []

        # ---- 1. decide ----
        if msg.kind == InputKind.SILENCE or not text.strip():
            d = Decision(action=Action.CLARIFY, source="rule:silence")
        else:
            try:
                d = await engine.decide(st, text)
            except Exception as e:  # engine totally failed: re-ask with a template rather than drop the call
                call = getattr(e, "call", None)
                d = Decision(action=Action.CLARIFY, source="error", raw={"error": str(e)[:200]},
                             calls=[call] if call else [])
        st.history.append(Turn(role="driver", text=text or "<침묵>"))
        t_decided = time.perf_counter()
        spans += [self._call_span(c) for c in d.calls]

        # ---- 2. rules ----
        res = resolve(st, d, text)
        st.last_action = res.action

        # ---- 3. reply + tools ----
        use_generated = d.reply is not None and not res.overridden and res.action not in (Action.COMPLETE, Action.ESCALATE, Action.END_CALL)
        reply = d.reply if use_generated else responses.render(res.action, st, res.filled, res.finish_reason)
        if res.action == Action.ANSWER_OTHER and d.reply:
            reply = d.reply
        extra_calls: list[ModelCall] = []
        tool_ms = 0.0
        tool_result: dict[str, Any] | None = None
        first_ready_ms: float | None = None
        mid = new_id("msg-")
        needs_tool = res.needs_tms or res.needs_escalation_ticket

        if needs_tool and engine.parallel_tools:
            tool_task = asyncio.ensure_future(self._run_tools(st, res, spans))
            first_ready_ms = (time.perf_counter() - t0) * 1000
            yield self._ev(EventType.TOOL_STARTED, msg, data={"tools": self._tool_names(res)})
            for chunk in split_sentences(reply):
                yield self._ev(EventType.TEXT_CHUNK, msg, text=chunk, message_id=mid)
            yield self._ev(EventType.TEXT_DONE, msg, text=reply, message_id=mid)
            tool_result, tool_ms = await asyncio.shield(tool_task)
            yield self._ev(EventType.TOOL_RESULT, msg, data=tool_result)
            if not tool_result.get("ok"):
                # follow-up as a separate message (A2A: a new artifact), never re-opening the spoken one
                mid2 = new_id("msg-")
                yield self._ev(EventType.TEXT_CHUNK, msg, text=responses.TMS_FAILED, message_id=mid2)
                yield self._ev(EventType.TEXT_DONE, msg, text=responses.TMS_FAILED, message_id=mid2)
                reply = f"{reply} {responses.TMS_FAILED}"
        elif needs_tool:
            yield self._ev(EventType.TOOL_STARTED, msg, data={"tools": self._tool_names(res)})
            tool_result, tool_ms = await asyncio.shield(self._run_tools(st, res, spans))
            yield self._ev(EventType.TOOL_RESULT, msg, data=tool_result)
            final, extra_calls = await engine.compose_after_tool(st, res.action, tool_result, d.reply)
            spans += [self._call_span(c) for c in extra_calls]
            if final:
                reply = final
            elif not tool_result.get("ok"):
                reply = f"{reply} {responses.TMS_FAILED}"
            first_ready_ms = (time.perf_counter() - t0) * 1000
            for chunk in split_sentences(reply):
                yield self._ev(EventType.TEXT_CHUNK, msg, text=chunk, message_id=mid)
            yield self._ev(EventType.TEXT_DONE, msg, text=reply, message_id=mid)
        else:
            first_ready_ms = (time.perf_counter() - t0) * 1000
            for chunk in split_sentences(reply):
                yield self._ev(EventType.TEXT_CHUNK, msg, text=chunk, message_id=mid)
            yield self._ev(EventType.TEXT_DONE, msg, text=reply, message_id=mid)

        st.history.append(Turn(role="agent", text=reply, action=res.action.value))
        if res.finish:
            st.finished, st.finish_reason = res.finish, res.finish_reason
            yield self._ev(EventType.FINISH, msg, data={"type": "ESCALATE" if res.finish == "ESCALATE" else "COMPLETE",
                                                         "outcome": res.finish, "reason": res.finish_reason})

        # ---- 4. metrics + spans ----
        calls = d.calls + extra_calls
        total_ms = (time.perf_counter() - t0) * 1000
        metrics = self._metrics(sess, d, res, calls, t0, t_decided, first_ready_ms, tool_ms, total_ms, reply, text, tool_result)
        sess.turn_metrics.append(metrics)
        root = _Span(name=f"invoke_agent {self.cfg.a2a_agent_name}", kind=2, start_ns=t0_ns, end_ns=time.time_ns(),
                     attrs={"gen_ai.agent.name": self.cfg.a2a_agent_name, "gen_ai.operation.name": "invoke_agent",
                            "gen_ai.usage.input_tokens": metrics["input_tokens"],
                            "gen_ai.usage.output_tokens": metrics["output_tokens"],
                            "acc.engine": sess.engine_name, "acc.action": res.action.value, "acc.source": d.source,
                            "acc.pending": st.pending_slot() or "", "acc.finish": res.finish or "",
                            "_input": text, "_output": reply})
        yield self._ev(EventType.TURN_END, msg, data={"metrics": metrics, "spans": [root.__dict__] + [s.__dict__ for s in spans]})

    # ------------------------------------------------------------------ tools
    @staticmethod
    def _tool_names(res: Resolution) -> list[str]:
        return (["tms.update_shipment"] if res.needs_tms else []) + (["tms.create_escalation"] if res.needs_escalation_ticket else [])

    async def _run_tools(self, st: CallState, res: Resolution, spans: list[_Span]) -> tuple[dict[str, Any], float]:
        t0 = time.perf_counter()
        out: dict[str, Any] = {"ok": True}
        s = st.slots
        status = {"COMPLETE": "confirmed", "ESCALATE": "escalated", "END_CALL": "partial"}.get(res.finish or "", "partial")
        if res.needs_tms:
            out["tms"] = await self._tool(spans, "tms.update_shipment", self.tms.update_shipment(
                st.shipment.shipment_id, session_id=st.session_id, eta_minutes=s.eta_minutes,
                issue=s.issue.value if s.issue else None, delay_minutes=s.delay_minutes, status=status))
            if out["tms"].get("error"):
                out["ok"] = False
            else:
                st.tms_updated, st.tms_record = True, out["tms"]
        if res.needs_escalation_ticket:
            out["escalation"] = await self._tool(spans, "tms.create_escalation", self.tms.create_escalation(
                st.shipment.shipment_id, session_id=st.session_id, reason=res.finish_reason or "escalate"))
            if out["escalation"].get("error"):
                out["ok"] = False
        return out, (time.perf_counter() - t0) * 1000

    async def _tool(self, spans: list[_Span], name: str, coro) -> dict[str, Any]:
        start = time.time_ns()
        try:
            r = await coro
            spans.append(_Span(name=f"execute_tool {name}", kind=3, start_ns=start, end_ns=time.time_ns(),
                               attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "_output": str(r)[:500]}))
            return r
        except TmsError as e:
            spans.append(_Span(name=f"execute_tool {name}", kind=3, start_ns=start, end_ns=time.time_ns(),
                               attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}, error=str(e)))
            return {"error": str(e)}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _call_span(c: ModelCall) -> _Span:
        return _Span(name=f"chat {c.model}", kind=3, start_ns=c.start_ns, end_ns=c.end_ns,
                     attrs={"gen_ai.operation.name": "chat", "gen_ai.request.model": c.model, "acc.purpose": c.purpose,
                            "gen_ai.usage.input_tokens": c.input_tokens, "gen_ai.usage.output_tokens": c.output_tokens,
                            "acc.cost_usd": c.cost_usd, "acc.ttft_ms": c.ttft_ms},
                     error=c.error if not c.ok else None)

    def _metrics(self, sess: Session, d: Decision, res: Resolution, calls: list[ModelCall], t0: float, t_decided: float,
                 first_ready_ms: float | None, tool_ms: float, total_ms: float, reply: str, text: str,
                 tool_result: dict[str, Any] | None) -> dict[str, Any]:
        by_model: dict[str, int] = {}
        for c in calls:
            by_model[c.model] = by_model.get(c.model, 0) + 1
        return {
            "session_id": sess.state.session_id,
            "engine": sess.engine_name,
            "turn": sess.state.turn_count,
            "input": text,
            "reply": reply,
            "intent": d.intent,
            "proposed_action": d.action.value,
            "action": res.action.value,
            "overridden": res.overridden,
            "rule_reasons": res.reasons,
            "source": d.source,
            "fallback": d.source.startswith("jev>"),
            "fallback_reason": d.fallback_reason,
            "confidence": d.confidence,
            "decision_ms": (t_decided - t0) * 1000,
            "model_decision_ms": d.decision_ms,
            "ttft_ms": d.ttft_ms,
            "first_response_ready_ms": first_ready_ms,
            "tool_ms": tool_ms,
            "tool_ok": None if tool_result is None else bool(tool_result.get("ok")),
            "turn_total_ms": total_ms,
            "calls": [c.model_dump(exclude={"start_ns", "end_ns"}) for c in calls],
            "calls_by_model": by_model,
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "cost_usd": sum(c.cost_usd for c in calls),
            "cost_actual": all(c.cost_actual for c in calls) if calls else True,
            "model_errors": [c.error for c in calls if not c.ok],
            "slots": sess.state.slots_summary(),
            "finish": res.finish,
            "finish_reason": res.finish_reason,
        }

    @staticmethod
    def _ev(t: EventType, msg: InputMessage, text: str | None = None, message_id: str | None = None,
            data: dict[str, Any] | None = None) -> OutputEvent:
        return OutputEvent(type=t, session_id=msg.session_id, turn_id=msg.turn_id, trace_id=msg.trace_id,
                           message_id=message_id, text=text, data=data or {})
