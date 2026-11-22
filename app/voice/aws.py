"""AWS implementations: Amazon Transcribe streaming (STT) and Amazon Polly (TTS). Credentials: default boto chain."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator

from app.voice.stt import AudioFormat, Transcript

log = logging.getLogger(__name__)


class TranscribeSTT:
    """One Transcribe stream per driver utterance (opened at VAD speech_start, so connect time overlaps the speech).
    Billing is per streamed second, so silence between turns is never sent."""

    def __init__(self, region: str, vocabulary_name: str = ""):
        from amazon_transcribe.client import TranscribeStreamingClient

        self.client = TranscribeStreamingClient(region=region)
        self.vocabulary_name = vocabulary_name or None

    async def stream(self, audio: AsyncIterator[bytes], fmt: AudioFormat, *, language: str = "ko-KR",
                     phrase_hints: list[str] | None = None) -> AsyncIterator[Transcript]:
        kw = {"vocabulary_name": self.vocabulary_name} if self.vocabulary_name else {}
        st = await self.client.start_stream_transcription(language_code=language, media_sample_rate_hz=fmt.sample_rate_hz,
                                                          media_encoding="pcm", **kw)

        async def pump() -> None:
            try:
                async for chunk in audio:
                    await st.input_stream.send_audio_event(audio_chunk=chunk)
            finally:
                await st.input_stream.end_stream()

        pump_task = asyncio.create_task(pump())
        try:
            async for ev in st.output_stream:
                for r in getattr(getattr(ev, "transcript", None), "results", None) or []:
                    if r.alternatives:
                        yield Transcript(text=r.alternatives[0].transcript, is_final=not r.is_partial,
                                         start_ms=(r.start_time or 0) * 1000, end_ms=(r.end_time or 0) * 1000)
            await pump_task
        finally:
            if not pump_task.done():
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump_task


class PollyTTS:
    """Polly synthesize_speech (pcm, 16-bit mono LE). The HTTP body is read in small chunks on a worker thread and
    handed over as it arrives, so the first audio leaves before the whole sentence is synthesised."""

    CHUNK = 3200  # 100 ms at 16 kHz

    def __init__(self, region: str, voice: str = "Seoyeon", engine: str = "neural"):
        import boto3

        self.client = boto3.client("polly", region_name=region)
        self.voice, self.engine = voice, engine

    async def stream(self, text: str, fmt: AudioFormat, *, voice: str | None = None, engine: str | None = None,
                     speaking_rate: float = 1.0) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        stop = threading.Event()
        kw = {"Text": text, "TextType": "text"}
        if speaking_rate != 1.0:
            kw = {"Text": f'<speak><prosody rate="{int(speaking_rate * 100)}%">{_xml(text)}</prosody></speak>',
                  "TextType": "ssml"}

        def work() -> None:
            try:
                r = self.client.synthesize_speech(VoiceId=voice or self.voice, Engine=engine or self.engine,
                                                  OutputFormat="pcm", SampleRate=str(fmt.sample_rate_hz), **kw)
                body = r["AudioStream"]
                while not stop.is_set():
                    b = body.read(self.CHUNK)
                    if not b:
                        break
                    loop.call_soon_threadsafe(q.put_nowait, b)
                body.close()
                loop.call_soon_threadsafe(q.put_nowait, None)
            except BaseException as e:  # noqa: BLE001 — re-raised on the event loop side
                loop.call_soon_threadsafe(q.put_nowait, e)

        fut = loop.run_in_executor(None, work)
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await asyncio.shield(fut)


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
