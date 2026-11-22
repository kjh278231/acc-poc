"""Voice pipeline (offline): energy VAD + scripted STT + silent TTS + mock engines."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from app.config import get_settings
from app.voice.cache import FileAudioCache
from app.voice.fakes import ScriptedSTT, SilentTTS
from app.voice.pipeline import VoiceOptions, VoiceSession
from app.voice.vad import EnergyVad, VadTurnDetector

RATE = 16000
CHUNK = 1024  # 32 ms


def tone(ms: int) -> bytes:
    t = np.arange(int(RATE * ms / 1000)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype(np.int16).tobytes()


def silence(ms: int) -> bytes:
    return b"\0" * (int(RATE * ms / 1000) * 2)


# ---------------------------------------------------------------------------- turn detector
def test_turn_detector_start_and_long_silence():
    det = VadTurnDetector(EnergyVad(), min_speech_ms=160, silence_ms=700, short_silence_ms=350)
    evs = det.feed(silence(300)) + det.feed(tone(100))
    assert not det.in_speech and not evs
    evs = det.feed(tone(200))
    assert det.in_speech and [e.kind for e in evs] == ["speech_start"]
    det.feed(silence(400))
    assert det.is_end_of_turn("한 시간") is None          # not a sentence ending, 400 < 700
    assert det.is_end_of_turn("한 시간 걸려요") == "short"  # sentence ending, 400 >= 350
    det.feed(silence(320))
    assert det.is_end_of_turn("한 시간") == "long"


def test_turn_detector_ignores_short_blips():
    det = VadTurnDetector(EnergyVad(), min_speech_ms=160)
    for _ in range(5):
        det.feed(tone(64) + silence(64))
    assert not det.in_speech


# ---------------------------------------------------------------------------- session helpers
class Rec:
    def __init__(self):
        self.items: list = []

    def __call__(self, item):
        self.items.append(item)

    def events(self, kind=None):
        return [i for i in self.items if isinstance(i, dict) and (kind is None or i.get("type") == kind)]

    def audio_bytes(self) -> int:
        return sum(len(i) for i in self.items if isinstance(i, bytes))


async def feed(vs: VoiceSession, pcm: bytes) -> None:
    for i in range(0, len(pcm), CHUNK):
        await vs.feed(pcm[i:i + CHUNK])
        await asyncio.sleep(0)


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def session(orch, tmp_path, texts, *, stt_delay=0.0, ms_per_char=0.5, **opts) -> tuple[VoiceSession, Rec, ScriptedSTT, SilentTTS]:
    rec, stt, tts = Rec(), ScriptedSTT(texts, delay_s=stt_delay), SilentTTS(ms_per_char=ms_per_char)
    vs = VoiceSession(orch, stt, tts, FileAudioCache(tmp_path / "tts"), EnergyVad(), rec,
                      VoiceOptions(engine=opts.pop("engine", "jev_haiku"), **opts), get_settings(), log_dir=tmp_path)
    return vs, rec, stt, tts


# ---------------------------------------------------------------------------- full turns
@pytest.mark.parametrize("engine", ["jev_haiku", "haiku_only"])
async def test_voice_turn_end_to_end(orch, tmp_path, engine):
    vs, rec, stt, tts = session(orch, tmp_path, ["한 시간 반 정도 걸려요"], engine=engine)
    await vs.start()
    await until(lambda: rec.events("state"))                     # greeting spoken
    assert rec.events("text_done")[0]["text"].startswith("안녕하세요")
    greet_audio = rec.audio_bytes()
    assert greet_audio > 0
    await until(lambda: not vs.speaking())
    await feed(vs, silence(200) + tone(600) + silence(800))
    await until(lambda: rec.events("voice_metrics"))
    assert rec.events("end_of_turn")[0]["rule"] in ("long", "short")
    assert rec.events("user_final")[0]["text"] == "한 시간 반 정도 걸려요"
    m = rec.events("voice_metrics")[0]
    for k in ("endpoint_ms", "stt_final_ms", "engine_ms", "tts_first_audio_ms", "response_ms"):
        assert m[k] is not None and m[k] >= 0, k
    assert m["response_ms"] >= m["endpoint_ms"]
    assert rec.audio_bytes() > greet_audio
    assert stt.audio_bytes[0] >= len(tone(600)) * 0.9            # preroll: speech onset not clipped
    st = orch.get(vs.session_id).state
    assert st.slots.eta_minutes == 90
    lines = (tmp_path / next(p.name for p in tmp_path.iterdir() if p.suffix == ".jsonl")).read_text().splitlines()
    assert json.loads(lines[-1])["engine"] == engine
    await vs.close()


async def test_noise_without_transcript_is_not_a_turn(orch, tmp_path):
    vs, rec, *_ = session(orch, tmp_path, [""])
    await vs.start()
    await until(lambda: rec.events("state"))
    await until(lambda: not vs.speaking())
    await feed(vs, tone(300) + silence(800))
    await until(lambda: rec.events("noise"))
    assert orch.get(vs.session_id).state.turn_count == 0
    await vs.close()


async def test_barge_in_clears_audio(orch, tmp_path):
    vs, rec, *_ = session(orch, tmp_path, ["잠깐만요"], ms_per_char=60)  # long greeting playback
    await vs.start()
    await until(lambda: rec.events("state"))
    assert vs.speaking()
    await feed(vs, tone(600))
    assert rec.events("audio_clear") and rec.events("speech_start")
    assert not vs.speaking()
    await vs.close()


async def test_half_duplex_ignores_speech_while_agent_talks(orch, tmp_path):
    vs, rec, *_ = session(orch, tmp_path, ["잠깐만요"], ms_per_char=60, barge_in=False)
    await vs.start()
    await until(lambda: rec.events("state"))
    await feed(vs, tone(600) + silence(800))
    assert not rec.events("audio_clear") and not rec.events("speech_start")
    await vs.close()


async def test_continued_speech_before_reply_is_carried_over(orch, tmp_path):
    vs, rec, *_ = session(orch, tmp_path, ["한 시간 정도요", "고속도로가 막혀서요"], stt_delay=0.3)
    await vs.start()
    await until(lambda: rec.events("state"))
    await until(lambda: not vs.speaking())
    await feed(vs, tone(500) + silence(750))           # turn 1 ends; STT final takes 300 ms
    assert rec.events("end_of_turn")
    await feed(vs, tone(500) + silence(750))           # driver keeps talking before the agent answered
    await until(lambda: rec.events("voice_metrics"))
    m = rec.events("voice_metrics")[-1]
    assert m["text"] == "한 시간 정도요 고속도로가 막혀서요"
    assert m["carried"] == "한 시간 정도요"
    assert orch.get(vs.session_id).state.turn_count == 1
    await vs.close()


async def test_typed_text_turn_and_cache(orch, tmp_path):
    vs, rec, _, tts = session(orch, tmp_path, [])
    await vs.start()
    await until(lambda: rec.events("state"))
    await vs.say_text("40분 걸려요")
    await until(lambda: len(rec.events("state")) >= 2)
    assert "40분" in rec.events("text_done")[-1]["text"]
    await vs.close()
    # a second session reuses the disk cache for the identical greeting tail sentence
    vs2, rec2, _, tts2 = session(orch, tmp_path, [])
    await vs2.start()
    await until(lambda: rec2.events("state"))
    assert "목적지까지 도착 예정 시간이 얼마나 될까요?" not in tts2.calls
    await vs2.close()


# ---------------------------------------------------------------------------- websocket
def test_voice_websocket(tmp_path):
    from fastapi.testclient import TestClient

    from app.adapters.voice_ws import VoiceDeps
    from app.factory import build_orchestrator
    from app.main import create_app
    from app.services.llm_mock import MockLLM
    from app.services.tms_mock import TmsMock

    o = build_orchestrator(llm=MockLLM(), tms=TmsMock(latency_ms=5))
    deps = VoiceDeps(stt=ScriptedSTT([]), tts=SilentTTS(ms_per_char=0.5), cache=FileAudioCache(tmp_path),
                     vad_factory=EnergyVad)
    with TestClient(create_app(o, voice_deps=deps)) as c:
        cfg = c.get("/api/voice/config").json()
        assert cfg["sample_rate"] == 16000 and "jev_haiku" in cfg["engines"]
        assert c.get("/voice").status_code == 200
        with c.websocket_connect("/voice/ws") as ws:
            ws.send_text(json.dumps({"type": "start", "engine": "jev_haiku"}))
            got_audio, kinds = False, []
            while "state" not in kinds:
                m = ws.receive()
                if m.get("bytes"):
                    got_audio = True
                elif m.get("text"):
                    kinds.append(json.loads(m["text"])["type"])
            assert kinds[0] == "ready" and got_audio
            ws.send_text(json.dumps({"type": "text", "text": "한 시간 걸려요"}))
            while True:
                m = ws.receive()
                if m.get("text") and json.loads(m["text"])["type"] == "state":
                    break
            ws.send_text(json.dumps({"type": "start", "engine": "nope"}))
            while True:
                m = ws.receive()
                if m.get("text") and json.loads(m["text"])["type"] == "error":
                    break
