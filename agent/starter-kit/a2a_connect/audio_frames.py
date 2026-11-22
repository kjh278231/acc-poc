# SPDX-License-Identifier: MIT-0
"""Audio frame builders for a speech-to-speech endpoint (Samples A.2, A.4b, A.7).

``protocol.py`` builds the text turn lifecycle; it does not build audio frames,
because the text Reference_Agent never sends audio. The Demo 1 audio bridge
does, so these three builders live in their own module rather than widening the
text protocol:

* ``init_session_response_with_audio`` - an INIT_SESSION_RESPONSE that
  counter-proposes an ``audioOutputConfiguration`` (Samples A.2). The text
  form stays in ``protocol.init_session_response``.
* ``audio_chunk_artifact`` - one AUDIO_RESPONSE_CHUNK artifactUpdate carrying a
  base64 PCM frame (Samples A.4b). The text form is
  ``protocol.text_chunk_artifact``.
* ``interruption_message`` - an agent-detected barge-in so Connect flushes
  buffered playback (Samples A.7).

The trace artifact and the terminal status are the same for audio and text, so
those come straight from ``protocol`` (``trace_artifact``, ``terminal_status``).
Everything here is pure: dicts in, dicts out, no transport.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from . import EVENT_TYPE_KEY
from .protocol import new_id, result_frame

# Outbound audio event type (agent -> Connect).
AUDIO_RESPONSE_CHUNK = "AUDIO_RESPONSE_CHUNK"
INTERRUPTION = "INTERRUPTION"
INIT_SESSION_RESPONSE = "INIT_SESSION_RESPONSE"

# PCM is 16-bit signed mono; LINEAR_PCM config, audio/lpcm parts.
AUDIO_MEDIA_TYPE = "audio/lpcm"


def audio_output_configuration(sample_rate_hertz: int) -> dict[str, Any]:
    """A LINEAR_PCM audioOutputConfiguration for the given rate (Samples A.1/A.2)."""
    return {
        "encoding": "LINEAR_PCM",
        "sampleRateHertz": sample_rate_hertz,
        "sampleSizeBits": 16,
        "channelCount": 1,
    }


def init_session_response_with_audio(
    rpc_id: Any,
    context_id: str,
    output_sample_rate: Optional[int],
    include_event_type: bool = True,
) -> dict[str, Any]:
    """INIT_SESSION_RESPONSE (Samples A.2) that counter-proposes an output rate.

    Same shape as ``protocol.init_session_response`` plus the
    ``audioOutputConfiguration`` in the ``initSessionResponse`` body when
    ``output_sample_rate`` is given. ``include_event_type`` drops the event-type
    metadata for the bridge's init-bad-shape fault.
    """
    body: dict[str, Any] = {"statusCode": "200"}
    if output_sample_rate:
        body["audioOutputConfiguration"] = audio_output_configuration(output_sample_rate)
    metadata: dict[str, Any] = {}
    if include_event_type:
        metadata[EVENT_TYPE_KEY] = INIT_SESSION_RESPONSE
    message = {
        "messageId": new_id(),
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [{
            "data": {"initSessionResponse": body},
            "mediaType": "application/json",
        }],
        "extensions": ["https://docs.aws.amazon.com/connect/a2a/ext/v1"],
        "metadata": metadata,
    }
    return result_frame(rpc_id, "message", message)


def audio_chunk_artifact(
    rpc_id: Any,
    task_id: str,
    context_id: str,
    artifact_id: str,
    pcm: bytes,
    append: bool,
    last_chunk: bool,
) -> dict[str, Any]:
    """One AUDIO_RESPONSE_CHUNK artifactUpdate carrying one base64 PCM frame
    (Samples A.4b). Same append/lastChunk flag discipline as a text chunk."""
    return result_frame(rpc_id, "artifactUpdate", {
        "taskId": task_id,
        "contextId": context_id,
        "artifact": {
            "artifactId": artifact_id,
            "parts": [{
                "raw": base64.b64encode(pcm).decode("ascii"),
                "mediaType": AUDIO_MEDIA_TYPE,
            }],
            "metadata": {EVENT_TYPE_KEY: AUDIO_RESPONSE_CHUNK},
        },
        "append": append,
        "lastChunk": last_chunk,
    })


def interruption_message(rpc_id: Any, context_id: str) -> dict[str, Any]:
    """An agent-detected barge-in (Samples A.7) so Connect flushes playback."""
    return result_frame(rpc_id, "message", {
        "messageId": new_id(),
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [{
            "data": {"interruption": {"type": "USER_AUDIO_INPUT", "reason": "user_speech"}},
            "mediaType": "application/json",
        }],
        "extensions": ["https://docs.aws.amazon.com/connect/a2a/ext/v1"],
        "metadata": {EVENT_TYPE_KEY: INTERRUPTION},
    })
