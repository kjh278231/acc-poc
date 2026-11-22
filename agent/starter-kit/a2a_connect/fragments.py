# SPDX-License-Identifier: MIT-0
"""WebSocket transport fragmentation (Samples A.9).

Inbound: buffer FRAGMENT frames by ``fragmentId``, and once ``totalParts`` of
them arrive, concatenate their ``payload`` values in ``partNumber`` order and
base64-decode into the original JSON frame.

Outbound: when a frame serializes larger than ``--max-frame-bytes``, split it
into FRAGMENT messages, each carrying exactly one data part. Fragmentation never
applies on HTTP and never to audio.
"""
from __future__ import annotations

import base64
import json
from typing import Optional

from . import EVENT_TYPE_KEY
from .protocol import FRAGMENT, new_id


class FragmentReassembler:
    """Buffers inbound fragments until a frame is complete."""

    def __init__(self) -> None:
        self._buffers: dict[str, dict[int, str]] = {}
        self._totals: dict[str, int] = {}

    def add(self, fragment: dict) -> Optional[dict]:
        """Add one FRAGMENT's ``fragment`` data. Returns the reassembled frame
        (a dict) once all parts are present, else None."""
        fid = fragment["fragmentId"]
        part = int(fragment["partNumber"])
        total = int(fragment["totalParts"])
        self._totals[fid] = total
        self._buffers.setdefault(fid, {})[part] = fragment["payload"]
        if len(self._buffers[fid]) < total:
            return None
        ordered = [self._buffers[fid][i] for i in range(total)]
        del self._buffers[fid]
        del self._totals[fid]
        raw = base64.b64decode("".join(ordered))
        return json.loads(raw.decode("utf-8"))


def fragment_frame(frame: dict, max_bytes: int, rpc_id, *,
                   start_part: int = 0) -> Optional[list[dict]]:
    """Fragment ``frame`` if its serialization exceeds ``max_bytes``.

    Returns a list of FRAGMENT message frames, or None if the frame fits and
    should be sent as-is. ``start_part`` is 0 per the spec; the
    ``bad-outbound-fragment`` fault passes 1.
    """
    serialized = json.dumps(frame).encode("utf-8")
    if len(serialized) <= max_bytes:
        return None

    encoded = base64.b64encode(serialized).decode("ascii")
    # Chunk the base64 text so each resulting FRAGMENT frame stays small. Use a
    # conservative slice of max_bytes to leave room for the JSON-RPC envelope.
    slice_size = max(256, max_bytes // 2)
    chunks = [encoded[i:i + slice_size] for i in range(0, len(encoded), slice_size)]
    total = len(chunks)
    fid = new_id()
    frames: list[dict] = []
    for idx, chunk in enumerate(chunks):
        frames.append({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "message": {
                    "messageId": new_id(),
                    "role": "ROLE_AGENT",
                    "parts": [{
                        "data": {
                            "fragment": {
                                "fragmentId": fid,
                                "partNumber": idx + start_part,
                                "totalParts": total,
                                "payload": chunk,
                                "encoding": "base64",
                            }
                        },
                        "mediaType": "application/json",
                    }],
                    "metadata": {EVENT_TYPE_KEY: FRAGMENT},
                }
            },
        })
    return frames
