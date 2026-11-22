"""loopback engine: verifies the Connect <-> ECS audio path with no AI model.

Per call:
* READY      -> greeting (Polly, or a short tone when no TTS voice is set).
* caller turn -> energy VAD decides start/end; the agent says a short prefix and
                 plays the caller's own audio back (echo).
* barge-in   -> caller speech while our audio is still playing emits the Nova-style
                 {"interrupted": true} text, which server.py turns into INTERRUPTION.
* after ``max_turns`` echo turns -> goodbye + end_call COMPLETE (FINISH).
  Saying nothing for ``idle_finish_s`` after a turn also ends the call.

Same surface as nova.NovaSonicSession: open / close / events / send_audio / send_tool_result.
"""
from __future__ import annotations

import array
import asyncio
import logging
import math
import os
import time
from typing import AsyncIterator, Optional

from .base import EnergyVad, EventEmitter, PollyTts, resample

log = logging.getLogger("bridge.loopback")

PHRASES = {
    "ko-KR": {
        "greeting": "안녕하세요. 루프백 테스트 에이전트입니다. 말씀하시면 그대로 들려드릴게요.",
        "prefix": "이렇게 말씀하셨어요.",
        "goodbye": "테스트를 마칩니다. 감사합니다.",
    },
    "id-ID": {
        "greeting": "Halo, ini agen uji loopback. Silakan bicara, saya akan memutar ulang suara Anda.",
        "prefix": "Anda mengatakan.",
        "goodbye": "Pengujian selesai. Terima kasih.",
    },
    "en-US": {
        "greeting": "Hello, this is the loopback test agent. Speak and I will play it back.",
        "prefix": "You said.",
        "goodbye": "The test is over. Thank you.",
    },
}


def _tone(ms: int, rate: int, hz: float = 660.0, amp: int = 6000) -> bytes:
    n = int(rate * ms / 1000)
    a = array.array("h", (int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))
    return a.tobytes()


class LoopbackSession:
    def __init__(self, *, region: str, voice_id: str, output_sample_rate: int, input_sample_rate: int,
                 language: str = "ko-KR", tts_engine: str = "neural", max_turns: int = 3,
                 vad_threshold: float = 600.0, end_ms: int = 700, max_echo_s: float = 8.0,
                 idle_finish_s: float = 30.0, **_ignored):
        self.out_rate = output_sample_rate
        self.in_rate = input_sample_rate
        self.lang = language if language in PHRASES else "en-US"
        self.max_turns = max_turns
        self.max_echo_bytes = int(max_echo_s * input_sample_rate) * 2
        self.idle_finish_s = idle_finish_s
        self.em = EventEmitter()
        self.vad = EnergyVad(input_sample_rate, threshold=vad_threshold, end_ms=end_ms)
        self.tts: Optional[PollyTts] = None
        if voice_id and voice_id.lower() not in ("none", "tone"):
            self.tts = PollyTts(region, voice_id, engine=tts_engine, sample_rate=output_sample_rate)
        self._buf = bytearray()
        self._preroll: list[bytes] = []
        self._turns = 0
        self._playing_until = 0.0
        self._busy = False
        self._finishing = False
        self._last_activity = time.monotonic()
        self._watchdog: Optional[asyncio.Task] = None
        log.info("loopback engine lang=%s in=%dHz out=%dHz tts=%s", self.lang, self.in_rate, self.out_rate,
                 voice_id or "tone")

    # --- lifecycle -----------------------------------------------------------
    async def open(self, prime_greeting: bool = True) -> None:
        if prime_greeting:
            await self._speak_turn(caller_text="", agent_text=PHRASES[self.lang]["greeting"])
        self._watchdog = asyncio.create_task(self._idle_watchdog())

    async def close(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        await self.em.close()

    def events(self) -> AsyncIterator[dict]:
        return self.em.events()

    async def send_tool_result(self, tool_use_id: str, result: dict, is_error: bool = False) -> None:
        return None

    # --- inbound caller audio (called for every ~20 ms frame) ---------------
    async def send_audio(self, pcm: bytes) -> None:
        if self._finishing:
            return
        edge = self.vad.feed(pcm)
        if self.vad.in_speech or edge == "end":
            self._buf.extend(pcm)
        else:
            self._preroll.append(pcm)          # keep ~200 ms before speech starts
            if len(self._preroll) > 10:
                self._preroll.pop(0)
        if edge == "start":
            self._last_activity = time.monotonic()
            self._buf[:0] = b"".join(self._preroll)
            self._preroll.clear()
            if time.monotonic() < self._playing_until:
                log.info("barge-in detected")
                self._playing_until = 0.0
                await self.em.interrupted()
        elif edge == "end":
            self._last_activity = time.monotonic()
            captured = bytes(self._buf[: self.max_echo_bytes])
            self._buf.clear()
            if not self._busy:
                asyncio.create_task(self._on_caller_turn(captured))

    # --- turns ---------------------------------------------------------------
    async def _on_caller_turn(self, captured: bytes) -> None:
        self._busy = True
        try:
            self._turns += 1
            secs = len(captured) / 2 / self.in_rate
            caller_text = f"[caller audio {secs:.1f}s]"
            if self._turns > self.max_turns:
                await self._goodbye(caller_text)
                return
            echo = resample(captured, self.in_rate, self.out_rate)
            await self._speak_turn(caller_text=caller_text, agent_text=PHRASES[self.lang]["prefix"],
                                   tail=echo, tail_label=f"[echo {secs:.1f}s]")
        except Exception:  # noqa: BLE001
            log.exception("loopback turn failed")
        finally:
            self._busy = False

    async def _goodbye(self, caller_text: str) -> None:
        self._finishing = True
        if caller_text:
            await self.em.text("USER", "FINAL", caller_text)
        await self.em.tool_use("end_call", {"outcome": "COMPLETE", "summary": "loopback test finished"})
        await self._speak_turn(caller_text="", agent_text=PHRASES[self.lang]["goodbye"])

    async def _speak_turn(self, *, caller_text: str, agent_text: str, tail: bytes = b"",
                          tail_label: str = "") -> None:
        if caller_text:
            await self.em.text("USER", "FINAL", caller_text)
        pcm = await self._tts(agent_text)
        pcm += tail
        await self.em.text("ASSISTANT", "SPECULATIVE", f"{agent_text} {tail_label}".strip())
        await self.em.audio_block(pcm, chunk_bytes=self.out_rate // 10 * 2)
        dur = len(pcm) / 2 / self.out_rate
        self._playing_until = max(self._playing_until, time.monotonic()) + dur
        await self.em.put({"usageEvent": {"totalInputTokens": 0, "totalOutputTokens": 0}})

    async def _tts(self, text: str) -> bytes:
        if self.tts is None:
            return _tone(300, self.out_rate) + b"\x00\x00" * (self.out_rate // 5)
        try:
            return await self.tts.asynth(text)
        except Exception:  # noqa: BLE001 - keep the call alive without TTS
            log.exception("Polly failed; using a tone")
            return _tone(300, self.out_rate)

    async def _idle_watchdog(self) -> None:
        try:
            while not self._finishing:
                await asyncio.sleep(1.0)
                idle = time.monotonic() - max(self._last_activity, self._playing_until)
                if idle >= self.idle_finish_s and not self._busy and not self.vad.in_speech:
                    log.info("no caller speech for %.0fs; finishing", idle)
                    await self._goodbye("")
                    return
        except asyncio.CancelledError:
            pass
