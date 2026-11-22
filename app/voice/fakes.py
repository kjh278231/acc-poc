"""Offline stand-ins for tests and VOICE_MODE=mock (no AWS)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.voice.stt import AudioFormat, Transcript


class ScriptedSTT:
    """Returns the next scripted transcript for each utterance (after consuming its audio)."""

    def __init__(self, texts: list[str] | None = None, delay_s: float = 0.0):
        self.texts = list(texts or [])
        self.delay_s = delay_s
        self.utterances = 0
        self.audio_bytes: list[int] = []

    async def stream(self, audio: AsyncIterator[bytes], fmt: AudioFormat, *, language: str = "ko-KR",
                     phrase_hints: list[str] | None = None) -> AsyncIterator[Transcript]:
        n = 0
        text = self.texts.pop(0) if self.texts else ""
        async for chunk in audio:
            n += len(chunk)
            if text and n > 0:
                yield Transcript(text=text[: max(1, len(text) // 2)], is_final=False)
        self.utterances += 1
        self.audio_bytes.append(n)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if text:
            yield Transcript(text=text, is_final=True)


class SilentTTS:
    """Produces silence whose length follows the text (≈ 8 chars/s), in 100 ms chunks."""

    def __init__(self, delay_s: float = 0.0, ms_per_char: float = 20.0):
        self.delay_s, self.ms_per_char = delay_s, ms_per_char
        self.calls: list[str] = []

    async def stream(self, text: str, fmt: AudioFormat, *, voice: str | None = None, engine: str | None = None,
                     speaking_rate: float = 1.0) -> AsyncIterator[bytes]:
        self.calls.append(text)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        total = int(fmt.sample_rate_hz * len(text) * self.ms_per_char / 1000) * 2
        chunk = fmt.sample_rate_hz // 10 * 2
        for i in range(0, total, chunk):
            yield b"\0" * min(chunk, total - i)
