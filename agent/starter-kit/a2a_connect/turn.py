# SPDX-License-Identifier: MIT-0
"""Run the backend for one turn and produce the ordered frame sequence.

The kit, not the backend, owns frame order. For a normal turn that is:

1. one or more TEXT_RESPONSE_CHUNK artifacts (first chunk ``append`` false,
   later chunks ``append`` true, the last chunk of an artifact ``lastChunk``
   true);
2. exactly one TRACING_SPAN artifact on the turn's ``taskId``, after the last
   response chunk and before the terminal status (unless tracing is off);
3. exactly one terminal statusUpdate; FINISH only on the last turn, only in
   talk mode, only with a supported type.

A backend failure becomes a TASK_STATE_FAILED terminal with a structured error.
Each fault flag perturbs exactly one of these steps.

The turn runner is transport-agnostic: it yields ``(rpc_id, frame)`` pairs as an
async generator. The WebSocket transport pushes each; the HTTP transport
collects artifacts and the terminal status into a Task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

from . import protocol as P
from .backends.base import Finish, ModelCall, TextChunk, ToolCall, Turn
from .backends.echo import EchoFailure
from .tracing import TraceBuilder

log = logging.getLogger("a2a_connect.turn")


class _SkipBackend(Exception):
    """Internal sentinel: skip running the backend (used by fail-on-empty)."""


class TurnRunner:
    def __init__(self, backend, faults, agent_name: str, service_name: str,
                 stream_delay_ms: int = 0) -> None:
        self.backend = backend
        self.faults = faults
        self.agent_name = agent_name
        self.service_name = service_name
        self.stream_delay_ms = stream_delay_ms

    def _has(self, name: str) -> bool:
        return self.faults.has(name)

    async def run(self, turn: Turn, session, *, trace_id: str,
                  parent_span_id: Optional[str],
                  rpc_id, interrupt_event: Optional[asyncio.Event] = None,
                  emit_trace: bool = True) -> AsyncIterator[tuple]:
        """Yield ``(rpc_id, frame)`` for one turn."""
        task_id = turn.task_id
        context_id = turn.context_id
        builder = TraceBuilder(self.agent_name, trace_id, parent_span_id,
                               self.service_name, faults=self.faults)
        builder.set_input(turn.input_text, participant="user")

        # overlap-tasks is applied just before the terminal status (see below);
        # the response itself always uses the turn's task.
        response_task_id = task_id

        response_text_parts: list[str] = []
        finish: Optional[Finish] = None
        interrupted = False
        failure: Optional[dict] = None
        artifact_id = P.new_id()
        first_chunk = True

        # fail-on-empty: answer <EMPTY_USER_INPUT> with a failed turn.
        force_fail_empty = self._has("fail-on-empty") and turn.is_empty_input

        # Collect backend events first so we know the full output for the
        # trace, and so reordering faults can place the trace correctly. Text
        # chunks still stream (with --stream-delay-ms) unless a reordering
        # fault needs them buffered.
        stream_live = not self._has("trace-before-response")

        # trace-before-response: emit the (empty-output) trace now, before the
        # response. We still need the output in it, so build a provisional
        # trace with whatever we collect; the simplest faithful deviation is
        # to emit the trace frame ahead of the response frames.
        pending_response_frames: list[tuple] = []

        if force_fail_empty:
            failure = {"errorCode": 500,
                       "errorReason": "Fault: fail on empty input",
                       "errorCategory": "INTERNAL_ERROR"}

        try:
            if force_fail_empty:
                raise _SkipBackend
            async for event in self.backend.respond(turn):
                if interrupt_event is not None and interrupt_event.is_set():
                    interrupted = True
                    break
                if isinstance(event, TextChunk):
                    if self._has("no-text"):
                        continue
                    words = event.text.split(" ")
                    for i, word in enumerate(words):
                        if interrupt_event is not None and interrupt_event.is_set():
                            interrupted = True
                            break
                        is_first = first_chunk
                        chunk_text = word if i == len(words) - 1 else word + " "
                        # The trace's output records what was actually sent, so an
                        # interrupted turn reports only the part the customer got.
                        response_text_parts.append(chunk_text)
                        last = (i == len(words) - 1) and not self._has("no-last-chunk")
                        frame = P.text_chunk_artifact(
                            rpc_id, response_task_id, context_id, artifact_id,
                            chunk_text, append=not is_first, last_chunk=last)
                        first_chunk = False
                        if stream_live:
                            yield (rpc_id, frame)
                            if self.stream_delay_ms:
                                await asyncio.sleep(self.stream_delay_ms / 1000.0)
                        else:
                            pending_response_frames.append((rpc_id, frame))
                    if interrupted:
                        break
                elif isinstance(event, ToolCall):
                    builder.add_tool_call(event)
                elif isinstance(event, ModelCall):
                    builder.add_model_call(event)
                elif isinstance(event, Finish):
                    finish = event
        except _SkipBackend:
            pass
        except EchoFailure as exc:
            failure = {"errorCode": exc.code, "errorReason": exc.reason,
                       "errorCategory": exc.category}
        except Exception as exc:  # backend error -> structured task failure
            log.exception("backend raised")
            failure = {"errorCode": 500, "errorReason": str(exc),
                       "errorCategory": "INTERNAL_ERROR"}

        builder.set_output("".join(response_text_parts))

        # Trace every turn whatever INIT_SESSION's subscribeToTracingEvents says:
        # the trace requirement covers every turn of every collaboration, and
        # Connect disables an endpoint that does not send it.
        tracing_on = emit_trace and not self._has("no-trace")

        # ---- Interruption path -------------------------------------------
        if interrupted:
            if tracing_on:
                async for f in self._emit_trace(builder, task_id, context_id, rpc_id):
                    yield f
            if self._has("interrupt-fail"):
                yield (rpc_id, P.terminal_status(
                    rpc_id, task_id, context_id, P.TASK_STATE_FAILED,
                    error={"errorCode": 500, "errorReason": "interrupted",
                           "errorCategory": "INTERNAL_ERROR"}))
            else:
                yield (rpc_id, P.terminal_status(
                    rpc_id, task_id, context_id, P.TASK_STATE_COMPLETED))
            return

        # ---- Failure path -------------------------------------------------
        if failure is not None:
            if tracing_on:
                async for f in self._emit_trace(builder, task_id, context_id, rpc_id):
                    yield f
            if self._has("failed-no-error-part"):
                yield (rpc_id, P.terminal_status(
                    rpc_id, task_id, context_id, P.TASK_STATE_FAILED))
            else:
                yield (rpc_id, P.terminal_status(
                    rpc_id, task_id, context_id, P.TASK_STATE_FAILED,
                    error=failure))
            return

        # ---- trace-before-response: trace, then buffered response --------
        if not stream_live:
            if tracing_on:
                async for f in self._emit_trace(builder, task_id, context_id, rpc_id):
                    yield f
            for f in pending_response_frames:
                yield f
        else:
            # Normal order: trace after the response chunks.
            if tracing_on:
                async for f in self._emit_trace(builder, task_id, context_id, rpc_id):
                    yield f

        # ---- FINISH decision ---------------------------------------------
        finish_tuple = self._decide_finish(finish, turn, session)

        terminal_context = context_id
        if self._has("wrong-context-id"):
            terminal_context = P.new_id()

        if self._has("no-terminal"):
            return

        # overlap-tasks: while this turn's task is still open, send a frame for
        # a different task (a non-terminal WORKING status). WebSocket only: an
        # HTTP turn is one request, so there is nothing to overlap. Only the
        # WebSocket transport passes an interrupt event.
        if self._has("overlap-tasks") and interrupt_event is not None:
            yield (rpc_id, P.terminal_status(
                rpc_id, P.new_id(), context_id, P.TASK_STATE_WORKING))

        if finish_tuple and self._has("finish-as-artifact"):
            yield (rpc_id, P.finish_message(rpc_id, context_id,
                                            finish_tuple[0], finish_tuple[1]))
            yield (rpc_id, P.terminal_status(
                rpc_id, task_id, terminal_context, P.TASK_STATE_COMPLETED))
        else:
            yield (rpc_id, P.terminal_status(
                rpc_id, task_id, terminal_context, P.TASK_STATE_COMPLETED,
                finish=finish_tuple))

        # The two post-terminal faults model a pushed frame after the terminal,
        # which only exists on WebSocket (an HTTP task is one response).
        ws_turn = interrupt_event is not None
        if finish_tuple:
            session.mark_finished()
            if self._has("finish-then-more") and ws_turn:
                yield (rpc_id, P.text_chunk_artifact(
                    rpc_id, task_id, context_id, P.new_id(),
                    "extra", append=False, last_chunk=True))

        if not finish_tuple and self._has("frames-after-terminal") and ws_turn:
            yield (rpc_id, P.text_chunk_artifact(
                rpc_id, task_id, context_id, P.new_id(),
                "extra", append=False, last_chunk=True))

    def _decide_finish(self, finish: Optional[Finish], turn: Turn,
                       session) -> Optional[tuple]:
        """Return (type, reason) or None, applying mode and fault rules."""
        if self._has("finish-every-turn") and turn.mode == "talk":
            return ("COMPLETE", "Fault: finish on every turn")
        if self._has("finish-in-delegate") and turn.mode == "delegate":
            return ("COMPLETE", "Fault: finish in delegate mode")

        if finish is None:
            return None
        if turn.mode != "talk":
            return None
        if self._has("never-finish"):
            return None

        ftype = finish.type
        reason = finish.reason
        if self._has("finish-bad-type"):
            return ("DONE", reason)

        supported = session.supported_finish_types or []
        if ftype not in supported:
            return ("COMPLETE", f"Requested {ftype}, not supported: {reason}")
        return (ftype, reason)

    async def _emit_trace(self, builder: TraceBuilder, task_id, context_id,
                          rpc_id) -> AsyncIterator[tuple]:
        otlp = builder.build()
        if self._has("trace-bad-envelope"):
            payload = {"resourceSpans": otlp["otlp"]["resourceSpans"]}
            yield (rpc_id, P.trace_artifact(
                rpc_id, task_id, context_id, P.new_id(),
                payload, bad_envelope=True))
        else:
            yield (rpc_id, P.trace_artifact(
                rpc_id, task_id, context_id, P.new_id(), otlp))
