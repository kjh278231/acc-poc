# SPDX-License-Identifier: MIT-0
"""Build the OTLP/JSON trace for one turn.

Every turn emits exactly one ``TRACING_SPAN`` artifact whose data part is
``{"otlp": {"resourceSpans": [...]}}``. The root is an ``invoke_agent <name>``
span (kind 2, SERVER) that adopts the ``traceId`` and parent span from the
request's ``traceparent``. It carries the turn input and output as
``aws.connect.span.message`` events. Each tool call adds an ``execute_tool
<name>`` span (kind 3) with the tool input and result as events; each model
call adds a ``chat <model>`` span (kind 3) with token counts.

The trace faults each perturb exactly one field so the Tester's trace checks can
be shown to fail.
"""
from __future__ import annotations

import secrets
import time
from typing import Optional

from .backends.base import ModelCall, ToolCall


def _attr(key: str, string: Optional[str] = None, intval: Optional[int] = None) -> dict:
    if intval is not None:
        return {"key": key, "value": {"intValue": str(intval)}}
    return {"key": key, "value": {"stringValue": string if string is not None else ""}}


def _message_event(ts_ns: int, role: str, participant: str, content: str,
                   msg_type: str = "TEXT_MESSAGE") -> dict:
    return {
        "name": "aws.connect.span.message",
        "timeUnixNano": str(ts_ns),
        "attributes": [
            _attr("aws.connect.span.message.role", role),
            _attr("aws.connect.span.message.type", msg_type),
            _attr("aws.connect.span.message.participant", participant),
            _attr("aws.connect.span.message.content", content),
        ],
    }


def new_trace_id() -> str:
    return secrets.token_hex(16)  # 32 hex chars


def new_span_id() -> str:
    return secrets.token_hex(8)  # 16 hex chars


class TraceBuilder:
    """Accumulates spans for one turn, then serializes an OTLP/JSON document."""

    def __init__(self, agent_name: str, trace_id: str,
                 parent_span_id: Optional[str], service_name: str,
                 faults=None) -> None:
        self.agent_name = agent_name
        self.trace_id = trace_id
        self.parent_span_id = parent_span_id
        self.service_name = service_name
        self.faults = faults
        self.root_span_id = new_span_id()
        self._child_spans: list[dict] = []
        self._start_ns = time.time_ns()
        self._root_input: Optional[tuple[str, str]] = None   # (participant, content)
        self._root_output: Optional[str] = None

    def _has(self, name: str) -> bool:
        return bool(self.faults) and self.faults.has(name)

    def set_input(self, content: str, participant: str = "user") -> None:
        self._root_input = (participant, content)

    def set_output(self, content: str) -> None:
        self._root_output = content

    def add_tool_call(self, call: ToolCall) -> None:
        import json

        events = [
            _message_event(call.start_ns, "input", "tool",
                           json.dumps(call.input, default=str)),
        ]
        if not self._has("tool-no-result"):
            events.append(
                _message_event(call.end_ns, "output", "tool",
                               json.dumps(call.result, default=str))
            )
        span = {
            "traceId": self.trace_id,
            "spanId": new_span_id(),
            "parentSpanId": self.root_span_id,
            "name": f"execute_tool {call.name}",
            "kind": 3,
            "startTimeUnixNano": str(call.start_ns),
            "endTimeUnixNano": str(call.end_ns),
            "attributes": [
                _attr("gen_ai.operation.name", "execute_tool"),
                _attr("gen_ai.tool.name", call.name),
            ],
            "events": events,
            "status": {"code": 2 if call.error else 1},
        }
        self._child_spans.append(span)

    def add_model_call(self, call: ModelCall) -> None:
        span = {
            "traceId": self.trace_id,
            "spanId": new_span_id(),
            "parentSpanId": self.root_span_id,
            "name": f"chat {call.model}",
            "kind": 3,
            "startTimeUnixNano": str(call.start_ns),
            "endTimeUnixNano": str(call.end_ns),
            "attributes": [
                _attr("gen_ai.operation.name", "chat"),
                _attr("gen_ai.request.model", call.model),
                _attr("gen_ai.usage.input_tokens", intval=call.input_tokens),
                _attr("gen_ai.usage.output_tokens", intval=call.output_tokens),
            ],
            "status": {"code": 1},
        }
        self._child_spans.append(span)

    def build(self) -> dict:
        end_ns = time.time_ns()
        start_ns = self._start_ns

        # trace-bad-times: end before start.
        if self._has("trace-bad-times"):
            end_ns = start_ns - 1_000_000_000

        # trace-new-trace-id: ignore the propagated id, mint a fresh one, and
        # drop the propagated parent.
        trace_id = self.trace_id
        parent = self.parent_span_id
        if self._has("trace-new-trace-id"):
            trace_id = new_trace_id()
            parent = None
            for span in self._child_spans:
                span["traceId"] = trace_id

        # trace-no-root: name it 'turn' with kind 1.
        if self._has("trace-no-root"):
            root_name = "turn"
            root_kind = 1
        else:
            root_name = f"invoke_agent {self.agent_name}"
            root_kind = 2

        events: list[dict] = []
        if self._root_input is not None and not self._has("trace-no-input-event"):
            events.append(_message_event(start_ns, "input",
                                         self._root_input[0], self._root_input[1]))
        if self._root_output is not None and not self._has("trace-no-output-event"):
            events.append(_message_event(end_ns, "output", "assistant",
                                         self._root_output))

        attributes = [
            _attr("gen_ai.agent.name", self.agent_name),
            _attr("gen_ai.operation.name", "invoke_agent"),
        ]
        # trace-huge-span: pad with a 20 KB attribute.
        if self._has("trace-huge-span"):
            attributes.append(_attr("padding", "x" * 20480))

        root = {
            "traceId": trace_id,
            "spanId": self.root_span_id,
            "name": root_name,
            "kind": root_kind,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": attributes,
            "events": events,
            "status": {"code": 1},
        }
        if parent:
            root["parentSpanId"] = parent

        spans = [root] + self._child_spans
        resource_spans = [{
            "resource": {
                "attributes": [_attr("service.name", self.service_name)],
            },
            "scopeSpans": [{
                "scope": {"name": f"a2a_connect.{self.agent_name}",
                          "version": "1.0.0"},
                "spans": spans,
            }],
        }]
        return {"otlp": {"resourceSpans": resource_spans}}


# ---- traceparent parsing -----------------------------------------------------

def parse_traceparent(header: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(trace_id, parent_span_id)`` from a W3C traceparent header.

    Format: ``version-traceid-spanid-flags`` (e.g.
    ``00-<32hex>-<16hex>-01``). Returns ``(None, None)`` when absent or
    malformed.
    """
    if not header:
        return None, None
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None, None
    _version, trace_id, span_id, _flags = parts
    if len(trace_id) != 32 or len(span_id) != 16:
        return None, None
    return trace_id, span_id


def parse_tracestate_parent(tracestate: Optional[str]) -> Optional[str]:
    """Connect may carry the parent span id in tracestate as
    ``connect=parentSpanId:<uuid>``. Returned as-is when present."""
    if not tracestate:
        return None
    for member in tracestate.split(","):
        member = member.strip()
        if member.startswith("connect="):
            value = member[len("connect="):]
            if value.startswith("parentSpanId:"):
                return value[len("parentSpanId:"):]
    return None
