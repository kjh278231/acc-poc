"""Shared helpers for engines: Nova-style event emission, energy VAD, Polly TTS."""
from __future__ import annotations

import array
import asyncio
import base64
import itertools
import json
import logging
import math
import os
import re
from typing import AsyncIterator, Optional

log = logging.getLogger("bridge.engine")

_ids = itertools.count(1)


def rms(pcm: bytes) -> float:
    """Root mean square of 16-bit mono PCM."""
    if len(pcm) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not a:
        return 0.0
    return math.sqrt(sum(s * s for s in a) / len(a))


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear resampling of 16-bit mono PCM (up or down)."""
    if src == dst or not pcm:
        return pcm
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(s)
    if n == 0:
        return b""
    m = max(1, round(n * dst / src))
    out = array.array("h", bytes(m * 2))
    for i in range(m):
        pos = i * (n - 1) / max(1, m - 1) if m > 1 else 0.0
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        out[i] = int(s[lo] + (s[hi] - s[lo]) * (pos - lo))
    return out.tobytes()


class EnergyVad:
    """Tiny energy VAD on 16-bit PCM frames of any size.

    speech starts after ``start_ms`` of frames above ``threshold``; it ends after
    ``end_ms`` of frames below it. ``feed`` returns "start", "end" or None.
    """

    def __init__(self, sample_rate: int, threshold: float = 600.0, start_ms: int = 120, end_ms: int = 700):
        self.rate = sample_rate
        self.threshold = threshold
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.in_speech = False
        self._loud_ms = 0.0
        self._quiet_ms = 0.0

    def feed(self, pcm: bytes) -> Optional[str]:
        ms = len(pcm) / 2 / self.rate * 1000
        loud = rms(pcm) >= self.threshold
        if not self.in_speech:
            self._loud_ms = self._loud_ms + ms if loud else 0.0
            if self._loud_ms >= self.start_ms:
                self.in_speech = True
                self._quiet_ms = 0.0
                return "start"
            return None
        self._quiet_ms = 0.0 if loud else self._quiet_ms + ms
        if self._quiet_ms >= self.end_ms:
            self.in_speech = False
            self._loud_ms = 0.0
            return "end"
        return None


# Phone numbers (010-1234-5678, 0212345678) and ID-like digit runs of 5+ (claim/order numbers,
# hyphens allowed) are read digit by digit; a following unit (원, 년, 건, % ...) keeps it a quantity.
_PHONE = re.compile(r"(?<![\d-])0\d{1,2}-?\d{3,4}-?\d{4}(?![\d-])")
_ID_DIGITS = re.compile(r"(?<![\d,.-])\d(?:-?\d){4,}(?![\d,.-]|\s*(?:원|만|억|년|개월|월|일|시|분|초|명|개|건|번째|%|퍼센트))")


def ssml_numbers(text: str) -> str:
    """XML-escape text and wrap phone/ID numbers in say-as; returns text unchanged if nothing to mark."""
    from xml.sax.saxutils import escape
    if not (_PHONE.search(text) or _ID_DIGITS.search(text)) and escape(text) == text:
        return text
    out = _PHONE.sub(lambda m: f'<say-as interpret-as="telephone">{m.group()}</say-as>', escape(text))
    parts = re.split(r"(<say-as[^>]*>.*?</say-as>)", out)
    return "".join(p if p.startswith("<say-as") else
                   _ID_DIGITS.sub(lambda m: f'<say-as interpret-as="digits">{m.group()}</say-as>', p)
                   for p in parts)


class PollyTts:
    """Amazon Polly SynthesizeSpeech -> 16-bit mono PCM (8000 or 16000 Hz)."""

    def __init__(self, region: str, voice_id: str, engine: str = "neural", sample_rate: int = 16000,
                 language_code: Optional[str] = None):
        import boto3  # lazy: unit tests do not need AWS
        self.client = boto3.client("polly", region_name=region)
        self.voice_id = voice_id
        self.engine = engine  # noqa: also read by cascade's streaming synth
        self.sample_rate = sample_rate
        self.language_code = language_code
        # Speaking rate in percent (SSML prosody); 100 or empty sends plain text.
        self.rate = os.environ.get("A2A_TTS_RATE", "").strip().rstrip("%")

    def request(self, text: str, sample_rate: Optional[int] = None) -> dict:
        """SynthesizeSpeech kwargs; SSML only when numbers need marking up or a rate is set."""
        kw = dict(Text=text, VoiceId=self.voice_id, Engine=self.engine, OutputFormat="pcm",
                  SampleRate=str(sample_rate or self.sample_rate))
        body = ssml_numbers(text)
        if self.rate and self.rate != "100":
            body = f'<prosody rate="{self.rate}%">{body}</prosody>'
        if body != text:
            kw.update(TextType="ssml", Text=f"<speak>{body}</speak>")
        if self.language_code:
            kw["LanguageCode"] = self.language_code
        return kw

    def synth(self, text: str) -> bytes:
        r = self.client.synthesize_speech(**self.request(text))
        return r["AudioStream"].read()

    async def asynth(self, text: str) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(None, self.synth, text)


class EventEmitter:
    """Queues Nova-style events for server.py."""

    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()

    async def events(self) -> AsyncIterator[dict]:
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        await self._q.put(None)

    async def put(self, ev: dict) -> None:
        await self._q.put(ev)

    async def text(self, role: str, stage: str, text: str) -> None:
        cid = f"t{next(_ids)}"
        await self.put({"contentStart": {"contentId": cid, "type": "TEXT", "role": role,
                                         "additionalModelFields": json.dumps({"generationStage": stage})}})
        await self.put({"textOutput": {"contentId": cid, "role": role, "content": text}})
        await self.put({"contentEnd": {"contentId": cid, "type": "TEXT", "stopReason": "PARTIAL_TURN"}})

    async def interrupted(self) -> None:
        await self.put({"textOutput": {"contentId": f"i{next(_ids)}", "role": "ASSISTANT",
                                       "content": json.dumps({"interrupted": True})}})

    async def audio_block(self, pcm: bytes, chunk_bytes: int = 3200, stop: str = "END_TURN") -> None:
        """One assistant audio block: contentStart, audioOutput chunks, contentEnd."""
        cid = f"a{next(_ids)}"
        await self.put({"contentStart": {"contentId": cid, "type": "AUDIO", "role": "ASSISTANT"}})
        for i in range(0, len(pcm), chunk_bytes):
            await self.put({"audioOutput": {"contentId": cid,
                                            "content": base64.b64encode(pcm[i:i + chunk_bytes]).decode()}})
        await self.put({"contentEnd": {"contentId": cid, "type": "AUDIO", "role": "ASSISTANT", "stopReason": stop}})

    async def tool_use(self, name: str, args: dict) -> None:
        await self.put({"toolUse": {"toolName": name, "toolUseId": f"tool-{next(_ids)}",
                                    "content": json.dumps(args)}})
