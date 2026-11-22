"""Amazon Transcribe Streaming over WebSocket, with no extra dependencies.

Uses aiohttp (already in the image) for the socket and botocore for the SigV4
presigned URL, and encodes/decodes the AWS event-stream framing by hand:

  [total_len u32][headers_len u32][prelude_crc u32][headers][payload][message_crc u32]

Audio goes out as ``AudioEvent`` messages (raw 16-bit mono PCM). Transcripts come
back as ``TranscriptEvent`` JSON. ``on_partial(text)`` / ``on_final(text)`` are
awaited for every result, in order.

The task role needs ``transcribe:StartStreamTranscriptionWebSocket``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import zlib
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

log = logging.getLogger("bridge.transcribe")

Callback = Callable[[str], Awaitable[None]]


# --- event-stream framing ----------------------------------------------------
def _crc(b: bytes) -> int:
    return zlib.crc32(b) & 0xFFFFFFFF


def encode_event(headers: dict, payload: bytes) -> bytes:
    hb = b""
    for k, v in headers.items():
        kb, vb = k.encode(), v.encode()
        hb += struct.pack(">B", len(kb)) + kb + b"\x07" + struct.pack(">H", len(vb)) + vb
    total = 12 + len(hb) + len(payload) + 4
    prelude = struct.pack(">II", total, len(hb))
    msg = prelude + struct.pack(">I", _crc(prelude)) + hb + payload
    return msg + struct.pack(">I", _crc(msg))


def audio_event(pcm: bytes) -> bytes:
    return encode_event({":content-type": "application/octet-stream", ":event-type": "AudioEvent",
                         ":message-type": "event"}, pcm)


def decode_events(buf: bytes):
    """Yield (headers, payload) for every complete message in ``buf``."""
    off = 0
    while len(buf) - off >= 16:
        total, hlen = struct.unpack_from(">II", buf, off)
        if len(buf) - off < total:
            break
        h = buf[off + 12: off + 12 + hlen]
        payload = buf[off + 12 + hlen: off + total - 4]
        headers, i = {}, 0
        while i < len(h):
            nl = h[i]; i += 1
            name = h[i:i + nl].decode(); i += nl
            t = h[i]; i += 1
            if t in (0, 1):
                val = t == 0
            elif t == 2:
                val = h[i]; i += 1
            elif t == 3:
                val = struct.unpack_from(">h", h, i)[0]; i += 2
            elif t == 4:
                val = struct.unpack_from(">i", h, i)[0]; i += 4
            elif t in (5, 8):
                val = struct.unpack_from(">q", h, i)[0]; i += 8
            elif t in (6, 7):
                ln = struct.unpack_from(">H", h, i)[0]; i += 2
                val = h[i:i + ln]; i += ln
                if t == 7:
                    val = val.decode()
            elif t == 9:
                val = h[i:i + 16]; i += 16
            else:
                raise ValueError(f"unknown header type {t}")
            headers[name] = val
        yield headers, payload
        off += total


# --- presigned URL -----------------------------------------------------------
def presigned_url(region: str, language: str, sample_rate: int, expires: int = 300,
                  extra: Optional[dict] = None) -> str:
    import boto3
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    creds = boto3.Session().get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials for Transcribe")
    q = {"language-code": language, "media-encoding": "pcm", "sample-rate": str(sample_rate)}
    q.update(extra or {})
    url = (f"https://transcribestreaming.{region}.amazonaws.com:8443/stream-transcription-websocket?"
           + urlencode(q))
    req = AWSRequest(method="GET", url=url)
    SigV4QueryAuth(creds.get_frozen_credentials(), "transcribe", region, expires=expires).add_auth(req)
    return req.url.replace("https://", "wss://", 1)


# --- streaming session -------------------------------------------------------
class TranscribeStream:
    """One Transcribe stream per call, reopened automatically if it drops."""

    def __init__(self, *, region: str, language: str, sample_rate: int,
                 on_partial: Callback, on_final: Callback, chunk_ms: int = 100,
                 stabilization: str = "high") -> None:
        self.region = region
        self.language = language
        self.rate = sample_rate
        self.on_partial = on_partial
        self.on_final = on_final
        self.chunk_bytes = int(sample_rate * chunk_ms / 1000) * 2
        self.extra = {"enable-partial-results-stabilization": "true",
                      "partial-results-stability": stabilization} if stabilization else {}
        self._q: asyncio.Queue = asyncio.Queue(maxsize=600)   # ~60 s of 100 ms chunks
        self._buf = bytearray()
        self._closed = False
        self._task: Optional[asyncio.Task] = None
        self.last_final_at = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def feed(self, pcm: bytes) -> None:
        if self._closed:
            return
        self._buf.extend(pcm)
        while len(self._buf) >= self.chunk_bytes:
            chunk = bytes(self._buf[: self.chunk_bytes])
            del self._buf[: self.chunk_bytes]
            if self._q.full():            # stream is down: drop the oldest audio
                self._q.get_nowait()
            self._q.put_nowait(chunk)

    async def close(self) -> None:
        self._closed = True
        with _suppress():
            self._q.put_nowait(None)
        if self._task:
            try:
                await asyncio.wait_for(self._task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._task.cancel()

    async def _run(self) -> None:
        import aiohttp
        backoff = 0.5
        while not self._closed:
            t0 = time.monotonic()
            try:
                url = await asyncio.get_running_loop().run_in_executor(
                    None, presigned_url, self.region, self.language, self.rate, 300, self.extra)
                from yarl import URL  # aiohttp dependency; encoded=True keeps the SigV4 query intact
                async with aiohttp.ClientSession() as s, \
                        s.ws_connect(URL(url, encoded=True), heartbeat=None, max_msg_size=0) as ws:
                    log.info("transcribe stream open lang=%s rate=%d (%.0f ms)", self.language, self.rate,
                             (time.monotonic() - t0) * 1000)
                    backoff = 0.5
                    sender = asyncio.create_task(self._send(ws))
                    try:
                        await self._recv(ws)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.warning("transcribe stream error: %s", e)
            if self._closed:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
            log.info("transcribe stream reopening")

    async def _send(self, ws) -> None:
        while True:
            chunk = await self._q.get()
            if chunk is None:
                with _suppress():
                    await ws.send_bytes(audio_event(b""))   # end of stream
                return
            await ws.send_bytes(audio_event(chunk))

    async def _recv(self, ws) -> None:
        import aiohttp
        async for m in ws:
            if m.type != aiohttp.WSMsgType.BINARY:
                if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            for headers, payload in decode_events(m.data):
                mtype = headers.get(":message-type")
                if mtype == "exception":
                    raise RuntimeError(f"{headers.get(':exception-type')}: {payload[:300].decode(errors='replace')}")
                if headers.get(":event-type") != "TranscriptEvent":
                    continue
                doc = json.loads(payload or b"{}")
                for r in (doc.get("Transcript") or {}).get("Results") or []:
                    alts = r.get("Alternatives") or []
                    text = (alts[0].get("Transcript") if alts else "") or ""
                    if not text.strip():
                        continue
                    if r.get("IsPartial"):
                        await self.on_partial(text)
                    else:
                        self.last_final_at = time.monotonic()
                        await self.on_final(text)
        log.info("transcribe stream closed by server (code=%s)", ws.close_code)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
