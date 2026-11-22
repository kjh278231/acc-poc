"""Bearer-token validation for the A2A WebSocket upgrade.

Connect Customer presents the secret as ``Authorization: Bearer <key>`` on the
upgrade. The peer build saw Connect send the whole secret string, and there is
a chance it wraps it as JSON, so the bridge accepts BOTH forms (README event
mapping, V6):

  * a raw string:            ``Bearer sk_live_abc123``
  * a JSON object:           ``Bearer {"apiKey": "sk_live_abc123"}``

The comparison is constant-time (``hmac.compare_digest``) so a caller cannot
learn the secret one byte at a time from response timing (R6).
"""
from __future__ import annotations

import hmac
import json
from typing import Optional, Tuple


def extract_presented_key(authorization_header: Optional[str]) -> Tuple[Optional[str], str]:
    """Pull the presented key out of an Authorization header.

    Returns ``(key, form)`` where ``form`` is one of ``"raw"``, ``"json"`` or
    ``"missing"``/``"malformed"``. ``key`` is ``None`` when nothing usable was
    presented. ``form`` is logged (V6) so we learn what Connect actually sends.
    """
    if not authorization_header:
        return None, "missing"
    value = authorization_header.strip()
    # Tolerate a missing scheme, but the normal case is "Bearer <token>".
    lowered = value.lower()
    if lowered == "bearer":
        return None, "missing"          # scheme word with no token
    if lowered.startswith("bearer "):
        token = value[len("bearer "):].strip()
    else:
        token = value
    if not token:
        return None, "missing"
    # JSON form: {"apiKey": "..."}. Only treat it as JSON if it parses to an
    # object carrying apiKey; otherwise it is an ordinary opaque token.
    if token.startswith("{"):
        try:
            parsed = json.loads(token)
        except ValueError:
            return None, "malformed"
        if isinstance(parsed, dict):
            key = parsed.get("apiKey") or parsed.get("api_key")
            if isinstance(key, str) and key:
                return key, "json"
        return None, "malformed"
    return token, "raw"


def bearer_matches(authorization_header: Optional[str], secret: str) -> Tuple[bool, str]:
    """Constant-time check of the presented bearer against the stored secret.

    Returns ``(ok, form)``. The comparison always runs against the full secret
    length so timing does not leak whether the key was absent or merely wrong.
    """
    presented, form = extract_presented_key(authorization_header)
    if presented is None:
        # Still burn a compare so a missing header is not measurably faster.
        hmac.compare_digest(secret, secret)
        return False, form
    return hmac.compare_digest(presented, secret), form
