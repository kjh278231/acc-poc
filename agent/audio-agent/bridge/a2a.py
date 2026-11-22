"""A2A wire-protocol helpers for the Connect Customer A2A extension (v1).

Task 3.7 moved the bridge's outbound frame building onto the Starter_Kit's
``a2a_connect`` modules so both the text Reference_Agent and the audio bridge
build frames from one implementation of the contract:

* text frames (INIT_SESSION_RESPONSE text form, TRACING_SPAN, terminal status
  with FINISH) come from ``a2a_connect.protocol``;
* audio frames (INIT_SESSION_RESPONSE with ``audioOutputConfiguration``, the
  AUDIO_RESPONSE_CHUNK artifact, the agent-detected INTERRUPTION) come from the
  narrow ``a2a_connect.audio_frames`` helper.

The builders here keep returning serialized JSON strings, because the bridge
server sends them with ``ws.send_str``; they are thin wrappers that call the kit
builder and ``json.dumps`` the result.

The inbound parsing helpers (``message_kind``, ``data_of``, ``audio_of``,
``iter_parts``, ``summarize``) stay in the bridge: the kit parses a text turn
and ignores audio, while the bridge needs the raw PCM parts and treats a frame
with no event type as an audio turn. Shapes follow the public contract and
sample events A.1-A.10.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Optional

from a2a_connect import EVENT_TYPE_KEY, EXT
from a2a_connect import audio_frames as _af
from a2a_connect import protocol as _proto

# data-part key -> event type, used when metadata is missing.
_DATA_KEY_TO_EVENT = {
    "initSession": "INIT_SESSION",
    "channelState": "CHANNEL_STATE",
    "interruption": "INTERRUPTION",
    "error": "ERROR",
    "fragment": "FRAGMENT",
    "dtmfInputComplete": "DTMF_INPUT_COMPLETE",
}


def new_id() -> str:
    return str(uuid.uuid4())


# --- Parsing inbound frames -------------------------------------------------
def parse_frame(text: str) -> dict:
    return json.loads(text)


def message_of(frame: dict) -> dict:
    params = frame.get("params") or {}
    return params.get("message") or {}


def iter_parts(frame: dict):
    return message_of(frame).get("parts") or []


def data_of(frame: dict, key: str) -> Optional[dict]:
    """Return ``part.data[key]`` from the first data part carrying ``key``."""
    for part in iter_parts(frame):
        data = part.get("data")
        if isinstance(data, dict) and key in data:
            return data[key] or {}
    return None


def message_kind(frame: dict) -> Optional[str]:
    """INIT_SESSION, CHANNEL_STATE, ... or None for a plain turn (audio/text)."""
    msg = message_of(frame)
    md = msg.get("metadata") or {}
    kind = md.get(EVENT_TYPE_KEY)
    if kind:
        return kind
    for part in msg.get("parts") or []:
        data = part.get("data")
        if isinstance(data, dict):
            for k in data:
                if k in _DATA_KEY_TO_EVENT:
                    return _DATA_KEY_TO_EVENT[k]
    return None


def audio_of(part: dict) -> Optional[bytes]:
    """Decode a ``raw`` audio part; None if the part is not audio."""
    raw = part.get("raw")
    if isinstance(raw, str) and (part.get("mediaType") or "").startswith("audio"):
        return base64.b64decode(raw) if raw else b""
    return None


def summarize(frame: dict, limit: int = 600) -> str:
    """Compact, log-safe rendering: base64 audio replaced by its length."""
    def scrub(o: Any):
        if isinstance(o, dict):
            return {k: (f"<{len(v)} b64 chars>" if k in ("raw", "payload") and isinstance(v, str)
                        else scrub(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [scrub(x) for x in o]
        return o
    return json.dumps(scrub(frame))[:limit]


# --- Building outbound frames (delegated to the kit) ------------------------
def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def init_session_response(request_id, context_id: str, output_sample_rate: Optional[int]) -> str:
    """A.2: reply to INIT_SESSION, echoing the request id, counter-proposing the
    output audio rate. Built by the kit's audio helper."""
    frame = _af.init_session_response_with_audio(request_id, context_id, output_sample_rate)
    return json.dumps(frame)


def audio_chunk_message(context_id: str, task_id: str, artifact_id: str,
                        pcm: bytes, *, append: bool, last: bool) -> str:
    """A.4b: one AUDIO_RESPONSE_CHUNK artifactUpdate carrying one PCM frame.

    Pushed frames may carry any JSON-RPC id (only direct replies echo the
    request id), so a fresh short id is used, matching the earlier behaviour."""
    frame = _af.audio_chunk_artifact(_short_id(), task_id, context_id, artifact_id,
                                     pcm, append=append, last_chunk=last)
    return json.dumps(frame)


def tracing_span_message(context_id: str, task_id: str, artifact_id: str, otlp_doc: dict) -> str:
    """A.10: TRACING_SPAN artifactUpdate. ``otlp_doc`` is ``{"otlp": {...}}``."""
    frame = _proto.trace_artifact(_short_id(), task_id, context_id, artifact_id, otlp_doc)
    return json.dumps(frame)


def status_update(context_id: str, task_id: str, state: str,
                  finish: Optional[dict] = None) -> str:
    """A.5: terminal statusUpdate for the turn. finish=None keeps the call alive;
    finish={"type","reason"} ends the A2A session."""
    finish_tuple = (finish["type"], finish.get("reason", "")) if finish else None
    frame = _proto.terminal_status(_short_id(), task_id, context_id, state,
                                   finish=finish_tuple, timestamp=_now_iso())
    return json.dumps(frame)


def interruption_message(context_id: str, task_id: Optional[str] = None) -> str:
    """A.7 (agent-detected barge-in) so Connect flushes buffered playback.

    ``task_id`` is accepted for call-site compatibility; the INTERRUPTION
    message is a session-level signal keyed by contextId (Samples A.7)."""
    frame = _af.interruption_message(_short_id(), context_id)
    return json.dumps(frame)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
