"""Lazy Secrets Manager access, using the ECS task role via the default chain.

Secrets are fetched on first use and cached, so the process starts even if
Secrets Manager is briefly slow, and a rotated value is picked up on restart.
No secret value is ever logged (R6). boto3 is used only here and for the
Connect UpdateContactAttributes call; the Nova stream uses the CRT SDK.
"""
from __future__ import annotations

import logging
import os

import boto3

log = logging.getLogger("bridge.secrets")


class SecretCache:
    def __init__(self, region: str) -> None:
        self._client = boto3.client("secretsmanager", region_name=region)
        self._cache: dict[str, str] = {}

    def get(self, secret_id: str, *, label: str) -> str:
        if secret_id in self._cache:
            return self._cache[secret_id]
        # Local override: LOCAL_SECRET__<sanitized id> lets the harness and unit
        # runs supply a value without a real Secrets Manager entry. Never set in
        # the deployed task; production always resolves from Secrets Manager.
        env_key = "LOCAL_SECRET__" + "".join(c if c.isalnum() else "_" for c in secret_id).upper()
        local = os.environ.get(env_key)
        if local is not None:
            self._cache[secret_id] = local
            log.info("%s loaded from %s (%d chars)", label, env_key, len(local))
            return local
        resp = self._client.get_secret_value(SecretId=secret_id)
        value = resp.get("SecretString")
        if value is None:
            raise RuntimeError(f"secret {secret_id} has no SecretString")
        self._cache[secret_id] = value
        # Log that it loaded and its length only, never the value (runbook step 3
        # gate: task log shows "<label> loaded").
        log.info("%s loaded (%d chars)", label, len(value))
        return value
