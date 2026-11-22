"""Simulated phone call through the full voice pipeline (no browser): a synthetic driver voice is streamed in
real time as mic audio; server VAD -> Transcribe -> engine -> Polly run exactly as for the web UI.

  uv run python -m app.voice.simulate --engine jev_haiku --say "한 시간 반 정도 걸릴 것 같아요" --say "아니요 없어요"
  uv run python -m app.voice.simulate --engine both --reps 2       # default script, both engines

Driver lines are synthesised with a different Polly voice (default Hyunduk/generative) so the agent's STT hears a
voice it does not speak with. Each line is sent after the agent's audio has finished "playing" (+ think time).
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

from app.config import get_settings
from app.factory import build_orchestrator
from app.voice.aws import PollyTTS, TranscribeSTT
from app.voice.cache import FileAudioCache
from app.voice.pipeline import VoiceOptions, VoiceSession
from app.voice.stt import AudioFormat
from app.voice.vad import make_vad

DEFAULT_SCRIPT = ["한 시간 반 정도 걸릴 것 같아요", "고속도로가 많이 막히네요", "한 이십 분 정도 늦어질 것 같아요"]
FRAME = 1024  # 32 ms of 16 kHz PCM16


async def driver_audio(tts: PollyTTS, cache: FileAudioCache, text: str, fmt: AudioFormat, voice: str, engine: str) -> bytes:
    pcm = cache.get(text, fmt, voice, engine)
    if pcm is None:
        pcm = b"".join([b async for b in tts.stream(text, fmt, voice=voice, engine=engine)])
        cache.put(text, fmt, pcm, voice, engine)
    return pcm


async def run_call(engine: str, script: list[str], *, driver_voice: str, driver_engine: str, think_s: float,
                   opts: dict[str, Any], verbose: bool = True) -> list[dict[str, Any]]:
    cfg = get_settings()
    orch = build_orchestrator()
    fmt = AudioFormat(sample_rate_hz=cfg.voice_sample_rate)
    tts = PollyTTS(cfg.aws_region, cfg.polly_voice, cfg.polly_engine)
    cache = FileAudioCache()
    metrics: list[dict[str, Any]] = []
    turn_done = asyncio.Event()
    ended = asyncio.Event()

    def emit(item: dict[str, Any] | bytes) -> None:
        if isinstance(item, bytes):
            return
        t = item.get("type")
        if t == "voice_metrics":
            metrics.append(item)
        if t == "state":
            turn_done.set()
        if t == "call_ended":
            ended.set()
        if verbose and t in ("user_final", "text_done", "end_of_turn", "noise", "error", "call_ended"):
            print(f"  [{t}] {item.get('text') or item.get('message') or item.get('rule') or item.get('outcome') or ''}")
        if verbose and t == "voice_metrics":
            print(f"  [latency] endpoint {item['endpoint_ms']} + stt {item['stt_final_ms']} + engine {item['engine_ms']}"
                  f" + tts {item['tts_first_audio_ms']} = response {item['response_ms']} ms"
                  f" (cache {item['tts_cached']}/{item['tts_clips']}, rule {item['eot_rule']})")

    vs = VoiceSession(orch, TranscribeSTT(cfg.aws_region, cfg.transcribe_vocabulary), tts, cache, make_vad(cfg), emit,
                      VoiceOptions(engine=engine, **opts), cfg)

    async def stream(pcm: bytes) -> None:
        t0 = time.perf_counter()
        for i in range(0, len(pcm), FRAME):
            await vs.feed(pcm[i:i + FRAME])
            await asyncio.sleep(max(0.0, t0 + (i + FRAME) / 2 / fmt.sample_rate_hz - time.perf_counter()))

    silence = b"\0" * FRAME
    await vs.start()
    for line in script:
        await turn_done.wait()
        turn_done.clear()
        while vs.speaking():  # agent audio still "playing" in the browser: keep sending room silence
            await stream(silence)
        await stream(b"\0" * int(fmt.sample_rate_hz * 2 * think_s))
        if ended.is_set():
            break
        if verbose:
            print(f"  [driver] {line}")
        pcm = await driver_audio(tts, cache, line, fmt, driver_voice, driver_engine)
        await stream(pcm)
        deadline = time.perf_counter() + 10
        while not turn_done.is_set() and time.perf_counter() < deadline:  # silence until the agent replies
            await stream(silence * 4)
    await asyncio.wait_for(turn_done.wait(), timeout=15)
    await vs.close()
    return metrics


def summarize(rows: list[dict[str, Any]]) -> None:
    keys = ["endpoint_ms", "stt_final_ms", "engine_ms", "tts_first_audio_ms", "response_ms"]
    for eng in sorted({r["engine"] for r in rows}):
        rs = [r for r in rows if r["engine"] == eng]
        parts = []
        for k in keys:
            vals = [r[k] for r in rs if r.get(k) is not None]
            parts.append(f"{k.replace('_ms', '')} P50 {statistics.median(vals):.0f}" if vals else f"{k} –")
        cached = sum(r["tts_cached"] for r in rs), sum(r["tts_clips"] for r in rs)
        cost = sum(r["cost_usd"]["total"] for r in rs)
        print(f"{eng:11s} n={len(rs)} | " + " | ".join(parts) + f" | tts cache {cached[0]}/{cached[1]} | ${cost:.5f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="both", help="haiku_only | jev_haiku | both")
    ap.add_argument("--say", action="append", help="driver line (repeatable); default script otherwise")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--driver-voice", default="Hyunduk")
    ap.add_argument("--driver-engine", default="generative")
    ap.add_argument("--think", type=float, default=0.6, help="seconds of silence before each driver line")
    ap.add_argument("--filler", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    engines = ["haiku_only", "jev_haiku"] if a.engine == "both" else [a.engine]
    rows: list[dict[str, Any]] = []
    for rep in range(a.reps):
        for eng in engines:
            print(f"== {eng} (rep {rep + 1})")
            rows += await run_call(eng, a.say or DEFAULT_SCRIPT, driver_voice=a.driver_voice,
                                   driver_engine=a.driver_engine, think_s=a.think, opts={"filler": a.filler},
                                   verbose=not a.quiet)
    print()
    summarize(rows)


if __name__ == "__main__":
    asyncio.run(main())
