"""Real-time client that plays Connect's side against a running agent (loopback engine).

python3 tests/live_loopback_client.py ws://127.0.0.1:8080/a2a   (A2A_API_KEY in env)

Sends INIT_SESSION (8 kHz in / 16 kHz out proposed), READY, then 20 ms frames in real time:
3 s quiet, then turns of 1.2 s tone + 1.5 s quiet. During the 2nd reply it talks over the
agent to provoke a barge-in. Prints a summary of every frame type the agent sends.
"""
import array
import asyncio
import base64
import json
import math
import os
import sys
import time
import uuid

import aiohttp

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8080/a2a"
EXT = "https://docs.aws.amazon.com/connect/a2a/ext/v1"
ET = EXT + "/eventType"
RATE = 8000
FRAME = RATE // 50


def tone(ms, amp=5000):
    n = int(RATE * ms / 1000)
    return array.array("h", (int(amp * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(n))).tobytes()


def quiet(ms):
    return b"\x00\x00" * int(RATE * ms / 1000)


def msg(ctx, parts, event=None, rid=None):
    return json.dumps({"jsonrpc": "2.0", "method": "SendMessage", "id": rid or uuid.uuid4().hex[:8],
                       "params": {"message": {"messageId": uuid.uuid4().hex, "contextId": ctx, "role": "ROLE_USER",
                                              "parts": parts, "metadata": {ET: event} if event else {}}}})


async def main():
    ctx = "ctx-" + uuid.uuid4().hex[:8]
    hdr = {"Authorization": "Bearer " + os.environ["A2A_API_KEY"], "A2A-Extensions": EXT,
           "traceparent": "00-" + uuid.uuid4().hex + "-" + uuid.uuid4().hex[:16] + "-01",
           "tracestate": "connect=parentSpanId:" + str(uuid.uuid4())}
    log = []
    t0 = time.monotonic()
    async with aiohttp.ClientSession() as s, s.ws_connect(URL, headers=hdr) as ws:
        init = {"initSession": {"instanceArn": "arn:aws:connect:x:1:instance/i", "contactArn": "arn:aws:connect:x:1:instance/i/contact/c",
                                "supportedFinishTypes": ["COMPLETE", "ESCALATE"], "subscribeToTracingEvents": True,
                                "audioInputConfiguration": {"encoding": "LINEAR_PCM", "sampleRateHertz": 8000, "sampleSizeBits": 16, "channelCount": 1},
                                "audioOutputConfiguration": {"encoding": "LINEAR_PCM", "sampleRateHertz": 16000, "sampleSizeBits": 16, "channelCount": 1},
                                "history": []}}
        await ws.send_str(msg(ctx, [{"data": init, "mediaType": "application/json"}], "INIT_SESSION", "1"))
        await ws.send_str(msg(ctx, [{"data": {"channelState": {"state": "READY"}}, "mediaType": "application/json"}], "CHANNEL_STATE"))

        done = asyncio.Event()

        async def reader():
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                f = json.loads(m.data)
                r = f.get("result", {})
                ts = f"{time.monotonic() - t0:5.1f}s"
                if "artifactUpdate" in r:
                    a = r["artifactUpdate"]["artifact"]
                    et = (a.get("metadata") or {}).get(ET)
                    if et == "AUDIO_RESPONSE_CHUNK":
                        if not r["artifactUpdate"].get("append"):
                            log.append(f"{ts} AUDIO start task={r['artifactUpdate']['taskId'][:6]}")
                        if r["artifactUpdate"].get("lastChunk"):
                            log.append(f"{ts} AUDIO last")
                    else:
                        log.append(f"{ts} {et}")
                elif "statusUpdate" in r:
                    st = r["statusUpdate"]["status"]
                    fin = (st.get("message") or {}).get("parts", [{}])[0].get("data", {}).get("finish")
                    log.append(f"{ts} STATUS {st['state']}" + (f" FINISH={fin}" if fin else ""))
                    if fin:
                        done.set()
                elif "message" in r:
                    log.append(f"{ts} MESSAGE {(r['message'].get('metadata') or {}).get(ET)}")
            done.set()

        rt = asyncio.create_task(reader())

        async def play(pcm):
            for i in range(0, len(pcm), FRAME * 2):
                if done.is_set() or ws.closed:
                    return
                await ws.send_str(msg(ctx, [{"raw": base64.b64encode(pcm[i:i + FRAME * 2]).decode(), "mediaType": "audio/lpcm"}]))
                await asyncio.sleep(0.02)

        await play(quiet(6000))                      # greeting plays
        await play(tone(1200) + quiet(1500))        # turn 1
        await play(quiet(2500) + tone(1200) + quiet(1500))  # turn 2, sent while turn-1 reply still plays -> barge-in
        await play(quiet(6000) + tone(1200) + quiet(1500))  # turn 3
        await play(quiet(6000) + tone(1200) + quiet(1500))  # turn 4 -> goodbye + FINISH
        await play(quiet(8000))
        try:
            await asyncio.wait_for(done.wait(), 15)
        except asyncio.TimeoutError:
            log.append("TIMEOUT waiting for FINISH")
        rt.cancel()
    print("\n".join(log))


asyncio.run(main())
