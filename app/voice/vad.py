"""Server-side VAD + end-of-turn detection.

The browser streams raw 16 kHz PCM continuously; deciding *when the driver started and stopped talking* happens here,
so the same code can later sit behind a Connect `audio/lpcm` stream (no browser there).

* SileroVad   — Silero VAD v6 ONNX (CPU, ~0.1 ms per 32 ms frame). Model is downloaded once to .cache/models/.
* EnergyVad   — RMS threshold. Used by tests and as a fallback when the model can't be loaded.
* VadTurnDetector — frame probabilities -> speech_start / speech_end with hysteresis, plus the end-of-turn rule:
  long silence always ends a turn; a shorter silence ends it when the running transcript already ends like a
  finished Korean sentence (…요 / …다 / …까 / punctuation), which cuts ~350 ms off typical answers.
"""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.request
from pathlib import Path
from typing import Protocol

import numpy as np

from app.config import ROOT, Settings, get_settings
from app.voice.stt import Transcript, VadEvent

log = logging.getLogger(__name__)

FRAME_SAMPLES = 512  # Silero window at 16 kHz (32 ms)
SENTENCE_END = re.compile(r"(요|다|까|죠|네)[.?!]?\s*$|[.?!]\s*$")


class FrameVad(Protocol):
    def prob(self, frame: np.ndarray) -> float: ...  # float32 [-1, 1], FRAME_SAMPLES long

    def reset(self) -> None: ...


class EnergyVad:
    def __init__(self, rms_threshold: float = 0.02):
        self.rms_threshold = rms_threshold

    def prob(self, frame: np.ndarray) -> float:
        return 1.0 if float(np.sqrt(np.mean(frame * frame))) >= self.rms_threshold else 0.0

    def reset(self) -> None:
        pass


class SileroVad:
    CONTEXT = 64  # samples of the previous window the v5+ model expects in front of each 512-sample window

    def __init__(self, model_path: Path, sample_rate: int = 16000):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.sr = np.array(sample_rate, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, self.state = self.session.run(None, {"input": x, "state": self.state, "sr": self.sr})
        self.context = x[:, -self.CONTEXT:]
        return float(out[0][0])


def ensure_model(cfg: Settings | None = None) -> Path:
    cfg = cfg or get_settings()
    path = ROOT / ".cache" / "models" / "silero_vad.onnx"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("downloading Silero VAD model from %s", cfg.vad_model_url)
        data = urllib.request.urlopen(cfg.vad_model_url, timeout=30).read()  # noqa: S310 (pinned https URL)
        digest = hashlib.sha256(data).hexdigest()
        if cfg.vad_model_sha256 and digest != cfg.vad_model_sha256:
            raise RuntimeError(f"Silero VAD model checksum mismatch: {digest}")
        path.write_bytes(data)
    return path


def make_vad(cfg: Settings | None = None) -> FrameVad:
    cfg = cfg or get_settings()
    try:
        return SileroVad(ensure_model(cfg), cfg.voice_sample_rate)
    except Exception as e:  # noqa: BLE001 — keep the voice page usable, but say so loudly
        log.warning("Silero VAD unavailable (%s); falling back to energy VAD", e)
        return EnergyVad()


class VadTurnDetector:
    """Implements app.voice.stt.TurnDetector. Times are audio-clock ms (samples received / rate)."""

    def __init__(self, vad: FrameVad, *, sample_rate: int = 16000, threshold: float = 0.5, neg_threshold: float = 0.35,
                 min_speech_ms: int = 160, silence_ms: int = 700, short_silence_ms: int = 350):
        self.vad, self.rate = vad, sample_rate
        self.threshold, self.neg_threshold = threshold, neg_threshold
        self.min_speech_ms, self.silence_ms, self.short_silence_ms = min_speech_ms, silence_ms, short_silence_ms
        self.frame_ms = FRAME_SAMPLES * 1000 / sample_rate
        self._buf = np.zeros(0, dtype=np.float32)
        self.in_speech = False
        self.speech_run_ms = 0.0   # consecutive speech before a turn opens
        self.silence_run_ms = 0.0  # consecutive silence inside a turn
        self.speech_ms = 0.0       # total speech in the current turn
        self.last_prob = 0.0
        self.clock_ms = 0.0

    def feed(self, pcm: bytes, now_ms: float | None = None) -> list[VadEvent]:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, samples])
        events: list[VadEvent] = []
        while len(self._buf) >= FRAME_SAMPLES:
            frame, self._buf = self._buf[:FRAME_SAMPLES], self._buf[FRAME_SAMPLES:]
            self.clock_ms += self.frame_ms
            p = self.last_prob = self.vad.prob(frame)
            if not self.in_speech:
                self.speech_run_ms = self.speech_run_ms + self.frame_ms if p >= self.threshold else 0.0
                if self.speech_run_ms >= self.min_speech_ms:
                    self.in_speech, self.silence_run_ms, self.speech_ms = True, 0.0, self.speech_run_ms
                    events.append(VadEvent("speech_start", self.clock_ms - self.speech_run_ms))
            else:
                if p < self.neg_threshold:
                    if self.silence_run_ms == 0.0:
                        events.append(VadEvent("speech_end", self.clock_ms - self.frame_ms))
                    self.silence_run_ms += self.frame_ms
                elif p >= self.threshold or self.silence_run_ms == 0.0:
                    if self.silence_run_ms > 0.0:
                        events.append(VadEvent("speech_resume", self.clock_ms))
                    self.silence_run_ms = 0.0
                    self.speech_ms += self.frame_ms
        return events

    def is_end_of_turn(self, partial: Transcript | str | None, silence_ms: float | None = None) -> str | None:
        """Return the rule that ended the turn ('long' | 'short'), or None to keep listening."""
        if not self.in_speech:
            return None
        silence = self.silence_run_ms if silence_ms is None else silence_ms
        if silence >= self.silence_ms:
            return "long"
        text = partial.text if isinstance(partial, Transcript) else (partial or "")
        if silence >= self.short_silence_ms and SENTENCE_END.search(text.strip()):
            return "short"
        return None

    def end_turn(self) -> None:
        self.in_speech = False
        self.speech_run_ms = self.silence_run_ms = self.speech_ms = 0.0

    def reset(self) -> None:
        self.end_turn()
        self.vad.reset()
        self._buf = np.zeros(0, dtype=np.float32)
