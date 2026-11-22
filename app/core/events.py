"""Channel-neutral input/output contract between transport adapters and the orchestrator.

Every adapter (REST/WebSocket UI, A2A for Amazon Connect, future audio pipeline) converts its wire format
into InputMessage and renders OutputEvent back. The engines never see transport details.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


class InputKind(str, Enum):
    TEXT = "text"          # driver utterance (typed, or final STT transcript)
    SILENCE = "silence"    # Connect <EMPTY_USER_INPUT>
    DTMF = "dtmf"          # keypad digits
    START = "start"        # agent speaks first (outbound call opening)


class InputMessage(BaseModel):
    session_id: str
    turn_id: str = Field(default_factory=lambda: new_id("t-"))
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)  # 32-hex W3C trace id
    parent_span_id: str | None = None
    kind: InputKind = InputKind.TEXT
    text: str = ""
    received_at: float = Field(default_factory=time.perf_counter)


class EventType(str, Enum):
    TEXT_CHUNK = "text_chunk"      # part of one response message (sentence-level)
    TEXT_DONE = "text_done"        # message complete (A2A lastChunk)
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    FINISH = "finish"              # COMPLETE / ESCALATE: conversation is over
    TURN_END = "turn_end"          # carries metrics + spans for the turn
    STAGE = "stage"                # live progress inside a turn (model calls, decision, rules); UI only, A2A ignores it
    ERROR = "error"


class OutputEvent(BaseModel):
    type: EventType
    session_id: str
    turn_id: str
    trace_id: str
    message_id: str | None = None
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    t: float = Field(default_factory=time.perf_counter)
