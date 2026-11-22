# SPDX-License-Identifier: MIT-0
"""The backend interface: one async method that answers one turn.

A backend receives a :class:`Turn` and yields a stream of semantic events:
:class:`TextChunk` (words of the response), :class:`ToolCall` (a tool was run,
with its input, result and timing), :class:`ModelCall` (an LLM call, with token
counts) and an optional :class:`Finish` (end the whole conversation).

The kit turns that stream into protocol frames and trace spans in the one
correct order. A backend cannot open two tasks, forget a ``lastChunk`` or put
FINISH on the wrong turn, because it never touches the wire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator, Optional, Protocol, Union, runtime_checkable


@dataclass
class Turn:
    """Everything a backend needs to answer one customer turn."""

    context_id: str
    task_id: str
    mode: str                       # "delegate" | "talk"
    channel: Optional[str]          # "voice" | "chat" | None
    input_text: str                 # the turn text, DTMF digits, or
    #                                 "<EMPTY_USER_INPUT>"
    history: list[dict] = field(default_factory=list)
    contact_context: Optional[dict] = None
    today: Optional[date] = None
    is_dtmf: bool = False
    is_empty_input: bool = False


@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCall:
    name: str
    input: Any
    result: Any
    start_ns: int
    end_ns: int
    error: Optional[str] = None


@dataclass
class ModelCall:
    model: str
    input_tokens: int
    output_tokens: int
    start_ns: int
    end_ns: int


@dataclass
class Finish:
    type: str                       # "COMPLETE" | "ESCALATE"
    reason: str


BackendEvent = Union[TextChunk, ToolCall, ModelCall, Finish]


@runtime_checkable
class Backend(Protocol):
    """A partner implements this. See :mod:`a2a_connect.backends.echo` for the
    smallest working example and the README for a minimal custom backend."""

    name: str

    def respond(self, turn: Turn) -> AsyncIterator[BackendEvent]:
        """Answer one turn. An async generator yielding BackendEvent values.

        Raising from here (or from the async iteration) is turned into a
        TASK_STATE_FAILED terminal with a structured error by the kit.
        """
        ...
