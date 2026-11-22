"""Live client for the cascade engine: plays Connect's side with synthesized Korean speech.

python3 tests/live_cascade_client.py ws://127.0.0.1:8080/a2a     (A2A_API_KEY in env)

Caller lines are Polly (Seoyeon, standard, 8 kHz) so the agent's Transcribe hears real
Korean. Scenario:
  1. greeting plays
  2. question -> wait for the full answer          (latency: caller end -> first audio)
  3. long question, then talk over the answer      (expect INTERRUPTION)
  4. "thanks, bye"                                 (expect FINISH COMPLETE)
Optional 5th arg "escalate" asks for a human instead (expect FINISH COMPLETE; the agent
files a TMS callback request, or logs CALLBACK_NOT_FILED when A2A_TMS_URL is unset).
Agent audio is saved to /tmp/cascade_agent_out.wav.
"""
import asyncio
import base64
import json
import os
import sys
import time
import uuid
import wave

import aiohttp
import boto3

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8080/a2a"
MODE = sys.argv[2] if len(sys.argv) > 2 else "complete"
EXT = "https://docs.aws.amazon.com/connect/a2a/ext/v1"
ET = EXT + "/eventType"
RATE = 8000
FRAME = RATE // 50
polly = boto3.client("polly", region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))


def say(text):
    r = polly.synthesize_speech(Text=text, VoiceId="Seoyeon", Engine="standard", OutputFormat="pcm",
                                SampleRate=str(RATE), LanguageCode="ko-KR")
    return r["AudioStream"].read()


def quiet(ms):
    return b"\x00\x00" * int(RATE * ms / 1000)


def msg(ctx, parts, event=None, rid=None):
    return json.dumps({"jsonrpc": "2.0", "method": "SendMessage", "id": rid or uuid.uuid4().hex[:8],
                       "params": {"message": {"messageId": uuid.uuid4().hex, "contextId": ctx, "role": "ROLE_USER",
                                              "parts": parts, "metadata": {ET: event} if event else {}}}})


async def main():
    lines = {
        "q1": say("안녕하세요. 주말에 서울에서 가볼 만한 실내 장소 하나만 추천해 주실래요?"),
        "q2": say("그럼 한국의 사계절 특징을 봄부터 겨울까지 자세하게 설명해 주세요."),
        "cut": say("아, 잠깐만요. 됐어요."),
        "bye": say("네, 궁금한 건 다 해결됐어요. 고맙습니다. 끊을게요."),
        "esc": say("상담원이랑 직접 통화하고 싶어요. 연결해 주세요."),
    }
    ctx = "ctx-" + uuid.uuid4().hex[:8]
    hdr = {"Authorization": "Bearer " + os.environ["A2A_API_KEY"], "A2A-Extensions": EXT,
           "traceparent": "00-" + uuid.uuid4().hex + "-" + uuid.uuid4().hex[:16] + "-01",
           "tracestate": "connect=parentSpanId:" + str(uuid.uuid4())}
    log, out_audio = [], bytearray()
    # play_end simulates Connect playing our audio in real time (the agent sends faster).
    st = {"last_audio": 0.0, "first_audio_after": None, "mark": None, "finish": None, "play_end": 0.0}
    t0 = time.monotonic()

    def ts():
        return f"{time.monotonic() - t0:5.1f}s"

    async with aiohttp.ClientSession() as s, s.ws_connect(URL, headers=hdr) as ws:
        init = {"initSession": {"instanceArn": "arn:aws:connect:x:1:instance/i",
                                "contactArn": "arn:aws:connect:x:1:instance/i/contact/c",
                                "supportedFinishTypes": ["COMPLETE", "ESCALATE"], "subscribeToTracingEvents": True,
                                "audioInputConfiguration": {"encoding": "LINEAR_PCM", "sampleRateHertz": 8000,
                                                            "sampleSizeBits": 16, "channelCount": 1},
                                "audioOutputConfiguration": {"encoding": "LINEAR_PCM", "sampleRateHertz": 16000,
                                                             "sampleSizeBits": 16, "channelCount": 1},
                                "history": []}}
        await ws.send_str(msg(ctx, [{"data": init, "mediaType": "application/json"}], "INIT_SESSION", "1"))
        await ws.send_str(msg(ctx, [{"data": {"channelState": {"state": "READY"}}, "mediaType": "application/json"}],
                              "CHANNEL_STATE"))
        done = asyncio.Event()

        async def reader():
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                f = json.loads(m.data)
                r = f.get("result", {})
                if "artifactUpdate" in r:
                    a = r["artifactUpdate"]["artifact"]
                    et = (a.get("metadata") or {}).get(ET)
                    if et == "AUDIO_RESPONSE_CHUNK":
                        now = time.monotonic()
                        for p in a.get("parts", []):
                            if p.get("raw"):
                                pcm = base64.b64decode(p["raw"])
                                out_audio.extend(pcm)
                                st["play_end"] = max(st["play_end"], now) + len(pcm) / 32000
                        if st["mark"] is not None and st["first_audio_after"] is None:
                            st["first_audio_after"] = now - st["mark"]
                            log.append(f"{ts()} >>> first agent audio {st['first_audio_after'] * 1000:.0f} ms "
                                       f"after caller audio ended")
                        st["last_audio"] = now
                        if not r["artifactUpdate"].get("append"):
                            log.append(f"{ts()} AUDIO start")
                        if r["artifactUpdate"].get("lastChunk"):
                            log.append(f"{ts()} AUDIO last")
                    elif et == "TRACING_SPAN":
                        doc = json.dumps(a)
                        log.append(f"{ts()} TRACING_SPAN ({len(doc)} bytes, chat={'chat ' in doc}, "
                                   f"tool={'execute_tool' in doc})")
                    else:
                        log.append(f"{ts()} {et}")
                elif "statusUpdate" in r:
                    stt = r["statusUpdate"]["status"]
                    fin = (stt.get("message") or {}).get("parts", [{}])[0].get("data", {}).get("finish")
                    log.append(f"{ts()} STATUS {stt['state']}" + (f" FINISH={fin}" if fin else ""))
                    if fin:
                        st["finish"] = fin
                        done.set()
                elif "message" in r:
                    et = (r['message'].get('metadata') or {}).get(ET)
                    if et == "INTERRUPTION":
                        st["play_end"] = time.monotonic()      # Connect flushes queued audio
                    log.append(f"{ts()} MESSAGE {et}")
            done.set()

        rt = asyncio.create_task(reader())

        async def play(pcm):
            for i in range(0, len(pcm), FRAME * 2):
                if done.is_set() or ws.closed:
                    return
                await ws.send_str(msg(ctx, [{"raw": base64.b64encode(pcm[i:i + FRAME * 2]).decode(),
                                             "mediaType": "audio/lpcm"}]))
                await asyncio.sleep(0.02)

        async def utter(key, label):
            log.append(f"{ts()} caller: {label}")
            await play(lines[key])
            st["mark"], st["first_audio_after"] = time.monotonic(), None

        async def wait_answer(max_s=20.0, gap=1.5):
            """Stream silence until the agent has answered and gone quiet for `gap` s."""
            start = time.monotonic()
            while time.monotonic() - start < max_s and not done.is_set():
                await play(quiet(200))
                now = time.monotonic()
                if st["first_audio_after"] is not None and now - st["last_audio"] > 0.5 \
                        and now > st["play_end"] + gap:
                    return

        await play(quiet(1000))
        await wait_answer(10)                                      # greeting
        await utter("q1", "q1 indoor place")
        await wait_answer()
        await utter("q2", "q2 long answer")
        start = time.monotonic()
        while st["first_audio_after"] is None and time.monotonic() - start < 10:
            await play(quiet(200))
        await play(quiet(1500))                                    # let it talk 1.5 s
        await utter("cut", "barge-in")
        await wait_answer(15)
        await utter("esc" if MODE == "escalate" else "bye", MODE)
        await wait_answer(15)
        await play(quiet(3000))
        try:
            await asyncio.wait_for(done.wait(), 12)
        except asyncio.TimeoutError:
            log.append("TIMEOUT waiting for FINISH")
        rt.cancel()
    with wave.open("/tmp/cascade_agent_out.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(out_audio))
    print("\n".join(log))
    print(f"RESULT finish={st['finish']} agent_audio={len(out_audio) / 32000:.1f}s")


asyncio.run(main())
