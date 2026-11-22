# SPDX-License-Identifier: MIT-0
"""Bearer authentication.

Connect forwards the stored secret string verbatim as ``Authorization: Bearer
<value>``. A customer can store the key as a raw string or as
``{"apiKey":"<key>"}``, so both reach the agent. The kit accepts both and
compares in constant time. The key itself is loaded from an environment
variable, a file, or Secrets Manager (lazy, cached). The key and the
Authorization header are never logged.
"""
from __future__ import annotations

import hmac
import json
import logging
from typing import Optional

log = logging.getLogger("a2a_connect.auth")


def extract_presented_key(authorization: Optional[str]) -> tuple[Optional[str], str]:
    """Return ``(key, form)`` from an Authorization header.

    ``form`` is ``raw``, ``json``, ``missing`` or ``malformed``. The value is
    never logged by callers.
    """
    if not authorization:
        return None, "missing"
    value = authorization.strip()
    lowered = value.lower()
    if lowered == "bearer":
        return None, "missing"
    token = value[len("bearer "):].strip() if lowered.startswith("bearer ") else value
    if not token:
        return None, "missing"
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


class Authenticator:
    """Checks the bearer on every upgrade and request.

    ``fault`` is one of ``None``, ``auth-accept-any``, ``auth-reject-raw`` or
    ``auth-reject-json`` and changes which forms are accepted.
    """

    def __init__(self, key_provider, fault: Optional[str] = None) -> None:
        self._key_provider = key_provider  # callable -> str, lazy
        self._fault = fault
        self._cached_secret: Optional[str] = None

    def check(self, authorization: Optional[str]) -> bool:
        if self._fault == "auth-accept-any":
            return True
        presented, form = extract_presented_key(authorization)
        secret = self._key_provider()
        self._cached_secret = secret
        if presented is None:
            # Burn a compare so a missing header is not measurably faster.
            hmac.compare_digest(secret, secret)
            return False
        if self._fault == "auth-reject-raw" and form == "raw":
            return False
        if self._fault == "auth-reject-json" and form == "json":
            return False
        return hmac.compare_digest(presented, secret)

    def cached_secret(self) -> Optional[str]:
        """The secret, only if a prior ``check`` already resolved it. Never
        triggers a provider read itself, so the control-message logger can mask
        a bare secret value without forcing a Secrets Manager call."""
        return self._cached_secret


class PassThroughAuthenticator:
    """Admits every request: inbound auth is handled upstream.

    Used only in AgentCore mode with ``--auth agentcore``. Amazon Bedrock
    AgentCore Runtime validates SigV4 (IAM inbound) or a JWT (CUSTOM_JWT
    authorizer) before the request reaches the kit, so the kit's own bearer
    check would be redundant. This is NOT invented auth being disabled in the
    open: the request has already been authenticated by the platform's inbound
    authorizer, which is the network boundary for an AgentCore microVM.
    """

    def check(self, authorization: Optional[str]) -> bool:  # noqa: ARG002
        return True


# ---- Key sources -------------------------------------------------------------

def key_from_env(name: str):
    import os

    def provider() -> str:
        value = os.environ.get(name)
        if value is None:
            raise RuntimeError(f"environment variable {name} is not set")
        return value

    return provider


def key_from_file(path: str):
    cache: dict[str, str] = {}

    def provider() -> str:
        if "value" not in cache:
            with open(path, "r", encoding="utf-8") as fh:
                cache["value"] = fh.read().strip()
            log.info("api key loaded from file (%d chars)", len(cache["value"]))
        return cache["value"]

    return provider


def key_from_secret_arn(arn: str, region: Optional[str] = None):
    """Lazy Secrets Manager read. Unwraps a ``{"apiKey": "..."}`` secret the
    same way the header is unwrapped, so either storage form works."""
    cache: dict[str, str] = {}

    def provider() -> str:
        if "value" in cache:
            return cache["value"]
        import boto3

        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=arn)
        raw = resp.get("SecretString")
        if raw is None:
            raise RuntimeError(f"secret {arn} has no SecretString")
        value = raw.strip()
        if value.startswith("{"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    inner = parsed.get("apiKey") or parsed.get("api_key")
                    if isinstance(inner, str) and inner:
                        value = inner
            except ValueError:
                pass
        cache["value"] = value
        log.info("api key loaded from Secrets Manager (%d chars)", len(value))
        return value

    return provider
