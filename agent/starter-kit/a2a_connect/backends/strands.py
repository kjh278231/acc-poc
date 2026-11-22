# SPDX-License-Identifier: MIT-0
"""A Strands Agents backend.

Strands Agents (https://pypi.org/project/strands-agents/) is an open-source SDK
that runs an agent loop against a model, calls tools, and emits OpenTelemetry
spans named ``invoke_agent <name>``, ``chat <model>`` and
``execute_tool <name>`` with ``gen_ai.*`` attributes. The kit's own
``tracing.py`` already reproduces exactly those span names and attributes, so
this backend does not touch OpenTelemetry at all: it drives a Strands ``Agent``,
reads the plain event dicts its ``stream_async`` iterator yields, and maps them
to the kit's semantic events (:class:`TextChunk`, :class:`ToolCall`,
:class:`ModelCall`, :class:`Finish`). The kit then builds the one correct frame
order and the trace.

Mapping (Strands stream event -> kit event):

* ``{"data": "<text delta>"}``           -> accumulated, yielded as one
  :class:`TextChunk` (the kit re-splits a chunk into words on the wire).
* a completed assistant message with a ``toolUse`` block, paired with the
  following ``{"message": ...}`` that carries the ``toolResult``
                                         -> :class:`ToolCall` (name, input,
  result, timing). This reproduces Strands' ``execute_tool <name>`` span.
* each model turn, from the final ``AgentResult.metrics`` usage
                                         -> :class:`ModelCall` (model, token
  counts). This reproduces Strands' ``chat <model>`` span.
* a configured finish phrase or an ``ESCALATE``/``COMPLETE`` signal in the
  final text (talk mode only)            -> :class:`Finish`.

The Strands dependency is optional and imported lazily. :func:`make_backend`
builds a real ``strands.Agent`` on a ``BedrockModel``; the backend class itself
consumes only plain dicts and an injected agent, so the unit tests drive it with
a fake model and never import Strands or call Bedrock.

Pinned dependency (say so in the deploy manifest): ``strands-agents==1.63.0``
(Requires-Python >= 3.10; pulls in ``boto3``, ``opentelemetry-api``,
``opentelemetry-sdk`` and ``pydantic``).
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator, Optional

from .base import BackendEvent, Finish, ModelCall, TextChunk, ToolCall, Turn

# Default model: Amazon Nova 2 Lite through the Global cross-Region inference
# profile (Requirement 4.4, D3). Not the ``us.`` profile, which can route to a
# Region the workshop's participant policy denies (see backends/bedrock.py).
DEFAULT_MODEL_ID = "global.amazon.nova-2-lite-v1:0"

DEFAULT_SYSTEM_PROMPT = (
    "You are an order-support assistant for an online store. Help the customer "
    "with order status and basic questions. Use the lookup_order tool when you "
    "need an order's status. Keep replies short and factual. Never make up phone "
    "numbers, addresses, web links, store locations or other contact details. If "
    "the customer asks for a person, say only that you are connecting them with "
    "a member of the team."
)

# Canned order data for the demo tool. Fictitious; no real customer data. Same
# shape as the bedrock backend's tool so both agents behave alike in the labs.
_CANNED_ORDERS = {
    "1234": {"order": "1234", "status": "shipped",
             "carrier": "ExampleShip", "eta": "2 days"},
    "5678": {"order": "5678", "status": "processing", "eta": "pending"},
}


def lookup_order(order_id: str) -> dict:
    """Look up the status of an order by its id. Canned data, no external calls.

    This is the one tool the Strands agent is given. Strands reads the type
    hints and docstring to build the tool's input schema, which the SDK then
    reports as ``gen_ai.tool.*`` on the ``execute_tool`` span.
    """
    return _CANNED_ORDERS.get(str(order_id),
                              {"order": str(order_id), "status": "not_found"})


def _text_of(message: dict) -> str:
    """Join the text blocks of a Strands/Bedrock message's content."""
    parts = []
    for block in (message or {}).get("content", []) or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _tool_uses(message: dict) -> list[dict]:
    out = []
    for block in (message or {}).get("content", []) or []:
        if isinstance(block, dict) and "toolUse" in block:
            out.append(block["toolUse"])
    return out


def _tool_results(message: dict) -> dict[str, Any]:
    """Map toolUseId -> the result content of a user message's toolResult blocks."""
    out: dict[str, Any] = {}
    for block in (message or {}).get("content", []) or []:
        if isinstance(block, dict) and "toolResult" in block:
            tr = block["toolResult"]
            out[tr.get("toolUseId")] = tr.get("content")
    return out


class StrandsBackend:
    """Drives a Strands ``Agent`` and maps its stream to kit events.

    ``agent`` is any object exposing ``stream_async(prompt) -> AsyncIterator``
    that yields Strands-style event dicts. In production this is a real
    ``strands.Agent``; in tests it is a tiny fake driven by a fake model, so no
    Strands import and no Bedrock call is needed to exercise the mapping.

    ``model_id`` is only used to label :class:`ModelCall` events (the
    ``chat <model>`` span's ``gen_ai.request.model``) when the stream's own
    events do not name a model.

    ``finish_phrase`` / ``escalate_phrase`` let talk-mode conversations end with
    a FINISH, matching the echo and bedrock backends' behaviour so the Tester's
    FIN checks pass. They are matched case-insensitively against the customer's
    turn text.
    """

    name = "strands"

    def __init__(self, agent, model_id: str,
                 *, finish_phrase: str = "goodbye",
                 escalate_phrase: str = "talk to a person") -> None:
        self._agent = agent
        self._model_id = model_id
        self._finish_phrase = finish_phrase.lower()
        self._escalate_phrase = escalate_phrase.lower()

    async def respond(self, turn: Turn) -> AsyncIterator[BackendEvent]:
        # Pending tool-use spans, keyed by toolUseId, waiting for their result.
        pending_tools: dict[str, dict] = {}
        text_parts: list[str] = []
        model_calls_emitted = False
        model_from_stream: Optional[str] = None
        loop_start = time.time_ns()

        async for event in self._agent.stream_async(turn.input_text):
            if not isinstance(event, dict):
                continue

            # Model named its request (optional; Strands does not always surface
            # this in a callback event, so AgentResult.metrics is the fallback).
            if event.get("model_id"):
                model_from_stream = event["model_id"]

            # Streamed text delta.
            data = event.get("data")
            if isinstance(data, str) and data:
                text_parts.append(data)

            # A completed message (assistant with toolUse, or user with
            # toolResult). Strands emits ``{"message": {...}}`` for each.
            message = event.get("message")
            if isinstance(message, dict):
                role = message.get("role")
                if role == "assistant":
                    for tu in _tool_uses(message):
                        tool_id = tu.get("toolUseId")
                        pending_tools[tool_id] = {
                            "name": tu.get("name"),
                            "input": tu.get("input", {}) or {},
                            "start_ns": time.time_ns(),
                        }
                elif role in ("user", "tool"):
                    results = _tool_results(message)
                    for tool_id, result in results.items():
                        pend = pending_tools.pop(tool_id, None)
                        if pend is None:
                            continue
                        yield ToolCall(
                            name=pend["name"], input=pend["input"],
                            result=result, start_ns=pend["start_ns"],
                            end_ns=time.time_ns())

            # Final result: carries metrics (token usage) and the last message.
            result = event.get("result")
            if result is not None:
                model_calls_emitted = True
                for mc in self._model_calls_from_result(
                        result, model_from_stream, loop_start):
                    yield mc

        # Any tool-use with no observed result still becomes a ToolCall so the
        # trace records the attempt (its result is empty).
        for pend in pending_tools.values():
            yield ToolCall(name=pend["name"], input=pend["input"], result=None,
                           start_ns=pend["start_ns"], end_ns=time.time_ns(),
                           error="no tool result observed")

        # If the stream never produced a final-result event (a bare fake), emit
        # one ModelCall so every turn has a ``chat`` span, as Strands would.
        if not model_calls_emitted:
            yield ModelCall(model=model_from_stream or self._model_id,
                            input_tokens=0, output_tokens=0,
                            start_ns=loop_start, end_ns=time.time_ns())

        text = "".join(text_parts)
        if text:
            yield TextChunk(text=text)

        finish = self._finish_for(turn)
        if finish is not None:
            yield finish

    def _model_calls_from_result(self, result: Any, model_from_stream,
                                 loop_start: int) -> list[ModelCall]:
        """Build ModelCall events from an AgentResult's accumulated usage.

        Strands accumulates usage across the loop's model invocations in
        ``result.metrics.accumulated_usage``. The contract only needs the token
        counts on a ``chat`` span, so one ModelCall carrying the accumulated
        counts is faithful. ``result`` may be a Strands ``AgentResult`` object
        or a plain dict (what a fake yields)."""
        metrics = _get(result, "metrics")
        usage = _get(metrics, "accumulated_usage") if metrics is not None else None
        in_tok = _usage_int(usage, "inputTokens")
        out_tok = _usage_int(usage, "outputTokens")
        model = model_from_stream or self._model_id
        return [ModelCall(model=model, input_tokens=in_tok,
                          output_tokens=out_tok, start_ns=loop_start,
                          end_ns=time.time_ns())]

    def _finish_for(self, turn: Turn) -> Optional[Finish]:
        if turn.mode != "talk":
            return None
        lower = (turn.input_text or "").lower()
        if self._escalate_phrase and self._escalate_phrase in lower:
            return Finish(type="ESCALATE", reason="Customer asked for a person")
        if self._finish_phrase and self._finish_phrase in lower:
            return Finish(type="COMPLETE",
                          reason="Customer ended the conversation")
        return None


def _get(obj: Any, attr: str) -> Any:
    """Read ``attr`` from an object or a mapping (AgentResult or a fake dict)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def _usage_int(usage: Any, key: str) -> int:
    val = _get(usage, key)
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def make_backend(agent=None, model_id: Optional[str] = None,
                 region: Optional[str] = None,
                 system_prompt_file: Optional[str] = None,
                 finish_phrase: str = "goodbye",
                 escalate_phrase: str = "talk to a person"):
    """Build a Strands backend.

    In production this imports Strands lazily and builds a real ``Agent`` on a
    ``BedrockModel``. Tests pass ``agent`` directly (a fake driven by a fake
    model), so Strands is never imported and no Bedrock call is made.
    """
    mid = model_id or DEFAULT_MODEL_ID
    if agent is None:
        if system_prompt_file:
            with open(system_prompt_file, "r", encoding="utf-8") as fh:
                prompt = fh.read().strip()
        else:
            prompt = DEFAULT_SYSTEM_PROMPT
        # Lazy import: the kit's base tests do not install strands-agents.
        from strands import Agent, tool
        from strands.models import BedrockModel

        model = BedrockModel(model_id=mid, region_name=region)
        # Strands registers only @tool functions (AgentTool instances). A plain
        # function is skipped with a log warning and the model never sees the
        # tool: on AgentCore, Nova 2 Lite then wrote 'lookup_order(order_id=...)'
        # as text (Connect contact, 2026-10-18). tool() wraps it, reading the
        # spec from the type hints and the docstring.
        # callback_handler=None: Strands' default handler prints every streamed
        # token and tool name to stdout, which would put the conversation in the
        # runtime's logs. The backend reads the stream itself.
        agent = Agent(model=model, tools=[tool(lookup_order)], system_prompt=prompt,
                      name="strands-reference-agent", callback_handler=None)
    return StrandsBackend(agent, mid, finish_phrase=finish_phrase,
                          escalate_phrase=escalate_phrase)
