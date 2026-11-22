"""TTS clip cache (memory + disk). Implements app.voice.tts.AudioCache.

Template replies are split into sentences by the orchestrator, and most template sentences are static, so the
Jev+Haiku path (templates first) mostly plays cached clips. `prewarm_texts()` lists those static sentences.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import ROOT
from app.core.orchestrator import split_sentences
from app.services import responses
from app.voice.stt import AudioFormat

FILLERS = {"wait": "네, 잠시만요.", "check": "네, 확인해 볼게요."}


class FileAudioCache:
    def __init__(self, root: Path | None = None, engine: str = "neural"):
        self.root = root or ROOT / ".cache" / "tts"
        self.engine = engine
        self.mem: dict[str, bytes] = {}
        self.hits = self.misses = 0

    def _key(self, text: str, fmt: AudioFormat, voice: str | None, engine: str | None = None) -> str:
        raw = f"{voice}|{engine or self.engine}|{fmt.sample_rate_hz}|{text.strip()}"
        return hashlib.sha1(raw.encode()).hexdigest()  # noqa: S324 — cache key, not security

    def get(self, text: str, fmt: AudioFormat, voice: str | None = None, engine: str | None = None) -> bytes | None:
        k = self._key(text, fmt, voice, engine)
        pcm = self.mem.get(k)
        if pcm is None:
            p = self.root / f"{k}.pcm"
            if p.exists():
                pcm = self.mem[k] = p.read_bytes()
        if pcm is None:
            self.misses += 1
        else:
            self.hits += 1
        return pcm

    def put(self, text: str, fmt: AudioFormat, pcm: bytes, voice: str | None = None, engine: str | None = None) -> None:
        if not pcm:
            return
        k = self._key(text, fmt, voice, engine)
        self.mem[k] = pcm
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{k}.pcm").write_bytes(pcm)


def prewarm_texts() -> list[str]:
    """Static template sentences (no slot values / names) + fillers."""
    out: list[str] = list(FILLERS.values())
    static = [*responses.CLARIFY.values(), *responses.ASK.values(), responses.TMS_FAILED,
              "그 부분은 담당자가 따로 안내드리겠습니다.", "담당자에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요.",
              "안전이 가장 중요합니다. 확인된 내용은 등록하고, 담당자에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요.",
              "알겠습니다. 확인된 내용까지만 등록해 두겠습니다. 안전 운행하세요.",
              "특이사항 없으신 것으로 확인했습니다.", "목적지까지 도착 예정 시간이 얼마나 될까요?"]
    for t in static:
        for s in split_sentences(t):
            if s not in out:
                out.append(s)
    return out
