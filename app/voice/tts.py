"""Text-to-speech + audio pipeline interfaces (Phase 3). Interfaces only.

How the pieces will connect to the existing (unchanged) orchestrator:

  Connect audio (A2A raw audio/lpcm parts)
      -> TurnDetector + SpeechToText             (voice/stt.py)
      -> InputMessage(kind=TEXT, text=final)     (same as the text path)
      -> Orchestrator.handle()  -> OutputEvent stream
      -> TEXT_CHUNK -> TextToSpeech.stream()     (sentence by sentence, cache first)
      -> A2A AUDIO_RESPONSE_CHUNK artifactUpdate (one artifact per spoken response, small frames, no FRAGMENT)
  Barge-in: TurnDetector speech_start while speaking -> AudioPipeline.interrupt():
      stop TTS, send INTERRUPTION to Connect, close the artifact without lastChunk, close the task,
      cancel the orchestrator turn (TMS writes are asyncio.shield-ed and are not rolled back).

Latency segments to record per turn (all already have a slot in the turn metrics / OTLP spans):
  stt_final_ms (speech_end -> final transcript), decide_ms, tool_ms, tts_first_audio_ms, network_ms.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.voice.stt import AudioFormat


class TextToSpeech(Protocol):
    async def stream(self, text: str, fmt: AudioFormat, *, voice: str | None = None,
                     speaking_rate: float = 1.0) -> AsyncIterator[bytes]:
        """Yield PCM chunks at `fmt` as soon as they are synthesised."""
        ...


class AudioCache(Protocol):
    """Pre-generated clips for template responses and filler ('네, 확인해 보겠습니다'). Key = (text, voice, fmt).
    Jev+Haiku benefits most: its replies are templates, so most turns can play a cached clip immediately."""

    def get(self, text: str, fmt: AudioFormat, voice: str | None = None) -> bytes | None: ...

    def put(self, text: str, fmt: AudioFormat, pcm: bytes, voice: str | None = None) -> None: ...


class AudioPipeline(Protocol):
    """Owns one Connect audio session: input stream -> turns -> orchestrator -> audio output."""

    async def run(self, audio_in: AsyncIterator[bytes], fmt_in: AudioFormat, fmt_out: AudioFormat) -> None: ...

    async def interrupt(self, reason: str = "user_speech") -> None: ...

    async def play_filler(self, key: str) -> None:
        """Play a cached filler while a slow tool / fallback model runs."""
        ...
