# SPDX-License-Identifier: MIT-0
"""Parse inbound A2A frames and build outbound ones.

The event type is carried in message/artifact metadata under
``<EXT>/eventType``. Connect sends JSON-RPC requests (``method`` + ``params``);
the agent's direct reply echoes the request ``id``. Streamed events ride in a
JSON-RPC result whose value is exactly one of ``message``, ``task``,
``statusUpdate`` or ``artifactUpdate``.

Everything here is pure: it turns dicts into dicts. No I/O, no transport.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import EVENT_TYPE_KEY, EXT

# Inbound event types (Connect -> agent).
INIT_SESSION = "INIT_SESSION"
CHANNEL_STATE = "CHANNEL_STATE"
INTERRUPTION = "INTERRUPTION"
DTMF_INPUT_COMPLETE = "DTMF_INPUT_COMPLETE"
FRAGMENT = "FRAGMENT"
ERROR = "ERROR"

# Outbound event types (agent -> Connect).
INIT_SESSION_RESPONSE = "INIT_SESSION_RESPONSE"
TEXT_RESPONSE_CHUNK = "TEXT_RESPONSE_CHUNK"
TRACING_SPAN = "TRACING_SPAN"
FINISH = "FINISH"

EMPTY_USER_INPUT = "<EMPTY_USER_INPUT>"

# Task states Connect recognizes for a terminal status.
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Inbound:
    """A parsed inbound JSON-RPC frame from Connect."""

    rpc_id: Any
    method: Optional[str]
    message: dict
    event_type: Optional[str]
    kind: str  # one of: init_session, channel_state, text_turn, dtmf,
    #                      interruption, fragment, error, get_task, unknown
    # Convenience extracts, populated by kind:
    context_id: Optional[str] = None
    text: Optional[str] = None
    data: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _event_type(message: dict) -> Optional[str]:
    md = message.get("metadata") or {}
    return md.get(EVENT_TYPE_KEY)


def _first_data_part(message: dict) -> dict:
    for part in message.get("parts", []) or []:
        if "data" in part and isinstance(part["data"], dict):
            return part["data"]
    return {}


def _first_text_part(message: dict) -> Optional[str]:
    for part in message.get("parts", []) or []:
        if "text" in part:
            return part["text"]
    return None


def parse(frame: dict) -> Inbound:
    """Parse one JSON-RPC request frame into an Inbound.

    Does not raise on unknown shapes; sets ``kind='unknown'`` so the caller can
    answer with a JSON-RPC error. GetTask is recognized by method name.
    """
    rpc_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params") or {}

    if method == "GetTask":
        return Inbound(
            rpc_id=rpc_id, method=method, message={}, event_type=None,
            kind="get_task", data=params, raw=frame,
        )

    message = params.get("message") or {}
    ev = _event_type(message)
    context_id = message.get("contextId")
    data = _first_data_part(message)

    if method not in (None, "SendMessage"):
        # Any method other than SendMessage/GetTask is unknown to this kit.
        return Inbound(rpc_id, method, message, ev, "unknown",
                       context_id=context_id, data=data, raw=frame)

    if ev == INIT_SESSION:
        kind = "init_session"
    elif ev == CHANNEL_STATE:
        kind = "channel_state"
    elif ev == DTMF_INPUT_COMPLETE:
        kind = "dtmf"
    elif ev == INTERRUPTION:
        kind = "interruption"
    elif ev == FRAGMENT:
        kind = "fragment"
    elif ev == ERROR:
        kind = "error"
    else:
        # No event type: a plain text turn (Samples A.3), or audio (ignored).
        kind = "text_turn"

    text = _first_text_part(message)
    return Inbound(rpc_id, method, message, ev, kind,
                   context_id=context_id, text=text, data=data, raw=frame)


# ---- Outbound builders -------------------------------------------------------

def result_frame(rpc_id: Any, value_key: str, value: dict) -> dict:
    """A JSON-RPC result whose value is one of message|task|statusUpdate|
    artifactUpdate."""
    return {"jsonrpc": "2.0", "id": rpc_id, "result": {value_key: value}}


def error_frame(rpc_id: Any, code: int, message: str, reason: str) -> dict:
    """A JSON-RPC error object (Contract: request level; A2A v1.0 ErrorInfo)."""
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {
            "code": code,
            "message": message,
            "data": {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": reason,
                "domain": "a2a-protocol.org",
            },
        },
    }


def init_session_response(rpc_id: Any, context_id: str,
                          include_event_type: bool = True) -> dict:
    """INIT_SESSION_RESPONSE (Samples A.2). Text-only: no audio override."""
    metadata = {}
    if include_event_type:
        metadata[EVENT_TYPE_KEY] = INIT_SESSION_RESPONSE
    message = {
        "messageId": new_id(),
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [{
            "data": {"initSessionResponse": {"statusCode": "200"}},
            "mediaType": "application/json",
        }],
        "extensions": [EXT],
        "metadata": metadata,
    }
    return result_frame(rpc_id, "message", message)


def init_session_rejected(rpc_id: Any, context_id: str) -> dict:
    """Used only by the reject-repeat-init fault: statusCode 409."""
    message = {
        "messageId": new_id(),
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [{
            "data": {"initSessionResponse": {"statusCode": "409"}},
            "mediaType": "application/json",
        }],
        "extensions": [EXT],
        "metadata": {EVENT_TYPE_KEY: INIT_SESSION_RESPONSE},
    }
    return result_frame(rpc_id, "message", message)


def text_chunk_artifact(rpc_id: Any, task_id: str, context_id: str,
                        artifact_id: str, text: str, append: bool,
                        last_chunk: bool) -> dict:
    """A TEXT_RESPONSE_CHUNK artifactUpdate (Samples A.4)."""
    return result_frame(rpc_id, "artifactUpdate", {
        "taskId": task_id,
        "contextId": context_id,
        "artifact": {
            "artifactId": artifact_id,
            "parts": [{"text": text, "mediaType": "text/plain"}],
            "metadata": {EVENT_TYPE_KEY: TEXT_RESPONSE_CHUNK},
        },
        "append": append,
        "lastChunk": last_chunk,
    })


def trace_artifact(rpc_id: Any, task_id: str, context_id: str,
                   artifact_id: str, otlp: dict,
                   bad_envelope: bool = False) -> dict:
    """A TRACING_SPAN artifactUpdate (Samples A.10). lastChunk true."""
    tracing_span = otlp if bad_envelope else {"otlp": otlp["otlp"]}
    return result_frame(rpc_id, "artifactUpdate", {
        "taskId": task_id,
        "contextId": context_id,
        "artifact": {
            "artifactId": artifact_id,
            "parts": [{
                "data": {"tracingSpan": tracing_span},
                "mediaType": "application/json",
            }],
            "metadata": {EVENT_TYPE_KEY: TRACING_SPAN},
        },
        "append": False,
        "lastChunk": True,
    })


def _finish_part(finish_type: str, reason: str) -> dict:
    return {
        "data": {"finish": {"type": finish_type, "reason": reason}},
        "mediaType": "application/json",
    }


def terminal_status(rpc_id: Any, task_id: str, context_id: str, state: str,
                    finish: Optional[tuple[str, str]] = None,
                    error: Optional[dict] = None,
                    timestamp: Optional[str] = None) -> dict:
    """A terminal statusUpdate (Samples A.5 / A.6).

    ``finish`` is (type, reason); it rides as a finish data part on
    status.message with event type FINISH. ``error`` is a structured error
    dict; it rides with event type ERROR on a FAILED status.
    """
    status: dict = {"state": state}
    if timestamp:
        status["timestamp"] = timestamp
    if finish is not None:
        status["message"] = {
            "messageId": new_id(),
            "role": "ROLE_AGENT",
            "parts": [_finish_part(finish[0], finish[1])],
            "metadata": {EVENT_TYPE_KEY: FINISH},
        }
    elif error is not None:
        status["message"] = {
            "messageId": new_id(),
            "role": "ROLE_AGENT",
            "parts": [{"data": {"error": error}, "mediaType": "application/json"}],
            "metadata": {EVENT_TYPE_KEY: ERROR},
        }
    return result_frame(rpc_id, "statusUpdate", {
        "taskId": task_id,
        "contextId": context_id,
        "status": status,
    })


def finish_message(rpc_id: Any, context_id: str,
                   finish_type: str, reason: str) -> dict:
    """FINISH as a standalone message (WebSocket form, used by the
    finish-as-artifact fault's counterpart and allowed by the contract)."""
    return result_frame(rpc_id, "message", {
        "messageId": new_id(),
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [_finish_part(finish_type, reason)],
        "extensions": [EXT],
        "metadata": {EVENT_TYPE_KEY: FINISH},
    })


def task_envelope(task_id: str, context_id: str, status: dict,
                  artifacts: list[dict]) -> dict:
    """An A2A Task object for an HTTP response (id, contextId, status,
    artifacts)."""
    return {
        "id": task_id,
        "contextId": context_id,
        "status": status,
        "artifacts": artifacts,
    }
