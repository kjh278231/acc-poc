"""The three agent tools: get_policies, file_claim, and the end_call finish.

get_policies / file_claim call the workshop Insurance API with the X-API-Key
header (key from Secrets Manager). end_call sets a pending finish that the
session handler acts on after the goodbye plays; on ESCALATE it first writes
the screen-pop attributes with connect:UpdateContactAttributes (README R4/V4).

Tool specs mirror partner-persona.md exactly; they are passed to Nova's
toolConfiguration.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import boto3
import urllib.request
import urllib.error

log = logging.getLogger("bridge.tools")

# JSON Schema strings copied verbatim from partner-persona.md.
TOOL_SPECS = [
    {"toolSpec": {"name": "get_policies",
                  "description": "Look up the verified caller's insurance policies. Returns policy IDs, types "
                                 "(auto, home, life, health, umbrella) and status.",
                  "inputSchema": {"json": "{\"type\":\"object\",\"properties\":{}}"}}},
    {"toolSpec": {"name": "file_claim",
                  "description": "File a new claim on one of the caller's active policies. Returns the new claim "
                                 "ID and status.",
                  "inputSchema": {"json": "{\"type\":\"object\",\"required\":[\"policyId\",\"claimType\","
                                          "\"incidentDate\",\"description\",\"amount\"],\"properties\":{\"policyId\""
                                          ":{\"type\":\"string\"},\"claimType\":{\"type\":\"string\",\"enum\":"
                                          "[\"collision\",\"comprehensive\",\"property-damage\",\"theft\","
                                          "\"medical\",\"liability\",\"other\"]},\"incidentDate\":{\"type\":"
                                          "\"string\",\"description\":\"ISO 8601 date-time of the incident\"},"
                                          "\"description\":{\"type\":\"string\"},\"amount\":{\"type\":\"number\","
                                          "\"description\":\"Estimated claim amount in USD\"}}}"}}},
    {"toolSpec": {"name": "end_call",
                  "description": "End your part of the call after you have said goodbye. Use COMPLETE when the "
                                 "caller is done, ESCALATE when a human adjuster is needed.",
                  "inputSchema": {"json": "{\"type\":\"object\",\"required\":[\"outcome\",\"summary\"],"
                                          "\"properties\":{\"outcome\":{\"type\":\"string\",\"enum\":[\"COMPLETE\","
                                          "\"ESCALATE\"]},\"summary\":{\"type\":\"string\",\"description\":\"What "
                                          "happened on the call, for the next agent or human\"},\"reason\":"
                                          "{\"type\":\"string\",\"enum\":[\"complex_issue\",\"customer_frustration\","
                                          "\"policy_exception\",\"out_of_scope\",\"other\"]}}}"}}},
]


@dataclass
class PendingFinish:
    outcome: str          # COMPLETE or ESCALATE
    summary: str
    reason: Optional[str] = None


class Tools:
    """Executes tools. Bound to one session's config, customer id and contact arn."""

    def __init__(self, *, api_url: str, api_key: str, customer_id: str, region: str,
                 instance_id: str, contact_arn: Optional[str]):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.customer_id = customer_id
        self.region = region
        self.instance_id = instance_id
        self.contact_arn = contact_arn
        self.pending: Optional[PendingFinish] = None
        self._connect = None

    # --- HTTP to the Insurance API ------------------------------------------
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self.api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"insurance API {method} {path} -> {e.code}: {detail}") from e

    def get_policies(self, _args: dict) -> dict:
        result = self._request("GET", f"/policies?customerId={self.customer_id}")
        # Normalise a couple of common envelope shapes.
        policies = result.get("policies", result if isinstance(result, list) else result.get("items", result))
        return {"policies": policies}

    def file_claim(self, args: dict) -> dict:
        body = {
            "customerId": self.customer_id,
            "policyId": args["policyId"],
            "claimType": args["claimType"],
            "incidentDate": args["incidentDate"],
            "description": args["description"],
            "amount": args["amount"],
        }
        result = self._request("POST", "/claims", body)
        # The insurance API answers {"claim": {"claimId": ..., "status": ...}}; accept a flat answer too.
        claim = result.get("claim") if isinstance(result.get("claim"), dict) else result
        return {"claimId": claim.get("claimId") or claim.get("id"), "status": claim.get("status")}

    # --- Finish -------------------------------------------------------------
    def end_call(self, args: dict) -> dict:
        outcome = args["outcome"]
        self.pending = PendingFinish(outcome=outcome, summary=args["summary"], reason=args.get("reason"))
        if outcome == "ESCALATE":
            self._set_escalation_attributes(args)
        return {"acknowledged": True, "outcome": outcome}

    def _set_escalation_attributes(self, args: dict) -> None:
        if not self.contact_arn:
            log.warning("ESCALATE but no contactArn; skipping UpdateContactAttributes")
            return
        contact_id = self.contact_arn.rsplit("/", 1)[-1]
        if self._connect is None:
            self._connect = boto3.client("connect", region_name=self.region)
        try:
            self._connect.update_contact_attributes(
                InitialContactId=contact_id,
                InstanceId=self.instance_id,
                Attributes={
                    "escalationReason": args.get("reason", "other"),
                    "escalationSummary": args["summary"],
                    "customerIntent": "file_claim",
                    "sentiment": "negative" if args.get("reason") == "customer_frustration" else "neutral",
                },
            )
            log.info("escalation attributes set on contact %s", contact_id)
        except Exception as e:  # noqa: BLE001 - never let this kill the turn
            log.warning("UpdateContactAttributes failed: %s", e)

    # --- Dispatch -----------------------------------------------------------
    def run(self, name: str, args: dict) -> dict:
        if name == "get_policies":
            return self.get_policies(args)
        if name == "file_claim":
            return self.file_claim(args)
        if name == "end_call":
            return self.end_call(args)
        raise RuntimeError(f"unknown tool {name}")
