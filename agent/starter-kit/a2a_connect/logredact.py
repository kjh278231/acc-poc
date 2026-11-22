# SPDX-License-Identifier: MIT-0
"""Redaction for the control-message log (Requirement 5.8).

The kit logs every control message it receives and how it handled it, so a
partner can see the INIT_SESSION / CHANNEL_STATE / ERROR traffic while
debugging. The API key must never reach those logs. Connect forwards the key as
``Authorization: Bearer <value>`` and a customer may store it raw or as
``{"apiKey": "<key>"}``, so the same key material can appear in either form.

:func:`redact` masks that material before it is logged:

* an ``Authorization`` bearer value, raw or JSON-wrapped;
* an ``apiKey`` / ``api_key`` field value wherever it appears in a structure;
* any occurrence of the configured secret itself.

Control frames carry no key field, so in normal operation nothing is masked.
The redaction is defence in depth: if a key ever lands in a logged structure
(a mis-built frame, a future field), it is masked rather than printed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

MASK = "***REDACTED***"

# Keys whose values are secret wherever they appear in a logged structure.
_SECRET_KEYS = {"apikey", "api_key", "authorization"}

# "Bearer <token>" anywhere in a string. The token runs to whitespace unless it
# opens a JSON object, in which case it runs to the matching brace depth 0.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+(\{.*?\}|\S+)")


def _redact_bearer(text: str) -> str:
    def repl(_m: "re.Match[str]") -> str:
        return "Bearer " + MASK

    return _BEARER_RE.sub(repl, text)


def redact(value: Any, secret: Optional[str] = None) -> Any:
    """Return ``value`` with any key material masked.

    Works on strings (masks bearer tokens and the secret) and on dict/list
    structures (masks secret-named field values, recursively, and bearer
    tokens inside any string). Other scalars are returned unchanged. The input
    is never mutated.
    """
    if isinstance(value, str):
        out = _redact_bearer(value)
        if secret:
            out = out.replace(secret, MASK)
        return out
    if isinstance(value, dict):
        out_d: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out_d[k] = MASK
            else:
                out_d[k] = redact(v, secret)
        return out_d
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


def redact_json(value: Any, secret: Optional[str] = None) -> str:
    """Redact ``value`` and return it as a compact JSON string for one log
    line. Falls back to ``str`` on anything JSON cannot serialize."""
    try:
        return json.dumps(redact(value, secret), default=str, sort_keys=True)
    except (TypeError, ValueError):
        return redact(str(value), secret)
