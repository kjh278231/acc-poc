"""Deterministic rule engine shared by both architectures.

Engines *propose* (intent, slot values, action). This module decides what actually happens:
  - slot validation (range checks; the common parser wins over model numbers)
  - which slot to ask next (fixed order: ETA -> issue -> delay)
  - completion approval (COMPLETE only when every required slot is known)
  - escalation (driver request, severe issue, too many failed re-asks / off-topic turns)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import get_settings
from app.core.decision import Decision
from app.core.state import SEVERE_ISSUES, Action, CallState, IssueType
from app.services.parser import count_time_mentions, parse_duration

MAX_MINUTES = 24 * 60
ASK_FOR = {"eta": Action.ASK_ETA, "issue": Action.ASK_ISSUE, "delay": Action.ASK_DELAY}


@dataclass
class Resolution:
    action: Action
    filled: set[str] = field(default_factory=set)
    overridden: bool = False          # rule engine changed the engine's proposed action
    reasons: list[str] = field(default_factory=list)
    finish: str | None = None         # COMPLETE | ESCALATE | END_CALL
    finish_reason: str | None = None
    needs_tms: bool = False
    needs_escalation_ticket: bool = False


def _valid_minutes(v: int | None) -> int | None:
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= MAX_MINUTES else None


def resolve(state: CallState, d: Decision, text: str) -> Resolution:
    """Mutates state.slots / counters and returns the action to execute."""
    cfg = get_settings()
    s = state.slots
    pending_before = state.pending_slot()
    reasons: list[str] = []
    filled: set[str] = set()

    # ---- 1. slot values: the common parser wins when it found exactly what the model reports ----
    parsed = parse_duration(text)
    eta, delay = _valid_minutes(d.eta_minutes), _valid_minutes(d.delay_minutes)
    if parsed.ok and count_time_mentions(text) <= 1:
        # a single number in the utterance belongs to the slot the model (or the state) points at
        if eta is not None and delay is None and eta != parsed.minutes:
            reasons.append(f"parser_override_eta:{eta}->{parsed.minutes}")
            eta = parsed.minutes
        elif delay is not None and eta is None and delay != parsed.minutes:
            reasons.append(f"parser_override_delay:{delay}->{parsed.minutes}")
            delay = parsed.minutes

    if eta is not None and s.eta_minutes != eta:
        s.eta_minutes = eta
        filled.add("eta")
    if d.issue is not None and (s.issue is None or d.issue != s.issue):
        s.issue = d.issue
        filled.add("issue")
    if delay is not None and s.issue not in (None, IssueType.NONE) and s.delay_minutes != delay:
        s.delay_minutes = delay
        filled.add("delay")
    if s.issue == IssueType.NONE:
        s.delay_minutes = None

    pending = state.pending_slot()
    proposed = d.action
    res = Resolution(action=proposed, filled=filled, reasons=reasons)

    # ---- 2. terminal requests from the driver ----
    if proposed == Action.ESCALATE:
        return _finish(res, state, Action.ESCALATE, d.raw.get("escalate_reason") or "driver_request")
    if proposed == Action.END_CALL:
        if pending is None:
            return _finish(res, state, Action.COMPLETE, "all_slots_collected", overridden=True)
        return _finish(res, state, Action.END_CALL, f"driver_ended_missing_{pending}")

    # ---- 3. severe issue: record what we have and hand to a human ----
    if s.issue in SEVERE_ISSUES and (pending is None or state.reprompts.get("delay", 0) >= 1 or proposed == Action.CLARIFY and pending == "delay"):
        return _finish(res, state, Action.ESCALATE, "severe_issue", overridden=proposed != Action.ESCALATE)

    # ---- 4. completion approval ----
    if pending is None:
        if s.issue in SEVERE_ISSUES:
            return _finish(res, state, Action.ESCALATE, "severe_issue", overridden=proposed != Action.ESCALATE)
        return _finish(res, state, Action.COMPLETE, "all_slots_collected", overridden=proposed != Action.COMPLETE)
    if proposed == Action.COMPLETE:
        reasons.append(f"blocked_premature_complete:{pending}")
        res.overridden = True
        proposed = ASK_FOR[pending]

    # ---- 5. off-topic / re-ask budget ----
    if proposed == Action.ANSWER_OTHER:
        state.off_topic_count += 1
        if state.off_topic_count > cfg.max_off_topic:
            return _finish(res, state, Action.ESCALATE, "too_many_off_topic", overridden=True)
        res.action = Action.ANSWER_OTHER
        return res

    progressed = bool(filled)
    if proposed == Action.CLARIFY or (not progressed and pending == pending_before and proposed in ASK_FOR.values()
                                      and state.last_action in (ASK_FOR[pending], Action.CLARIFY, Action.GREET_ASK_ETA)):
        n = state.reprompts.get(pending, 0) + 1
        state.reprompts[pending] = n
        if n > cfg.max_reprompts_per_slot:
            return _finish(res, state, Action.ESCALATE, f"unresolved_{pending}", overridden=True)
        res.overridden = res.overridden or proposed != Action.CLARIFY
        res.action = Action.CLARIFY
        return res

    # ---- 6. ask the next missing slot (fixed order) ----
    want = ASK_FOR[pending]
    if proposed != want:
        res.overridden = True
        reasons.append(f"ask_order:{proposed.value}->{want.value}")
    res.action = want
    return res


def _finish(res: Resolution, state: CallState, action: Action, reason: str, overridden: bool = False) -> Resolution:
    res.action = action
    res.overridden = res.overridden or overridden
    res.finish = action.value
    res.finish_reason = reason
    s = state.slots
    has_any = s.eta_minutes is not None or s.issue is not None
    res.needs_tms = action == Action.COMPLETE or has_any
    res.needs_escalation_ticket = action == Action.ESCALATE
    return res
