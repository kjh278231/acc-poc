"""Voice session: continuous mic PCM in -> VAD/turn detection -> streaming STT -> Orchestrator -> TTS PCM out.

Implements app.voice.tts.AudioPipeline for one browser connection. The orchestrator / engines / rules are untouched:
a final transcript becomes InputMessage(kind=TEXT) and TEXT_CHUNK sentences become audio.

Timeline of one driver turn (all wall-clock, perf_counter):

    speech_end ──endpoint──> end_of_turn ──stt_final──> transcript ──engine──> first reply text ──tts──> first audio
    (VAD silence onset)      (silence window met)       (Transcribe final)     (TEXT_CHUNK)              (sent to browser)

`response_ms` = speech_end -> first reply audio is what the driver hears as the agent's reaction time
(plus network + browser playback buffer, which are not measured here).

Barge-in: speech_start while the agent is speaking or still working on its reply -> audio_clear to the browser,
stop TTS, cancel the in-flight turn. If the agent had not started speaking yet, the interrupted utterance is
carried over and prepended to the next one (the driver was simply still talking).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import ROOT, Settings, get_settings
from app.core.events import EventType, InputKind, InputMessage, OutputEvent
from app.core.orchestrator import Orchestrator
from app.voice.cache import FILLERS, FileAudioCache, prewarm_texts
from app.voice.stt import AudioFormat, SpeechToText
from app.voice.tts import TextToSpeech
from app.voice.vad import FrameVad, VadTurnDetector

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any] | bytes], None]
PREROLL_MS = 400
MAX_UTTERANCE_S = 30.0
PLAYBACK_MARGIN_S = 0.25


class VoiceOptions(BaseModel):
    engine: str = "jev_haiku"
    voice: str | None = None
    polly_engine: str | None = None
    barge_in: bool = True
    filler: bool = False
    silence_ms: int | None = None
    short_silence_ms: int | None = None
    speaking_rate: float = 1.0


def ev_json(e: OutputEvent) -> dict[str, Any]:
    d = e.model_dump(mode="json", exclude={"t"})
    if e.type == EventType.TURN_END:
        d["data"] = {"metrics": e.data.get("metrics")}
    return d


@dataclass
class Utterance:
    q: asyncio.Queue = field(default_factory=asyncio.Queue)
    finals: list[str] = field(default_factory=list)
    partial: str = ""
    audio_bytes: int = 0
    t_open: float = field(default_factory=time.perf_counter)
    task: asyncio.Task | None = None

    def text(self) -> str:
        return " ".join([*self.finals, self.partial]).strip()


@dataclass
class Clip:
    text: str
    filler: bool = False
    cached: bool = False
    q: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None


class Speech:
    """One spoken response: sentences are synthesised concurrently (cache first) and played strictly in order."""

    def __init__(self, vs: VoiceSession):
        self.vs = vs
        self.clips: list[Clip] = []
        self._order: asyncio.Queue[Clip | None] = asyncio.Queue()
        self.t_first_audio: float | None = None
        self.t_first_reply_audio: float | None = None
        self.synth_chars = 0
        self.cancelled = False
        self.runner = asyncio.create_task(self._run())

    def say(self, text: str, filler: bool = False) -> None:
        if self.cancelled:
            return
        c = Clip(text=text, filler=filler)
        c.task = asyncio.create_task(self._synth(c))
        self.clips.append(c)
        self._order.put_nowait(c)

    def close(self) -> None:
        self._order.put_nowait(None)

    async def wait(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self.runner)
        if self.runner.cancelled() and not self.cancelled:
            raise asyncio.CancelledError

    async def cancel(self) -> None:
        self.cancelled = True
        self.runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.runner
        # synthesis tasks are left to finish: they only fill the cache

    async def _synth(self, c: Clip) -> None:
        vs = self.vs
        try:
            pcm = vs.cache.get(c.text, vs.fmt, vs.voice, vs.polly_engine)
            if pcm is not None:
                c.cached = True
                c.q.put_nowait(pcm)
                return
            self.synth_chars += len(c.text)
            buf: list[bytes] = []
            async for b in vs.tts.stream(c.text, vs.fmt, voice=vs.voice, engine=vs.polly_engine,
                                         speaking_rate=vs.opts.speaking_rate):
                buf.append(b)
                c.q.put_nowait(b)
            vs.cache.put(c.text, vs.fmt, b"".join(buf), vs.voice, vs.polly_engine)
        except Exception as e:  # noqa: BLE001 — skip the sentence, keep the call going
            log.warning("TTS failed for %r: %s", c.text, e)
            vs.emit({"type": "error", "where": "tts", "message": str(e)[:300]})
        finally:
            c.q.put_nowait(None)

    async def _run(self) -> None:
        while (c := await self._order.get()) is not None:
            while (b := await c.q.get()) is not None:
                now = time.perf_counter()
                self.t_first_audio = self.t_first_audio or now
                if not c.filler:
                    self.t_first_reply_audio = self.t_first_reply_audio or now
                self.vs.send_audio(b)


class VoiceSession:
    def __init__(self, orch: Orchestrator, stt: SpeechToText, tts: TextToSpeech, cache: FileAudioCache, vad: FrameVad,
                 emit: Emit, opts: VoiceOptions, cfg: Settings | None = None, log_dir: Path | None = None):
        self.cfg = cfg or get_settings()
        self.orch, self.stt, self.tts, self.cache, self.emit, self.opts = orch, stt, tts, cache, emit, opts
        self.fmt = AudioFormat(sample_rate_hz=self.cfg.voice_sample_rate)
        self.voice = opts.voice or self.cfg.polly_voice
        self.polly_engine = opts.polly_engine or self.cfg.polly_engine
        self.min_speech_ms = self.cfg.vad_min_speech_ms
        self.det = VadTurnDetector(vad, sample_rate=self.fmt.sample_rate_hz, threshold=self.cfg.vad_threshold,
                                   neg_threshold=self.cfg.vad_neg_threshold, min_speech_ms=self.min_speech_ms,
                                   silence_ms=opts.silence_ms or self.cfg.vad_silence_ms,
                                   short_silence_ms=opts.short_silence_ms or self.cfg.vad_short_silence_ms)
        self.session_id: str | None = None
        self.preroll: deque[bytes] = deque()
        self.preroll_bytes = 0
        self.utt: Utterance | None = None
        self.carry: Utterance | None = None   # interrupted utterance whose turn never reached the speaker
        self.turn_task: asyncio.Task | None = None
        self.turn_utt: Utterance | None = None
        self.speech: Speech | None = None
        self.turn_spoke = False
        self.play_end = 0.0  # estimated end of browser playback of what was sent so far
        self.ended = False
        self.turn_no = 0
        self._last_level = 0.0
        self.log_path = (log_dir or ROOT / "results" / "voice") / f"{datetime.now():%Y%m%d}.jsonl"
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> str:
        self.session_id = self.orch.create_session(self.opts.engine).state.session_id
        self._spawn(self._prewarm())
        self.emit({"type": "ready", "session_id": self.session_id, "engine": self.opts.engine, "voice": self.voice,
                   "polly_engine": self.polly_engine})
        self.turn_task = asyncio.create_task(self._respond(InputMessage(session_id=self.session_id, kind=InputKind.START)))
        return self.session_id

    async def close(self) -> None:
        self.ended = True
        if self.utt:
            self.utt.q.put_nowait(None)
        await self._cancel_turn()
        for t in list(self._bg):
            t.cancel()

    # ------------------------------------------------------------------ input
    def speaking(self) -> bool:
        return time.perf_counter() < self.play_end + PLAYBACK_MARGIN_S

    def busy(self) -> bool:
        return self.speaking() or (self.turn_task is not None and not self.turn_task.done())

    async def feed(self, pcm: bytes) -> None:
        """Mic audio (16-bit mono LE PCM at fmt rate), any chunk size."""
        if self.ended or not pcm:
            return
        self._preroll(pcm)
        blocked = not self.opts.barge_in and self.busy()
        self.det.min_speech_ms = self.cfg.vad_barge_in_ms if self.busy() else self.min_speech_ms
        events = self.det.feed(pcm)
        now = time.perf_counter()
        if now - self._last_level > 0.1:
            self._last_level = now
            self.emit({"type": "vad", "p": round(self.det.last_prob, 2), "speech": self.det.in_speech})
        if blocked:  # half-duplex: ignore the mic while the agent is busy
            if self.det.in_speech:
                self.det.end_turn()
            return
        if self.utt is None and self.det.in_speech and any(e.kind == "speech_start" for e in events):
            if self.busy():
                await self.interrupt("barge_in")
            self._open_utterance()
        elif self.utt is not None:
            self.utt.q.put_nowait(pcm)
            self.utt.audio_bytes += len(pcm)
        if self.utt is not None:
            rule = self.det.is_end_of_turn(self.utt.text())
            if rule is None and now - self.utt.t_open > MAX_UTTERANCE_S:
                rule = "max_length"
            if rule:
                self._close_utterance(rule, now)

    async def say_text(self, text: str) -> None:
        """Typed input (debugging / no microphone): bypasses VAD and STT."""
        if self.ended or not text.strip():
            return
        if self.busy():
            await self.interrupt("typed_input")
        self.emit({"type": "user_final", "text": text, "typed": True})
        self.turn_utt, self.turn_spoke = None, False
        self.turn_task = asyncio.create_task(self._respond(InputMessage(session_id=self.session_id, text=text)))

    async def interrupt(self, reason: str = "user_speech") -> None:
        self.emit({"type": "audio_clear", "reason": reason})
        self.play_end = 0.0
        await self._cancel_turn()

    async def play_filler(self, key: str) -> None:
        if self.speech:
            self.speech.say(FILLERS[key], filler=True)

    # ------------------------------------------------------------------ utterances
    def _preroll(self, pcm: bytes) -> None:
        self.preroll.append(pcm)
        self.preroll_bytes += len(pcm)
        limit = self.fmt.sample_rate_hz * 2 * PREROLL_MS // 1000
        while self.preroll_bytes - len(self.preroll[0]) >= limit:
            self.preroll_bytes -= len(self.preroll.popleft())

    def _open_utterance(self) -> None:
        u = self.utt = Utterance()
        pre = b"".join(self.preroll)  # includes the current chunk
        u.q.put_nowait(pre)
        u.audio_bytes = len(pre)
        u.task = asyncio.create_task(self._run_stt(u))
        self.emit({"type": "speech_start"})

    async def _run_stt(self, u: Utterance) -> str:
        async def audio() -> AsyncIterator[bytes]:
            while (b := await u.q.get()) is not None:
                yield b

        hints = None
        try:
            async for tr in self.stt.stream(audio(), self.fmt, language="ko-KR", phrase_hints=hints):
                if tr.is_final:
                    u.finals.append(tr.text)
                    u.partial = ""
                else:
                    u.partial = tr.text
                self.emit({"type": "partial", "text": u.text()})
        except Exception as e:  # noqa: BLE001
            log.warning("STT failed: %s", e)
            self.emit({"type": "error", "where": "stt", "message": str(e)[:300]})
        return u.text()

    def _close_utterance(self, rule: str, now: float) -> None:
        u, self.utt = self.utt, None
        silence_ms = self.det.silence_run_ms
        speech_ms = self.det.speech_ms
        self.det.end_turn()
        u.q.put_nowait(None)
        carry, self.carry = self.carry, None
        timing = {"eot_rule": rule, "speech_ms": speech_ms, "t_speech_end": now - silence_ms / 1000, "t_eot": now,
                  "stt_audio_s": u.audio_bytes / 2 / self.fmt.sample_rate_hz}
        self.emit({"type": "end_of_turn", "rule": rule, "silence_ms": round(silence_ms)})
        self.turn_utt, self.turn_spoke = u, False
        self.turn_task = asyncio.create_task(self._finish_utterance(u, carry, timing))

    async def _finish_utterance(self, u: Utterance, carry: Utterance | None, timing: dict[str, Any]) -> None:
        try:
            text = await asyncio.wait_for(asyncio.shield(u.task), timeout=self.cfg.stt_final_timeout_s)
        except TimeoutError:
            text = u.text()
            timing["stt_timeout"] = True
        if carry is not None and carry.task is not None:
            try:
                prev = await asyncio.wait_for(asyncio.shield(carry.task), timeout=self.cfg.stt_final_timeout_s)
            except TimeoutError:
                prev = carry.text()
            if prev:
                text = f"{prev} {text}".strip()
                timing["carried"] = prev
        timing["t_final"] = time.perf_counter()
        if not text.strip():
            self.emit({"type": "noise", "audio_s": round(timing["stt_audio_s"], 2)})
            return
        self.emit({"type": "user_final", "text": text})
        await self._respond(InputMessage(session_id=self.session_id, text=text, received_at=timing["t_final"]), timing, u)

    # ------------------------------------------------------------------ output
    def send_audio(self, pcm: bytes) -> None:
        self.play_end = max(time.perf_counter(), self.play_end) + len(pcm) / 2 / self.fmt.sample_rate_hz
        self.emit(pcm)

    async def _respond(self, msg: InputMessage, timing: dict[str, Any] | None = None, u: Utterance | None = None) -> None:
        sp = self.speech = Speech(self)
        self.turn_spoke = False
        t_chunk: float | None = None
        metrics: dict[str, Any] | None = None
        finished = False
        filler = asyncio.create_task(self._filler(sp)) if self.opts.filler and timing else None
        try:
            async for ev in self.orch.handle(msg):
                self.emit(ev_json(ev))
                if ev.type == EventType.TEXT_CHUNK and ev.text:
                    if t_chunk is None:
                        t_chunk = time.perf_counter()
                        if filler:
                            filler.cancel()
                    sp.say(ev.text)
                    self.turn_spoke = True
                elif ev.type == EventType.TURN_END:
                    metrics = ev.data.get("metrics")
                elif ev.type == EventType.FINISH:
                    finished = True
            sp.close()
            await sp.wait()
        except asyncio.CancelledError:
            await sp.cancel()
            raise
        finally:
            if filler:
                filler.cancel()
        st = self.orch.get(self.session_id).state
        self.emit({"type": "state", "data": st.model_dump(mode="json")})
        if timing is not None and metrics is not None:
            self._record(timing, metrics, sp, t_chunk, u)
        if finished:
            await asyncio.sleep(max(0.0, self.play_end - time.perf_counter()))
            self.ended = True
            self.emit({"type": "call_ended", "outcome": st.finished, "reason": st.finish_reason})

    async def _filler(self, sp: Speech) -> None:
        await asyncio.sleep(self.cfg.filler_after_ms / 1000)
        sp.say(FILLERS["check"], filler=True)

    async def _cancel_turn(self) -> None:
        """Stop the agent's audio. A turn that has not spoken yet is cancelled and its utterance carried over;
        a turn that is already speaking keeps running silently so its tools / FINISH / state still complete."""
        if self.speech:
            await self.speech.cancel()
        t = self.turn_task
        if t and not t.done() and not self.turn_spoke:
            self.carry = self.turn_utt
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def _record(self, timing: dict[str, Any], m: dict[str, Any], sp: Speech, t_chunk: float | None,
                u: Utterance | None) -> None:
        def ms(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else round((b - a) * 1000, 1)

        cfg = self.cfg
        stt_cost = timing["stt_audio_s"] / 60 * cfg.transcribe_price_per_min
        tts_cost = sp.synth_chars / 1e6 * cfg.polly_price_per_mchar.get(self.polly_engine, 16.0)
        self.turn_no += 1
        rec = {
            "type": "voice_metrics", "session_id": self.session_id, "engine": self.opts.engine, "turn": self.turn_no,
            "voice": f"{self.voice}/{self.polly_engine}", "text": m.get("input"), "reply": m.get("reply"),
            "action": m.get("action"), "source": m.get("source"), "fallback": m.get("fallback"),
            "eot_rule": timing["eot_rule"], "speech_ms": round(timing["speech_ms"]), "carried": timing.get("carried"),
            "stt_timeout": timing.get("stt_timeout", False),
            "endpoint_ms": ms(timing["t_speech_end"], timing["t_eot"]),
            "stt_final_ms": ms(timing["t_eot"], timing["t_final"]),
            "engine_ms": ms(timing["t_final"], t_chunk),
            "tts_first_audio_ms": ms(t_chunk, sp.t_first_reply_audio),
            "first_audio_ms": ms(timing["t_speech_end"], sp.t_first_audio),
            "response_ms": ms(timing["t_speech_end"], sp.t_first_reply_audio),
            "filler": any(c.filler for c in sp.clips),
            "tts_clips": sum(not c.filler for c in sp.clips), "tts_cached": sum(c.cached and not c.filler for c in sp.clips),
            "tts_chars_synth": sp.synth_chars, "stt_audio_s": round(timing["stt_audio_s"], 2),
            "cost_usd": {"llm": m.get("cost_usd", 0.0), "stt": stt_cost, "tts": tts_cost,
                         "total": m.get("cost_usd", 0.0) + stt_cost + tts_cost},
            "agent": {k: m.get(k) for k in ("decision_ms", "first_response_ready_ms", "tool_ms", "ttft_ms", "intent",
                                            "confidence", "fallback_reason", "slots")},
        }
        self.emit(rec)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "ts": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("voice log write failed: %s", e)

    # ------------------------------------------------------------------ misc
    async def _prewarm(self) -> None:
        sem = asyncio.Semaphore(4)

        async def one(text: str) -> None:
            if self.cache.get(text, self.fmt, self.voice, self.polly_engine) is not None:
                return
            async with sem:
                buf = [b async for b in self.tts.stream(text, self.fmt, voice=self.voice, engine=self.polly_engine)]
                self.cache.put(text, self.fmt, b"".join(buf), self.voice, self.polly_engine)

        try:
            await asyncio.gather(*(one(t) for t in prewarm_texts()))
        except Exception as e:  # noqa: BLE001
            log.warning("TTS prewarm failed: %s", e)

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
