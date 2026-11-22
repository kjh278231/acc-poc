"""Offline check of the loopback engine (no AWS, no aiohttp): python tests/test_loopback_offline.py

Feeds 20 ms frames like Connect does: silence, a 1 s tone ("speech"), silence, and checks
the engine emits greeting, echo turns, a barge-in and finally end_call + goodbye.
"""
import array
import asyncio
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge.engines.loopback import LoopbackSession  # noqa: E402

RATE = 8000
FRAME = RATE // 50  # 20 ms


def tone(ms, amp=4000):
    n = int(RATE * ms / 1000)
    return array.array("h", (int(amp * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(n))).tobytes()


def silence(ms):
    return b"\x00\x00" * int(RATE * ms / 1000)


async def feed(s, pcm):
    for i in range(0, len(pcm), FRAME * 2):
        await s.send_audio(pcm[i:i + FRAME * 2])
        await asyncio.sleep(0.0005)


async def main():
    s = LoopbackSession(region="ap-northeast-2", voice_id="tone", output_sample_rate=16000,
                        input_sample_rate=RATE, language="ko-KR", max_turns=2, idle_finish_s=999)
    seen = []

    async def drain():
        async for ev in s.events():
            k = next(iter(ev))
            if k == "audioOutput":
                continue
            v = ev[k]
            seen.append((k, v.get("role"), v.get("stopReason"), v.get("toolName"), (v.get("content") or "")[:40]
                         if k == "textOutput" else ""))

    t = asyncio.create_task(drain())
    await s.open()
    for _ in range(3):  # 2 echo turns + 1 goodbye turn
        await feed(s, silence(300) + tone(1000) + silence(900))
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.3)
    await s.close()
    await t
    for e in seen:
        print(e)
    kinds = [e[0] for e in seen]
    assert kinds.count("contentEnd") >= 4
    assert any(e[0] == "toolUse" and e[3] == "end_call" for e in seen), "no end_call"
    assert any("interrupted" in e[4] for e in seen), "no barge-in"
    print("OK")


asyncio.run(main())
