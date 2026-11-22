"""Probe the hand-rolled Transcribe WebSocket client with Polly speech (8 kHz ko-KR).

python3 tests/stt_probe.py [stabilization: high|medium|low|none]
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import boto3  # noqa: E402

from bridge.engines.transcribe_ws import TranscribeStream  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
STAB = sys.argv[1] if len(sys.argv) > 1 else "high"


async def main():
    pcm = boto3.client("polly", region_name=REGION).synthesize_speech(
        Text="안녕하세요. 내일 오후 세 시에 예약을 바꾸고 싶어요.", VoiceId="Seoyeon", Engine="standard",
        OutputFormat="pcm", SampleRate="8000")["AudioStream"].read()
    t0 = time.monotonic()
    end = {"t": 0.0}

    async def partial(t):
        print(f"{time.monotonic() - t0:5.2f}s partial: {t}")

    async def final(t):
        lag = (time.monotonic() - end["t"]) * 1000 if end["t"] else -1
        print(f"{time.monotonic() - t0:5.2f}s FINAL: {t}   (audio end -> final {lag:.0f} ms)")

    s = TranscribeStream(region=REGION, language="ko-KR", sample_rate=8000, on_partial=partial, on_final=final,
                         stabilization=None if STAB == "none" else STAB)
    s.start()
    audio = pcm + b"\x00\x00" * 8000 * 4
    for i in range(0, len(audio), 320):
        s.feed(audio[i:i + 320])
        if i == len(pcm) - len(pcm) % 320:
            end["t"] = time.monotonic()
        await asyncio.sleep(0.02)
    await s.close()


asyncio.run(main())
