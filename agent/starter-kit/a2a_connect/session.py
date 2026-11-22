# SPDX-License-Identifier: MIT-0
"""Per-``contextId`` session state.

A session is NEW until INIT_SESSION, INITIALIZED after INIT_SESSION_RESPONSE,
READY once CHANNEL_STATE READY is seen (talk mode), and FINISHED after FINISH.
The kit keys sessions by ``contextId`` so a chat-style new connection, or a
voice reconnect, finds the same session.

Mode detection follows the interop convention: a session is talk mode once it
has seen READY and delegate mode until then, unless INIT_SESSION carried an
explicit ``interactionMode`` (HANDOFF or DELEGATE, seen in service code).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

NEW = "NEW"
INITIALIZED = "INITIALIZED"
READY = "READY"
FINISHED = "FINISHED"


@dataclass
class Session:
    context_id: str
    state: str = NEW
    supported_finish_types: list[str] = field(default_factory=list)
    subscribe_to_tracing: bool = True
    history: list[dict] = field(default_factory=list)
    contact_context: Optional[dict] = None
    channel: Optional[str] = None
    explicit_mode: Optional[str] = None     # "talk" | "delegate" from
    #                                         interactionMode, if present
    ready_seen: bool = False
    initialized: bool = False

    def apply_init(self, init: dict) -> None:
        self.supported_finish_types = list(
            init.get("supportedFinishTypes") or [])
        self.subscribe_to_tracing = bool(
            init.get("subscribeToTracingEvents", True))
        self.history = list(init.get("history") or [])
        cc = init.get("contactContext")
        if isinstance(cc, dict):
            self.contact_context = cc
            self.channel = cc.get("channel")
        im = init.get("interactionMode")
        if im == "HANDOFF":
            self.explicit_mode = "talk"
        elif im == "DELEGATE":
            self.explicit_mode = "delegate"
        self.state = INITIALIZED
        self.initialized = True

    def mark_ready(self) -> None:
        self.ready_seen = True
        if self.state != FINISHED:
            self.state = READY

    def mark_finished(self) -> None:
        self.state = FINISHED

    @property
    def mode(self) -> str:
        if self.explicit_mode is not None:
            return self.explicit_mode
        return "talk" if self.ready_seen else "delegate"


class SessionStore:
    """In-memory sessions keyed by ``contextId``. A Lambda deployment swaps
    this for a DynamoDB-backed store (see deploy notes); the interface is
    ``get_or_create`` / ``get``."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, context_id: str) -> tuple[Session, bool]:
        """Return ``(session, created)``. ``created`` is False for a repeated
        INIT_SESSION on a known contextId, which the kit answers like the
        first (Server guide: Reconnects)."""
        existing = self._sessions.get(context_id)
        if existing is not None:
            return existing, False
        session = Session(context_id=context_id)
        self._sessions[context_id] = session
        return session, True

    def get(self, context_id: str) -> Optional[Session]:
        return self._sessions.get(context_id)
