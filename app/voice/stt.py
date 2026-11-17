"""Speech-to-text interface (Phase 3). Interfaces only — no implementation in Phase 1.

Ownership (Connect "talk to the customer" with audioStreamingEnabled=true): Connect does NOT run STT, TTS or
turn detection for an external agent. This service owns them. The decision engines stay text-in / text-out:
the audio pipeline produces InputMessage(kind=TEXT) from a final transcript and consumes TEXT_CHUNK events.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AudioFormat:
    """Connect negotiates LINEAR_PCM, 16-bit, mono, 8000/16000/24000 Hz; fixed for the session."""
    encoding: str = "LINEAR_PCM"
    sample_rate_hz: int = 8000
    sample_size_bits: int = 16
    channels: int = 1

    @classmethod
    def from_connect(cls, cfg: dict) -> "AudioFormat":
        return cls(cfg.get("encoding", "LINEAR_PCM"), int(cfg["sampleRateHertz"]), int(cfg.get("sampleSizeBits", 16)),
                   int(cfg.get("channelCount", 1)))


@dataclass
class Transcript:
    text: str
    is_final: bool
    confidence: float | None = None
    start_ms: float | None = None   # relative to stream start, for latency accounting
    end_ms: float | None = None


@dataclass
class VadEvent:
    kind: str          # speech_start | speech_end
    at_ms: float


class SpeechToText(Protocol):
    """Streaming STT. Implementations: e.g. Amazon Transcribe streaming, Faster-Whisper server, Deepgram."""

    async def stream(self, audio: AsyncIterator[bytes], fmt: AudioFormat, *, language: str = "ko-KR",
                     phrase_hints: list[str] | None = None) -> AsyncIterator[Transcript]:
        """Yield partial transcripts as they stabilise and a final one per utterance."""
        ...


class TurnDetector(Protocol):
    """VAD + end-of-turn decision. Tunables: silence window, min speech, partial-transcript heuristics
    (e.g. a trailing '요/다/까' ends a Korean turn sooner)."""

    def feed(self, pcm: bytes, now_ms: float) -> list[VadEvent]: ...

    def is_end_of_turn(self, partial: Transcript | None, silence_ms: float) -> bool: ...
