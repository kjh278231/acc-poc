"""In-memory TMS mock with configurable latency, failure injection and idempotency."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from typing import Any


class TmsError(Exception):
    pass


class TmsMock:
    def __init__(self, latency_ms: int = 150, failure_rate: float = 0.0, seed: int | None = None):
        self.latency_ms = latency_ms
        self.failure_rate = failure_rate
        self._rng = random.Random(seed)
        self.records: dict[str, dict[str, Any]] = {}      # shipment_id -> latest record
        self.updates: dict[str, dict[str, Any]] = {}      # idempotency_key -> record
        self.tickets: list[dict[str, Any]] = []
        self.fail_next: int = 0                           # test hook: fail N next calls

    async def _io(self) -> None:
        await asyncio.sleep(self.latency_ms / 1000)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TmsError("TMS unavailable (injected)")
        if self.failure_rate and self._rng.random() < self.failure_rate:
            raise TmsError("TMS unavailable (random)")

    async def update_shipment(self, shipment_id: str, *, session_id: str, eta_minutes: int | None, issue: str | None,
                              delay_minutes: int | None, status: str) -> dict[str, Any]:
        payload = {"shipment_id": shipment_id, "eta_minutes": eta_minutes, "issue": issue,
                   "delay_minutes": delay_minutes, "status": status}
        key = hashlib.sha256(f"{session_id}:{json.dumps(payload, sort_keys=True)}".encode()).hexdigest()[:24]
        if key in self.updates:
            return {**self.updates[key], "duplicate": True}
        await self._io()
        rec = {**payload, "session_id": session_id, "idempotency_key": key, "updated_at": time.time()}
        self.updates[key] = rec
        self.records[shipment_id] = rec
        return rec

    async def create_escalation(self, shipment_id: str, *, session_id: str, reason: str) -> dict[str, Any]:
        await self._io()
        t = {"ticket_id": f"ESC-{len(self.tickets) + 1:04d}", "shipment_id": shipment_id, "session_id": session_id,
             "reason": reason, "queue": "dispatch", "created_at": time.time()}
        self.tickets.append(t)
        return t
