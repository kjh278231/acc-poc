# SPDX-License-Identifier: MIT-0
"""Fault flags for the Tester's negative suite.

Each ``--fault NAME`` makes the kit deviate in exactly one way, so the Tester
can prove each check catches its own failure. The kit refuses to start with any
fault unless it is bound to a loopback host, and logs every active fault at
startup. Unknown names make the kit exit non-zero.

This module is just the catalog of names plus a small holder. The deviations
themselves live where the behavior is: auth in :mod:`auth`, framing in
:mod:`turn`, transport quirks in the transport modules. Keeping the names in one
place means ``server.py`` can validate them and the holder can answer ``has()``.
"""
from __future__ import annotations

from dataclasses import dataclass

# Every fault name the catalog defines, with its one-line meaning (for the
# startup log and --help). The behavior is implemented at the named site.
FAULTS: dict[str, str] = {
    "auth-accept-any": "accept any or no bearer",
    "auth-reject-raw": "accept only the JSON-wrapped bearer form",
    "auth-reject-json": "accept only the raw bearer form",
    "no-ext-echo": "do not echo A2A-Extensions",
    "init-no-response": "never answer INIT_SESSION",
    "init-wrong-id": "answer INIT_SESSION with a different JSON-RPC id",
    "init-bad-shape": "omit the event-type metadata on INIT_SESSION_RESPONSE",
    "reject-repeat-init": "answer a repeated INIT_SESSION with statusCode 409",
    "greet-before-ready": "send a greeting turn right after INIT_SESSION_RESPONSE",
    "wait-ready-in-delegate": "never answer a turn until READY has been seen",
    "ws-delegate-stream": "in delegate mode on WebSocket, stream the turn instead of answering with a Task",
    "no-text": "send no response artifact",
    "no-last-chunk": "never set lastChunk on response artifacts",
    "wrong-context-id": "put a different contextId on the terminal status",
    "no-terminal": "never send the terminal status",
    "frames-after-terminal": "send one extra text chunk after the terminal status",
    "finish-every-turn": "FINISH COMPLETE on every talk-mode turn",
    "overlap-tasks": "open the next task before closing the current one",
    "fail-on-empty": "answer <EMPTY_USER_INPUT> with TASK_STATE_FAILED",
    "ignore-dtmf": "do not answer DTMF_INPUT_COMPLETE",
    "interrupt-fail": "close an interrupted task with TASK_STATE_FAILED",
    "no-trace": "send no trace",
    "trace-before-response": "send the trace before the response artifacts",
    "trace-bad-envelope": "send resourceSpans without the otlp key",
    "trace-bad-times": "set the root span end time before its start time",
    "trace-no-root": "name the root span 'turn' with kind 1",
    "trace-no-input-event": "omit the input message event",
    "trace-no-output-event": "omit the output message event",
    "tool-no-result": "omit the result event on execute_tool spans",
    "trace-new-trace-id": "ignore traceparent; mint a new trace id",
    "trace-huge-span": "pad the root span with a 20 KB attribute",
    "never-finish": "never send FINISH",
    "finish-bad-type": "send FINISH type DONE",
    "finish-as-artifact": "send FINISH as an artifact instead of on the status",
    "finish-then-more": "send one text chunk after FINISH",
    "finish-in-delegate": "FINISH COMPLETE in delegate mode",
    "ignore-unknown-method": "do not answer unknown JSON-RPC methods",
    "gettask-unknown-ok": "answer GetTask for an unknown id with a completed task",
    "failed-no-error-part": "send TASK_STATE_FAILED with no error part",
    "no-fragment-reassembly": "ignore inbound FRAGMENT frames",
    "bad-outbound-fragment": "number outbound fragment parts from 1 instead of 0",
    "http-task-never-completes": "HTTP: return a TASK_STATE_WORKING task forever",
    "http-wrong-id": "HTTP: answer with a different JSON-RPC id",
}


class UnknownFault(ValueError):
    pass


@dataclass
class Faults:
    """The set of active fault names, with a membership helper."""

    active: frozenset[str]

    @classmethod
    def from_names(cls, names: list[str]) -> "Faults":
        unknown = [n for n in names if n not in FAULTS]
        if unknown:
            raise UnknownFault(
                "unknown fault name(s): " + ", ".join(sorted(set(unknown)))
            )
        return cls(active=frozenset(names))

    def has(self, name: str) -> bool:
        return name in self.active

    def __bool__(self) -> bool:
        return bool(self.active)
