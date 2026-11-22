# SPDX-License-Identifier: MIT-0
"""The echo backend: deterministic, no AWS calls.

Its behavior is fixed by the conformance catalog's echo table, because the
Tester's cross-run checks expect exactly these responses. One artifact per
turn, one chunk per word (the kit applies ``--stream-delay-ms`` between
chunks). ``lookup`` runs one tool call; ``fail`` fails the turn; ``goodbye`` and
``talk to a person`` finish the conversation in talk mode only.
"""
from __future__ import annotations

import re
import time
from typing import AsyncIterator

from .base import Backend, BackendEvent, Finish, TextChunk, ToolCall, Turn

_NUMBER = re.compile(r"\d+")

_LONG_STORY = (
    "Once upon a time there was a small package that set out on a long journey "
    "across many sorting centers and trucks and planes to reach a customer who "
    "had been waiting patiently for a very long while and wondered each day "
    "whether the package would arrive before the weekend or perhaps a little "
    "later than that if the weather turned bad along the route somewhere"
)  # 60 words of filler.


class EchoBackend:
    name = "echo"

    async def respond(self, turn: Turn) -> AsyncIterator[BackendEvent]:
        text = turn.input_text or ""
        lower = text.lower()
        talk = turn.mode == "talk"

        if turn.is_dtmf:
            yield _words(f"You entered {text}.")
            return

        if turn.is_empty_input or text == "<EMPTY_USER_INPUT>":
            yield _words("Are you still there?")
            return

        if "fail" in lower:
            # Signal a turn failure. The kit emits TASK_STATE_FAILED with the
            # structured error (Samples A.6) from this exception.
            raise EchoFailure(424, "DEPENDENCY_FAILURE",
                              "Simulated dependency failure")

        if "lookup" in lower:
            m = _NUMBER.search(text)
            start = time.time_ns()
            if m:
                number = m.group(0)
                result = {"order": number, "status": "shipped"}
                end = time.time_ns()
                yield ToolCall(name="lookup_order", input={"query": text},
                               result=result, start_ns=start, end_ns=end)
                yield _words(f"Order {number} has shipped.")
            else:
                result = {"order": None, "status": "not_found"}
                end = time.time_ns()
                yield ToolCall(name="lookup_order", input={"query": text},
                               result=result, start_ns=start, end_ns=end)
                yield _words("I could not find an order number.")
            return

        if talk and "goodbye" in lower:
            yield _words("Goodbye!")
            yield Finish(type="COMPLETE",
                         reason="Customer ended the conversation")
            return

        if talk and "talk to a person" in lower:
            yield _words("I'll connect you with a person.")
            yield Finish(type="ESCALATE", reason="Customer asked for a person")
            return

        if "long story" in lower:
            yield _words(_LONG_STORY)
            return

        yield _words(f"You said: {text}")


def _words(sentence: str) -> TextChunk:
    # The kit streams one chunk per word; it splits a TextChunk on whitespace.
    return TextChunk(text=sentence)


class EchoFailure(Exception):
    """Raised by the echo backend to request a TASK_STATE_FAILED turn."""

    def __init__(self, code: int, category: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.category = category
        self.reason = reason


def make_backend() -> Backend:
    return EchoBackend()
