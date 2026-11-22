"""Per-turn TRACING_SPAN construction, built on the Starter_Kit's tracing.

Task 3.7 moved the bridge onto the kit's ``a2a_connect.tracing`` so its spans
carry the current contract's ``aws.connect.span.message`` events with the
``aws.connect.span.message.role``, ``.type``, ``.participant`` and ``.content``
attributes (the shape the Tester's TRACE-06 to TRACE-08 require). The bridge
used to emit ``gen_ai.message.role`` / ``aws.connect.participant`` on message
events and ``gen_ai.tool.message`` on tool events, which would fail those
checks; delegating to the kit's ``TraceBuilder`` fixes that in one place.

This module keeps the small ``TurnTrace`` API the bridge's ``server.py`` drives
(``start_tool`` / ``finish_tool`` / ``finish``) and adapts it onto the kit
builder, which wants a ``ToolCall`` / ``ModelCall`` per call. Trace and span
IDs, the OTLP/JSON envelope and the message-event shape all come from the kit.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from a2a_connect import tracing as kit_tracing
from a2a_connect.backends.base import ModelCall, ToolCall

AGENT_NAME = "anycompany-claims-voice"
SERVICE_NAME = AGENT_NAME

# Kept for the bridge's unit tests and any caller that referenced them; the kit
# uses the same OTLP SpanKind / status integers.
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


def _hex(n_bytes: int) -> str:
    return os.urandom(n_bytes).hex()


def now_ns() -> int:
    return time.time_ns()


# Trace-context parsing stays identical to the kit's (same W3C format).
parse_traceparent = kit_tracing.parse_traceparent
parse_tracestate_parent = kit_tracing.parse_tracestate_parent


@dataclass
class _ToolInProgress:
    """A tool call the bridge opened with start_tool and will close later."""

    name: str
    tool_id: str
    arguments: str
    start_ns: int
    result: str = ""
    ok: bool = True
    end_ns: Optional[int] = None


class TurnTrace:
    """Collects one turn's spans and renders the TRACING_SPAN via the kit.

    The bridge drives this: ``start_tool`` when Nova asks for a tool,
    ``finish_tool`` when it returns, and ``finish`` once the turn's audio is
    flushed. Every turn uses the trace id and parent span Connect sent on the
    upgrade; ``server.py`` passes them to the constructor.
    """

    def __init__(self, trace_id: str, parent_span_id: Optional[str], model_id: str) -> None:
        self.trace_id = trace_id
        self.parent_span_id = parent_span_id
        self.model_id = model_id
        self.start_ns = now_ns()
        self._tools: List[_ToolInProgress] = []

    def start_tool(self, name: str, tool_id: str, arguments: str) -> _ToolInProgress:
        span = _ToolInProgress(name=name, tool_id=tool_id, arguments=arguments, start_ns=now_ns())
        self._tools.append(span)
        return span

    def finish_tool(self, span: _ToolInProgress, result: str, ok: bool) -> None:
        span.result = result
        span.ok = ok
        span.end_ns = now_ns()

    def finish(self, *, caller_text: str, agent_text: str,
               input_tokens: int, output_tokens: int) -> dict:
        """Render the OTLP/JSON document ``{"otlp": {"resourceSpans": [...]}}``."""
        end_ns = now_ns()
        builder = kit_tracing.TraceBuilder(
            agent_name=AGENT_NAME,
            trace_id=self.trace_id,
            parent_span_id=self.parent_span_id,
            service_name=SERVICE_NAME,
        )
        builder._start_ns = self.start_ns
        builder.set_input(caller_text, participant="user")
        builder.set_output(agent_text)
        # One chat span for the model call, with token counts on it.
        builder.add_model_call(ModelCall(
            model=self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            start_ns=self.start_ns,
            end_ns=end_ns,
        ))
        # One execute_tool span per tool call. The kit records the tool id and
        # the input/result as role input/output message events.
        for t in self._tools:
            builder.add_tool_call(ToolCall(
                name=t.name,
                input=_maybe_json(t.arguments),
                result=_maybe_json(t.result),
                start_ns=t.start_ns,
                end_ns=t.end_ns if t.end_ns is not None else now_ns(),
                error=None if t.ok else "tool failed",
            ))
        doc = builder.build()
        _annotate_model_and_tool_ids(doc, self.model_id, self._tools)
        return doc


def _maybe_json(value: str):
    """Pass through already-parsed structures; parse a JSON string; else keep
    the raw string. The kit json.dumps()es whatever it is given, so a dict
    round-trips and a plain string stays a string."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _annotate_model_and_tool_ids(doc: dict, model_id: str, tools: List[_ToolInProgress]) -> None:
    """Add the bridge's extra attributes the kit builder does not set:

    * ``gen_ai.request.model`` on the root (the bridge exposed it for operators)
    * ``aws.connect.tool.id`` on each execute_tool span (Nova's toolUseId).

    These are additive attributes; they do not change the message-event shape
    the trace checks read.
    """
    spans = doc["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    if spans:
        spans[0]["attributes"].append(
            {"key": "gen_ai.request.model", "value": {"stringValue": model_id}})
    tool_spans = [s for s in spans if s.get("name", "").startswith("execute_tool ")]
    for span, tool in zip(tool_spans, tools):
        span["attributes"].append(
            {"key": "aws.connect.tool.id", "value": {"stringValue": tool.tool_id}})
