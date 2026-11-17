"""Local stand-in for Amazon Connect Customer acting as the A2A *client*.

Drives a talk-to-the-customer session (WebSocket or HTTPS) or a behind-the-scenes invocation exactly as the
Connect docs describe, validates every frame against the contract, and measures timings:

  init_ms            INIT_SESSION sent -> INIT_SESSION_RESPONSE received
  first_chunk_ms     customer turn sent -> first TEXT_RESPONSE_CHUNK
  turn_ms            customer turn sent -> terminal statusUpdate
  server_ms          time the server spent inside the orchestrator (from task metadata, HTTPS only) / model+tool time
  overhead_ms        turn_ms - orchestrator total (A2A serialization + transport)

CLI:  uv run python -m app.adapters.mock_connect --url ws://127.0.0.1:8080/a2a/ws --say "30분이요" --say "없어요"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from app.adapters.a2a import ET, EXT

ARN_I = "arn:aws:connect:ap-northeast-2:123456789012:instance/00000000-0000-0000-0000-000000000000"


class ProtocolViolation(AssertionError):
    pass


def _send(method_id: str, ctx: str, parts: list[dict[str, Any]], event_type: str | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "SendMessage", "id": method_id, "params": {"message": {
        "messageId": f"m-{uuid.uuid4().hex[:8]}", "contextId": ctx, "role": "ROLE_USER", "parts": parts,
        "metadata": {ET: event_type} if event_type else {}}}}


def init_session(ctx: str, req_id: str = "1", history: list | None = None, finish_types=("COMPLETE", "ESCALATE"),
                 audio: bool = False) -> dict[str, Any]:
    init: dict[str, Any] = {"instanceArn": ARN_I, "contactArn": f"{ARN_I}/contact/{ctx}",
                            "supportedFinishTypes": list(finish_types), "subscribeToTracingEvents": True,
                            "history": history or []}
    if audio:
        init["audioInputConfiguration"] = {"encoding": "LINEAR_PCM", "sampleRateHertz": 8000, "sampleSizeBits": 16, "channelCount": 1}
        init["audioOutputConfiguration"] = {"encoding": "LINEAR_PCM", "sampleRateHertz": 16000, "sampleSizeBits": 16, "channelCount": 1}
    return _send(req_id, ctx, [{"data": {"initSession": init}, "mediaType": "application/json"}], "INIT_SESSION")


def channel_ready(ctx: str, req_id: str = "cs-1") -> dict[str, Any]:
    return _send(req_id, ctx, [{"data": {"channelState": {"state": "READY"}}, "mediaType": "application/json"}], "CHANNEL_STATE")


def text_turn(ctx: str, text: str, req_id: str) -> dict[str, Any]:
    return _send(req_id, ctx, [{"text": text, "mediaType": "text/plain"}])


def interruption(ctx: str, req_id: str) -> dict[str, Any]:
    return _send(req_id, ctx, [{"data": {"interruption": {"type": "USER_AUDIO_INPUT", "reason": "user_speech"}},
                                "mediaType": "application/json"}], "INTERRUPTION")


@dataclass
class TurnResult:
    input: str
    text: str = ""
    artifacts: int = 0
    chunks: int = 0
    traced: bool = False
    finish: dict[str, Any] | None = None
    state: str | None = None
    error: dict[str, Any] | None = None
    first_chunk_ms: float | None = None
    turn_ms: float = 0.0
    server_total_ms: float | None = None
    interrupted: bool = False
    metrics: dict[str, Any] | None = None


@dataclass
class SessionResult:
    transport: str
    context_id: str
    init_ms: float = 0.0
    greeting: TurnResult | None = None
    turns: list[TurnResult] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


class TurnValidator:
    """Checks the per-turn frame sequence rules from the Connect contract."""

    def __init__(self, ctx: str, finish_types: tuple[str, ...]):
        self.ctx, self.finish_types = ctx, finish_types
        self.open: dict[str, int] = {}
        self.closed: set[str] = set()
        self.terminal = False
        self.trace_seen = False
        self.task_id: str | None = None
        self.violations: list[str] = []

    def v(self, msg: str) -> None:
        self.violations.append(msg)

    def frame(self, f: dict[str, Any], tr: TurnResult, t0: float) -> bool:
        """Returns True when the turn is over."""
        if "error" in f:
            tr.error = f["error"]
            return True
        res = f.get("result") or {}
        if self.terminal:
            self.v(f"frame after terminal status: {list(res)}")
        if "artifactUpdate" in res:
            au = res["artifactUpdate"]
            self.task_id = self.task_id or au.get("taskId")
            if au.get("taskId") != self.task_id:
                self.v("artifact for a different taskId inside one turn")
            if au.get("contextId") != self.ctx:
                self.v("contextId mismatch")
            art = au["artifact"]
            et = (art.get("metadata") or {}).get(ET)
            aid = art["artifactId"]
            if et == "TRACING_SPAN":
                self.trace_seen = tr.traced = True
                spans = art["parts"][0]["data"]["tracingSpan"]["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
                for s in spans:
                    for k in ("traceId", "spanId", "name", "startTimeUnixNano", "endTimeUnixNano"):
                        if not s.get(k):
                            self.v(f"span missing {k}")
                if not spans[0]["name"].startswith("invoke_agent"):
                    self.v("first span is not invoke_agent")
                return False
            if et != "TEXT_RESPONSE_CHUNK":
                self.v(f"unexpected artifact eventType {et}")
            if self.trace_seen:
                self.v("response chunk after TRACING_SPAN")
            if aid in self.closed:
                self.v(f"chunk on closed artifact {aid}")
            n = self.open.get(aid, 0)
            if n == 0 and au.get("append"):
                self.v("first chunk has append=true")
            if n > 0 and not au.get("append"):
                self.v("continuation chunk has append=false")
            if n == 0 and any(o not in self.closed for o in self.open):
                self.v("second artifact opened while another is open")
            self.open[aid] = n + 1
            tr.chunks += 1
            if tr.first_chunk_ms is None:
                tr.first_chunk_ms = (time.perf_counter() - t0) * 1000
            tr.text += "".join(p.get("text", "") for p in art["parts"]) + " "
            if au.get("lastChunk"):
                self.closed.add(aid)
                tr.artifacts += 1
            return False
        if "statusUpdate" in res:
            st = res["statusUpdate"]["status"]
            tr.state = st["state"]
            self.terminal = True
            unclosed = [a for a in self.open if a not in self.closed]
            if unclosed and tr.state == "TASK_STATE_COMPLETED" and not tr.interrupted:
                self.v(f"terminal status with open artifacts {unclosed}")
            msg = st.get("message") or {}
            for p in msg.get("parts") or []:
                if "finish" in (p.get("data") or {}):
                    tr.finish = p["data"]["finish"]
                    if tr.finish["type"] not in self.finish_types:
                        self.v(f"FINISH type {tr.finish['type']} not in supportedFinishTypes")
                if "error" in (p.get("data") or {}):
                    tr.error = p["data"]["error"]
            return True
        if "task" in res:  # HTTPS: whole task at once
            task = res["task"]
            tr.state = task["status"]["state"]
            for a in task.get("artifacts") or []:
                et = (a.get("metadata") or {}).get(ET)
                if et == "TRACING_SPAN":
                    tr.traced = True
                elif et == "TEXT_RESPONSE_CHUNK":
                    tr.artifacts += 1
                    tr.chunks += len(a["parts"])
                    tr.text += "".join(p.get("text", "") for p in a["parts"]) + " "
            msg = task["status"].get("message") or {}
            for p in msg.get("parts") or []:
                if "finish" in (p.get("data") or {}):
                    tr.finish = p["data"]["finish"]
            tr.metrics = (task.get("metadata") or {}).get("acc.metrics")
            return True
        return False


class MockConnectWS:
    def __init__(self, url: str, api_key: str, finish_types=("COMPLETE", "ESCALATE")):
        self.url, self.api_key, self.finish_types = url, api_key, tuple(finish_types)

    async def run(self, utterances: list[str], ctx: str | None = None, interrupt_after_first_chunk_on: int | None = None,
                  expect_greeting: bool = True) -> SessionResult:
        ctx = ctx or f"ctx-{uuid.uuid4().hex[:12]}"
        trace_id, span_id = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        headers = {"Authorization": f"Bearer {self.api_key}", "A2A-Extensions": EXT,
                   "traceparent": f"00-{trace_id}-{span_id}-01", "tracestate": f"connect=parentSpanId:{uuid.uuid4()}"}
        out = SessionResult(transport="ws", context_id=ctx)
        async with websockets.connect(self.url, additional_headers=headers, max_size=None) as ws:
            t0 = time.perf_counter()
            await ws.send(json.dumps(init_session(ctx, finish_types=self.finish_types)))
            resp = json.loads(await ws.recv())
            out.init_ms = (time.perf_counter() - t0) * 1000
            if resp.get("id") != "1":
                out.violations.append("INIT_SESSION_RESPONSE does not echo request id")
            m = resp.get("result", {}).get("message", {})
            if (m.get("metadata") or {}).get(ET) != "INIT_SESSION_RESPONSE":
                out.violations.append("missing INIT_SESSION_RESPONSE eventType")
            if m["parts"][0]["data"]["initSessionResponse"]["statusCode"] != "200":
                out.violations.append("init statusCode != 200")

            t0 = time.perf_counter()
            await ws.send(json.dumps(channel_ready(ctx)))
            if expect_greeting:  # the agent speaks first on a fresh outbound call; not again after a reconnect
                out.greeting = await self._collect(ws, "<READY>", t0, ctx, out)
                if not out.greeting.traced:
                    out.violations.append("greeting turn without TRACING_SPAN")
            for i, u in enumerate(utterances):
                t0 = time.perf_counter()
                await ws.send(json.dumps(text_turn(ctx, u, f"req-{i}")))
                if interrupt_after_first_chunk_on == i:
                    tr = await self._collect(ws, u, t0, ctx, out, interrupt=True)
                else:
                    tr = await self._collect(ws, u, t0, ctx, out)
                out.turns.append(tr)
                if tr.finish or tr.error or tr.state != "TASK_STATE_COMPLETED":
                    break
        return out

    async def _collect(self, ws, text: str, t0: float, ctx: str, out: SessionResult, interrupt: bool = False) -> TurnResult:
        tr = TurnResult(input=text)
        val = TurnValidator(ctx, self.finish_types)
        while True:
            f = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            done = val.frame(f, tr, t0)
            if interrupt and tr.chunks == 1 and not tr.interrupted:
                tr.interrupted = True
                await ws.send(json.dumps(interruption(ctx, "int-1")))
            if done:
                break
        tr.turn_ms = (time.perf_counter() - t0) * 1000
        tr.text = tr.text.strip()
        out.violations += [f"[{text}] {v}" for v in val.violations]
        return tr


class MockConnectHTTP:
    def __init__(self, url: str, api_key: str, finish_types=("COMPLETE", "ESCALATE"), client: httpx.AsyncClient | None = None):
        self.url, self.api_key, self.finish_types = url, api_key, tuple(finish_types)
        self.client = client

    async def run(self, utterances: list[str], ctx: str | None = None) -> SessionResult:
        ctx = ctx or f"ctx-{uuid.uuid4().hex[:12]}"
        trace_id = uuid.uuid4().hex
        headers = {"Authorization": f"Bearer {self.api_key}", "A2A-Extensions": EXT,
                   "traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"}
        out = SessionResult(transport="https", context_id=ctx)
        own = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30)
        try:
            t0 = time.perf_counter()
            r = await client.post(self.url, json=init_session(ctx, finish_types=self.finish_types), headers=headers)
            out.init_ms = (time.perf_counter() - t0) * 1000
            if r.headers.get("a2a-extensions") != EXT:
                out.violations.append("extension header not echoed")
            t0 = time.perf_counter()
            r = await client.post(self.url, json=channel_ready(ctx), headers=headers)
            out.greeting = self._turn("<READY>", r.json(), t0, ctx, out)
            for i, u in enumerate(utterances):
                t0 = time.perf_counter()
                r = await client.post(self.url, json=text_turn(ctx, u, f"req-{i}"), headers=headers)
                tr = self._turn(u, r.json(), t0, ctx, out)
                out.turns.append(tr)
                if tr.finish or tr.error or tr.state != "TASK_STATE_COMPLETED":
                    break
        finally:
            if own:
                await client.aclose()
        return out

    def _turn(self, text: str, body: dict[str, Any], t0: float, ctx: str, out: SessionResult) -> TurnResult:
        tr = TurnResult(input=text)
        val = TurnValidator(ctx, self.finish_types)
        val.frame(body, tr, t0)
        tr.turn_ms = (time.perf_counter() - t0) * 1000
        tr.text = tr.text.strip()
        if tr.metrics:
            tr.server_total_ms = tr.metrics.get("turn_total_ms")
        out.violations += [f"[{text}] {v}" for v in val.violations]
        return tr


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8080/a2a/ws")
    ap.add_argument("--key", default="local-dev-key")
    ap.add_argument("--say", action="append", default=[])
    a = ap.parse_args()
    client = MockConnectWS(a.url, a.key) if a.url.startswith("ws") else MockConnectHTTP(a.url, a.key)
    res = await client.run(a.say or ["한 시간 정도 걸려요", "특이사항 없어요"])
    print(f"init {res.init_ms:.1f} ms  violations={res.violations or 'none'}")
    for tr in [res.greeting] + res.turns:
        print(f"> {tr.input}\n< {tr.text}\n  first_chunk={tr.first_chunk_ms and round(tr.first_chunk_ms)}ms "
              f"turn={tr.turn_ms:.0f}ms traced={tr.traced} state={tr.state} finish={tr.finish}")


if __name__ == "__main__":
    asyncio.run(_main())
