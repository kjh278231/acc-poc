# SPDX-License-Identifier: MIT-0
"""An Amazon Bedrock Converse backend with tool use.

A small order-support agent. It calls the Bedrock Converse API, runs the
``lookup_order`` tool when the model asks for it, feeds the result back, and
yields the final text plus a ModelCall per model turn and a ToolCall per tool
run so the kit can build the trace.

The Bedrock client is injected (``make_backend`` builds a boto3 client lazily,
tests pass a stub), so the unit tests run with no network and no AWS calls.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Optional

from .base import BackendEvent, Finish, ModelCall, TextChunk, ToolCall, Turn

# Default model: Amazon Nova 2 Lite through the Global cross-Region inference profile
# (Requirement 4.4). Not the `us.` profile: it can route to us-east-2 or us-west-2, which
# the workshop's Region guard denies, so it fails under the participant role (found by
# the policy probe, 2026-10-18). The Global profile evaluates as Region "unspecified".
DEFAULT_MODEL_ID = "global.amazon.nova-2-lite-v1:0"

DEFAULT_SYSTEM_PROMPT = (
    "You are an order-support assistant for an online store. Help the customer "
    "with order status and basic questions. Use the lookup_order tool when you "
    "need an order's status. Keep replies short and factual. Never make up phone "
    "numbers, addresses, web links, store locations or other contact details. If "
    "the customer asks for a person, say only that you are connecting them with "
    "a member of the team."
)

# Reasoning-effort levels, low to high. The same knob maps to each model
# family's own Converse additionalModelRequestFields shape (Requirement 4.4).
REASONING_EFFORTS = ("low", "medium", "high")


def reasoning_fields(model_id: str, effort: Optional[str]) -> dict:
    """Build the ``additionalModelRequestFields`` for a reasoning effort.

    ``effort`` is one of ``low``, ``medium`` or ``high`` (or None to pass
    nothing). The field shape is per model family; only the three families this
    workshop ships are supported, each confirmed against the Bedrock docs:

    * Amazon Nova 2 (``*amazon.nova-2*``): ``reasoningConfig`` with
      ``maxReasoningEffort`` taking low|medium|high. (Requirement 4.4.)
    * Anthropic Claude Sonnet 5.5 and the other 5.x models (``*anthropic.*``):
      adaptive thinking with the effort inside a separate ``output_config``.
      Extended thinking (``thinking.type: enabled`` with ``budget_tokens``) is
      NOT supported on Sonnet 5.5 and returns a ValidationException, so the kit
      uses adaptive thinking.
      https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html
      (section "Using adaptive thinking with the Converse API").
    * OpenAI GPT (``*openai.*``): ``{"reasoning": {"effort": <effort>}}``
      (low|medium|high), carried through Converse's additionalModelRequestFields.
      https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-openai.html
      ("Include any other fields in the additionalModelRequestFields object").
      GPT-6 Luna rejects the Chat Completions name ``reasoning_effort`` with
      "Unknown parameter" (live check, 2026-10-18).

    A model id outside these families raises ``UnsupportedReasoningModel`` so a
    misconfiguration fails loudly rather than silently sending a field the model
    rejects. The caller may catch it and run without a reasoning field.
    """
    if not effort:
        return {}
    if effort not in REASONING_EFFORTS:
        raise ValueError(
            f"reasoning effort must be one of {REASONING_EFFORTS}, got {effort!r}")
    mid = model_id.lower()
    # Strip any cross-Region inference prefix (global./us./eu.) before matching.
    for prefix in ("global.", "us.", "eu.", "apac."):
        if mid.startswith(prefix):
            mid = mid[len(prefix):]
            break
    if "amazon.nova-2" in mid or mid.startswith("amazon.nova-2"):
        return {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}}
    if mid.startswith("anthropic."):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    if mid.startswith("openai."):
        return {"reasoning": {"effort": effort}}
    raise UnsupportedReasoningModel(
        f"reasoning effort is not supported for model {model_id!r}: only Amazon "
        f"Nova 2, Anthropic Claude and OpenAI GPT model families have a "
        f"confirmed Bedrock Converse reasoning shape")


class UnsupportedReasoningModel(ValueError):
    """Raised when a reasoning effort is requested for a model family whose
    Bedrock Converse reasoning shape the kit does not know."""

# Canned order data for the demo tool. Fictitious; no real customer data.
_CANNED_ORDERS = {
    "1234": {"order": "1234", "status": "shipped",
             "carrier": "ExampleShip", "eta": "2 days"},
    "5678": {"order": "5678", "status": "processing", "eta": "pending"},
}

_TOOL_SPEC = {
    "tools": [{
        "toolSpec": {
            "name": "lookup_order",
            "description": "Look up the status of an order by its order id.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string",
                                 "description": "The order identifier."}
                },
                "required": ["order_id"],
            }},
        }
    }]
}


def lookup_order(order_id: str) -> dict:
    """The demo tool. Canned data, no external calls."""
    return _CANNED_ORDERS.get(str(order_id),
                              {"order": str(order_id), "status": "not_found"})


class BedrockBackend:
    name = "bedrock"

    def __init__(self, client, model_id: str, system_prompt: str,
                 reasoning_effort: Optional[str] = None, *,
                 finish_phrase: str = "goodbye",
                 escalate_phrase: str = "talk to a person") -> None:
        self._client = client
        # In talk mode (handoff) the endpoint must end the conversation with
        # FINISH (Tester FIN-00): COMPLETE when the customer says the finish
        # phrase, ESCALATE when they ask for a person. Same rule as the strands
        # and echo backends.
        self._finish_phrase = finish_phrase.lower()
        self._escalate_phrase = escalate_phrase.lower()
        self._model_id = model_id
        self._system_prompt = system_prompt
        self._tools = {"lookup_order": lookup_order}
        # Precompute the reasoning additionalModelRequestFields once, failing
        # fast on an unsupported family rather than on the first turn.
        self._reasoning_effort = reasoning_effort
        self._extra_fields = reasoning_fields(model_id, reasoning_effort)

    def _finish_for(self, turn: Turn) -> Optional[Finish]:
        if turn.mode != "talk":
            return None  # delegate mode never sends FINISH (Tester FIN-04)
        lower = (turn.input_text or "").lower()
        if self._escalate_phrase and self._escalate_phrase in lower:
            return Finish(type="ESCALATE", reason="Customer asked for a person")
        if self._finish_phrase and self._finish_phrase in lower:
            return Finish(type="COMPLETE", reason="Customer ended the conversation")
        return None

    async def respond(self, turn: Turn) -> AsyncIterator[BackendEvent]:
        messages: list[dict] = _history_to_messages(turn.history)
        messages.append({"role": "user",
                         "content": [{"text": turn.input_text}]})

        max_rounds = 4
        for _ in range(max_rounds):
            start = time.time_ns()
            kwargs = dict(
                modelId=self._model_id,
                messages=messages,
                system=[{"text": self._system_prompt}],
                toolConfig=_TOOL_SPEC,
            )
            if self._extra_fields:
                kwargs["additionalModelRequestFields"] = self._extra_fields
            response = self._client.converse(**kwargs)
            end = time.time_ns()
            usage = response.get("usage", {}) or {}
            yield ModelCall(
                model=self._model_id,
                input_tokens=int(usage.get("inputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)),
                start_ns=start, end_ns=end)

            output = response.get("output", {}).get("message", {})
            content = output.get("content", []) or []
            messages.append({"role": "assistant", "content": content})

            tool_uses = [c["toolUse"] for c in content if "toolUse" in c]
            if not tool_uses:
                # Final answer: yield its text.
                for block in content:
                    if "text" in block and block["text"]:
                        yield TextChunk(text=block["text"])
                finish = self._finish_for(turn)
                if finish is not None:
                    yield finish
                return

            # Run each requested tool, feed results back, loop.
            tool_results = []
            for tu in tool_uses:
                name = tu["name"]
                tool_input = tu.get("input", {}) or {}
                t0 = time.time_ns()
                fn = self._tools.get(name)
                if fn is None:
                    result: Any = {"error": f"unknown tool {name}"}
                    err: Optional[str] = f"unknown tool {name}"
                else:
                    result = fn(**tool_input)
                    err = None
                t1 = time.time_ns()
                yield ToolCall(name=name, input=tool_input, result=result,
                               start_ns=t0, end_ns=t1, error=err)
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"json": result}],
                    }
                })
            messages.append({"role": "user", "content": tool_results})

        # Ran out of rounds without a final text answer.
        yield TextChunk(text="I was unable to complete that request.")


def _history_to_messages(history: list[dict]) -> list[dict]:
    """Map A2A history Message objects to Converse messages (text only)."""
    out: list[dict] = []
    for msg in history or []:
        role = "assistant" if msg.get("role") == "ROLE_AGENT" else "user"
        text_parts = [p.get("text", "") for p in msg.get("parts", [])
                      if "text" in p]
        text = " ".join(t for t in text_parts if t)
        if text:
            out.append({"role": role, "content": [{"text": text}]})
    return out


def make_backend(model_id: Optional[str] = None, region: Optional[str] = None,
                 system_prompt_file: Optional[str] = None, client=None,
                 reasoning_effort: Optional[str] = None):
    mid = model_id or DEFAULT_MODEL_ID
    if system_prompt_file:
        with open(system_prompt_file, "r", encoding="utf-8") as fh:
            prompt = fh.read().strip()
    else:
        prompt = DEFAULT_SYSTEM_PROMPT
    # Reasoning effort: the CLI flag wins, else the A2A_REASONING_EFFORT env
    # var (Requirement 4.4: configurable by config, env or CLI). Empty or unset
    # means no reasoning field.
    if reasoning_effort is None:
        import os
        reasoning_effort = os.environ.get("A2A_REASONING_EFFORT") or None
    if client is None:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=region)
    return BedrockBackend(client, mid, prompt, reasoning_effort=reasoning_effort)
