"""Voice WebSocket for the browser mic UI (/voice).

Client -> server:  {"type":"start", ...VoiceOptions}  |  binary PCM16LE mono @ VOICE_SAMPLE_RATE  |
                   {"type":"text","text":...} (typed turn)  |  {"type":"stop"}
Server -> client:  JSON events (ready, vad, speech_start, partial, end_of_turn, user_final, noise, orchestrator
                   events, audio_clear, voice_metrics, state, call_ended, error)  |  binary PCM16LE agent audio
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import Settings, get_settings
from app.core.orchestrator import Orchestrator
from app.voice.cache import FileAudioCache
from app.voice.pipeline import VoiceOptions, VoiceSession
from app.voice.vad import make_vad

log = logging.getLogger(__name__)

VOICES = [
    {"voice": "Seoyeon", "engine": "neural", "label": "서연 (Neural)"},
    {"voice": "Jihye", "engine": "neural", "label": "지혜 (Neural)"},
    {"voice": "Seoyeon", "engine": "generative", "label": "서연 (Generative)"},
    {"voice": "Hyunduk", "engine": "generative", "label": "현덕 (Generative)"},
]


@dataclass
class VoiceDeps:
    """STT / TTS / cache / VAD factory. Built lazily so a missing AWS setup shows up as a WS error, not a crash."""
    stt: Any = None
    tts: Any = None
    cache: FileAudioCache | None = None
    vad_factory: Any = None

    @classmethod
    def from_settings(cls, cfg: Settings) -> VoiceDeps:
        if cfg.voice_mode == "mock":
            from app.voice.fakes import ScriptedSTT, SilentTTS
            return cls(stt=ScriptedSTT(), tts=SilentTTS(), cache=FileAudioCache(), vad_factory=lambda: make_vad(cfg))
        from app.voice.aws import PollyTTS, TranscribeSTT
        return cls(stt=TranscribeSTT(cfg.aws_region, cfg.transcribe_vocabulary),
                   tts=PollyTTS(cfg.aws_region, cfg.polly_voice, cfg.polly_engine),
                   cache=FileAudioCache(), vad_factory=lambda: make_vad(cfg))


def build_router(orch: Orchestrator, deps: VoiceDeps | None = None) -> APIRouter:
    r = APIRouter()
    cfg = get_settings()
    holder: dict[str, VoiceDeps | None] = {"deps": deps}

    def get_deps() -> VoiceDeps:
        if holder["deps"] is None:
            holder["deps"] = VoiceDeps.from_settings(cfg)
        return holder["deps"]

    @r.get("/api/voice/config")
    async def voice_config() -> dict[str, Any]:
        return {"mode": cfg.voice_mode, "region": cfg.aws_region, "sample_rate": cfg.voice_sample_rate,
                "engines": list(orch.engines), "voices": VOICES,
                "default_voice": {"voice": cfg.polly_voice, "engine": cfg.polly_engine},
                "vad": {"silence_ms": cfg.vad_silence_ms, "short_silence_ms": cfg.vad_short_silence_ms,
                        "min_speech_ms": cfg.vad_min_speech_ms, "barge_in_ms": cfg.vad_barge_in_ms,
                        "threshold": cfg.vad_threshold},
                "filler_after_ms": cfg.filler_after_ms}

    @r.websocket("/voice/ws")
    async def voice_ws(sock: WebSocket):
        await sock.accept()
        out: asyncio.Queue[dict[str, Any] | bytes | None] = asyncio.Queue()

        async def sender() -> None:
            while (item := await out.get()) is not None:
                if isinstance(item, bytes | bytearray):
                    await sock.send_bytes(bytes(item))
                else:
                    await sock.send_text(json.dumps(item, ensure_ascii=False, default=str))

        send_task = asyncio.create_task(sender())
        sess: VoiceSession | None = None
        try:
            while True:
                m = await sock.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    if sess is not None:
                        await sess.feed(m["bytes"])
                    continue
                msg = json.loads(m.get("text") or "{}")
                kind = msg.get("type")
                if kind == "start":
                    if sess is not None:
                        await sess.close()
                        sess = None
                    try:
                        d = get_deps()
                        opts = VoiceOptions(**{k: v for k, v in msg.items() if k != "type"})
                        if opts.engine not in orch.engines:
                            raise ValueError(f"unknown engine {opts.engine!r}")
                        sess = VoiceSession(orch, d.stt, d.tts, d.cache, d.vad_factory(), out.put_nowait, opts, cfg)
                        await sess.start()
                    except Exception as e:  # noqa: BLE001
                        log.exception("voice session start failed")
                        out.put_nowait({"type": "error", "where": "start", "message": str(e)[:300]})
                elif kind == "text" and sess is not None:
                    await sess.say_text(msg.get("text", ""))
                elif kind == "stop" and sess is not None:
                    await sess.close()
                    sess = None
                    out.put_nowait({"type": "stopped"})
        except WebSocketDisconnect:
            pass
        finally:
            if sess is not None:
                await sess.close()
            out.put_nowait(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(send_task, timeout=2)

    return r
