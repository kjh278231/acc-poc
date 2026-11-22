"""Tools for the loopback and cascade engines.

Only end_call is needed for the protocol: it records the pending FINISH that
server.py acts on after the goodbye plays. The contact flow has no human queue,
so ESCALATE never reaches Connect: it files a callback request with the TMS and
Connect is told COMPLETE.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Optional

from .tools import PendingFinish

log = logging.getLogger("bridge.local_tools")

END_CALL_SPEC = {"toolSpec": {
    "name": "end_call",
    "description": "End your part of the call after you have said goodbye. Use COMPLETE when the caller is "
                   "done, ESCALATE when a person needs to call the customer back.",
    "inputSchema": {"json": "{\"type\":\"object\",\"required\":[\"outcome\",\"summary\"],\"properties\":"
                            "{\"outcome\":{\"type\":\"string\",\"enum\":[\"COMPLETE\",\"ESCALATE\"]},"
                            "\"summary\":{\"type\":\"string\"},\"reason\":{\"type\":\"string\"}}}"}}}

# Two tries must finish inside server.py's tool timeout (TOOL_TIMEOUT_SECONDS, 5 s).
TMS_TIMEOUT_S = 1.5
TMS_TRIES = 2
# A2A_TMS_URL=mock answers in-process instead of calling a TMS.
MOCK_TMS = "mock"


class LocalTools:
    def __init__(self, *, region: str, instance_id: str = "", contact_arn: Optional[str] = None,
                 tms_url: str = "") -> None:
        self.region = region
        self.instance_id = instance_id
        self.contact_arn = contact_arn
        self.tms_url = tms_url
        self.pending: Optional[PendingFinish] = None

    def run(self, name: str, args: dict) -> dict:
        if name == "end_call":
            outcome = args.get("outcome", "COMPLETE")
            self.pending = PendingFinish(outcome="COMPLETE", summary=args.get("summary", ""),
                                         reason=args.get("reason"))
            if outcome == "ESCALATE":
                self._request_callback(args)
            return {"acknowledged": True, "outcome": outcome}
        return {"error": f"unknown tool {name}"}

    def _caller_number(self) -> Optional[str]:
        """Caller's phone number from the contact; web calls have none."""
        if not (self.instance_id and self.contact_arn):
            return None
        try:
            import boto3
            contact = boto3.client("connect", region_name=self.region).describe_contact(
                InstanceId=self.instance_id, ContactId=self.contact_arn.rsplit("/", 1)[-1])["Contact"]
            return (contact.get("CustomerEndpoint") or {}).get("Address")
        except Exception:  # noqa: BLE001
            log.exception("DescribeContact failed")
            return None

    def _request_callback(self, args: dict) -> None:
        body = json.dumps({
            "contactId": (self.contact_arn or "").rsplit("/", 1)[-1],
            "phone": self._caller_number(),
            "reason": str(args.get("reason") or "other"),
            "summary": str(args.get("summary") or "")[:1000],
        }, ensure_ascii=False)
        for attempt in range(1, TMS_TRIES + 1 if self.tms_url else 1):
            try:
                log.info("callback request filed with TMS: %s", self._post(body))
                return
            except Exception as e:  # noqa: BLE001
                log.warning("TMS callback request try %d failed: %s", attempt, e)
        # A lost request means nobody calls the customer back: this line is what to recover from.
        log.error("CALLBACK_NOT_FILED %s", body)

    def _post(self, body: str) -> str:
        """Send the callback request; returns the TMS reply (trimmed) for the log."""
        if self.tms_url == MOCK_TMS:
            # shortcut: stand-in TMS (0.5 s, always accepts) until the real API exists; set A2A_TMS_URL to it then.
            time.sleep(0.5)
            return json.dumps({"status": "received", "contactId": json.loads(body)["contactId"]})
        # shortcut: JSON POST without auth; add the TMS auth header and field names once its API is fixed.
        req = urllib.request.Request(self.tms_url, data=body.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TMS_TIMEOUT_S) as r:
            return r.read(200).decode("utf-8", "replace")
