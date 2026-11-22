"""cascade engine (D1): Transcribe Streaming -> Bedrock ConverseStream -> Polly.

Replaces Nova Sonic for languages it does not speak (ko-KR, id-ID). Same surface as
``nova.NovaSonicSession`` (open / close / events / send_audio / send_tool_result) and
the same Nova-style events, so server.py stays a pure A2A state machine.

Per call
* open       -> Transcribe stream (one per call) + fixed greeting through Polly.
* caller     -> Transcribe partial / final. End of turn = final result followed by
                ``turn_end_ms`` with no new partial.
* answer     -> ConverseStream tokens are cut into sentences; each sentence goes to
                Polly (pcm 16 kHz) and out as soon as it is ready, inside ONE audio
                block per turn (contentStart ... contentEnd END_TURN).
* barge-in   -> caller words (partial >= ``barge_in_chars``) or VAD speech while the
                answer is playing or being generated: {"interrupted": true} (server
                sends INTERRUPTION), the LLM/TTS task is cancelled, the open audio
                block ends INTERRUPTED.
* end_call   -> the model's tool call is forwarded as toolUse after the goodbye audio;
                server.py runs LocalTools (ESCALATE files a TMS callback request)
                and sends FINISH once the goodbye has played.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import re
import threading
import time
from typing import AsyncIterator, Optional

from .base import EnergyVad, EventEmitter, PollyTts
from .transcribe_ws import TranscribeStream

log = logging.getLogger("bridge.cascade")

_ids = itertools.count(1)

PHRASES = {
    "ko-KR": {
        "greeting": "안녕하세요, 에이투에이 음성 상담 에이전트입니다. 무엇을 도와드릴까요?",
        "goodbye": "통화를 마치겠습니다. 감사합니다.",
        "escalate": "담당자가 다시 연락드리겠습니다. 감사합니다.",
        "reprompt": "여보세요, 제 말 들리세요?",
        "connected": "(전화가 연결되었습니다)",
    },
    "id-ID": {
        "greeting": "Halo, ini agen suara A2A. Ada yang bisa saya bantu?",
        "goodbye": "Terima kasih, sampai jumpa.",
        "escalate": "Petugas kami akan menghubungi Anda kembali. Terima kasih.",
        "reprompt": "Halo, apakah Anda masih di sana?",
        "connected": "(panggilan tersambung)",
    },
    "en-US": {
        "greeting": "Hello, this is the A2A voice agent. How can I help you?",
        "goodbye": "Thank you for calling. Goodbye.",
        "escalate": "Someone from our team will call you back. Thank you.",
        "reprompt": "Hello, are you still there?",
        "connected": "(call connected)",
    },
}

# Sentence ends: . ? ! ~ and their full-width forms, newline. Korean also ends
# sentences with "요" / "다" followed by a space, but punctuation is reliable enough.
_SENT_END = re.compile(r"([.?!~。？！…]+[\"')\]]*)(\s+|$)|\n+")
_STRIP = re.compile(r"[*#_`>|]|^\s*[-•]\s*", re.M)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def clean_for_tts(text: str) -> str:
    return _EMOJI.sub("", _STRIP.sub("", text)).strip()


class ThinkingFilter:
    """Drops <thinking>...</thinking> spans from a token stream (Nova emits them with tools)."""

    OPEN, CLOSE = "<thinking>", "</thinking>"

    def __init__(self) -> None:
        self.inside = False
        self.pending = ""

    def feed(self, s: str) -> str:
        self.pending += s
        out = ""
        while self.pending:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.pending.find(tag)
            if i >= 0:
                if not self.inside:
                    out += self.pending[:i]
                self.pending = self.pending[i + len(tag):]
                self.inside = not self.inside
                continue
            # keep a possible partial tag at the end
            keep = 0
            for k in range(min(len(tag) - 1, len(self.pending)), 0, -1):
                if tag.startswith(self.pending[-k:]):
                    keep = k
                    break
            if not self.inside:
                out += self.pending[: len(self.pending) - keep]
            self.pending = self.pending[len(self.pending) - keep:]
            break
        return out


class SentenceSplitter:
    """Cuts streamed text into speakable pieces. The FIRST piece may end at a comma
    ("네," / "좋은 질문이에요,") so the first audio starts as early as possible."""

    _COMMA = re.compile(r"[,，、](\s+)")

    def __init__(self, min_chars: int = 2, soft_max: int = 60, first_comma: bool = True) -> None:
        self.buf = ""
        self.min_chars = min_chars
        self.soft_max = soft_max
        self.first_comma = first_comma
        self.emitted = 0

    def feed(self, s: str) -> list[str]:
        self.buf += s
        out = []
        while True:
            m = _SENT_END.search(self.buf)
            if m and len(self.buf[: m.end()].strip()) >= self.min_chars:
                out.append(self.buf[: m.end()].strip())
                self.buf = self.buf[m.end():]
                self.emitted += 1
                continue
            if self.first_comma and self.emitted == 0:
                c = self._COMMA.search(self.buf)
                if c and len(self.buf[: c.start() + 1].strip()) >= self.min_chars:
                    out.append(self.buf[: c.start() + 1].strip())
                    self.buf = self.buf[c.end():]
                    self.emitted += 1
                    continue
            if len(self.buf) > self.soft_max:            # long clause: cut at the last comma/space
                cut = max(self.buf.rfind(", ", 0, self.soft_max), self.buf.rfind(" ", 0, self.soft_max))
                if cut > 10:
                    out.append(self.buf[: cut + 1].strip())
                    self.buf = self.buf[cut + 1:]
                    self.emitted += 1
                    continue
            break
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


class CascadeSession:
    def __init__(self, *, region: str, voice_id: str, output_sample_rate: int, input_sample_rate: int,
                 language: str = "ko-KR", tts_engine: str = "neural", llm_model_id: str,
                 llm_region: Optional[str] = None, system_prompt: str = "", tool_specs: Optional[list] = None,
                 greeting: Optional[str] = None, turn_end_ms: int = 150, barge_in_chars: int = 2,
                 barge_in_vad_ms: int = 400, vad_threshold: float = 600.0, max_tokens: int = 300,
                 temperature: float = 0.0, top_k: int = 1, idle_prompt_s: float = 25.0,
                 idle_finish_s: float = 50.0,
                 **_ignored) -> None:
        import boto3
        self.region = region
        self.out_rate = output_sample_rate
        self.in_rate = input_sample_rate
        self.lang = language
        self.phr = PHRASES.get(language, PHRASES["en-US"])
        self.model_id = llm_model_id
        self.bedrock = boto3.client("bedrock-runtime", region_name=llm_region or region)
        self.tts = PollyTts(region, voice_id, engine=tts_engine, sample_rate=output_sample_rate)
        self.system = [{"text": system_prompt}] if system_prompt else []
        self.tool_config = self._tool_config(tool_specs or [])
        self.greeting = greeting or self.phr["greeting"]
        self.turn_end_s = turn_end_ms / 1000
        self.barge_in_chars = barge_in_chars
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Nova models call tools reliably only with greedy decoding (temperature 0, topK 1):
        # at 0.3 the end_call ESCALATE was skipped in a multi-turn call (v3 test 2026-11-19).
        self.extra_fields = ({"inferenceConfig": {"topK": top_k}}
                             if top_k and "nova" in llm_model_id else None)
        self.idle_prompt_s = idle_prompt_s
        self.idle_finish_s = idle_finish_s

        self.em = EventEmitter()
        self.stt = TranscribeStream(region=region, language=language, sample_rate=input_sample_rate,
                                    on_partial=self._on_partial, on_final=self._on_final)
        self.vad = EnergyVad(input_sample_rate, threshold=vad_threshold, start_ms=barge_in_vad_ms, end_ms=600)
        self.history: list[dict] = []
        self.user_buf: list[str] = []
        self.partial = ""
        self._eou: Optional[asyncio.TimerHandle] = None
        self._resp: Optional[asyncio.Task] = None
        self._audio_cid: Optional[str] = None      # open assistant audio block
        self._playing_until = 0.0
        self._finishing = False
        self._closed = False
        self._tool_waiters: dict[str, asyncio.Future] = {}
        self._last_activity = time.monotonic()
        self._reprompted = False
        self._watchdog: Optional[asyncio.Task] = None
        self._speech_end_at = 0.0
        self._marks: Optional[dict] = None
        self.turns = 0
        log.info("cascade engine lang=%s in=%dHz out=%dHz llm=%s tts=%s/%s turn_end=%dms",
                 language, input_sample_rate, output_sample_rate, llm_model_id, voice_id, tts_engine, turn_end_ms)

    @staticmethod
    def _tool_config(specs: list) -> Optional[dict]:
        tools = []
        for s in specs:
            ts = dict(s["toolSpec"])
            schema = ts.get("inputSchema", {}).get("json")
            if isinstance(schema, str):
                schema = json.loads(schema)
            tools.append({"toolSpec": {"name": ts["name"], "description": ts.get("description", ""),
                                       "inputSchema": {"json": schema}}})
        return {"tools": tools} if tools else None

    # --- lifecycle -----------------------------------------------------------
    async def open(self, prime_greeting: bool = True) -> None:
        self.stt.start()
        self.history = [{"role": "user", "content": [{"text": self.phr["connected"]}]},
                        {"role": "assistant", "content": [{"text": self.greeting}]}]
        if prime_greeting:
            await self._say_fixed(self.greeting)
        self._watchdog = asyncio.create_task(self._idle_watchdog())

    async def close(self) -> None:
        self._closed = True
        for t in (self._watchdog, self._resp):
            if t:
                t.cancel()
        await self.stt.close()
        await self.em.close()

    def events(self) -> AsyncIterator[dict]:
        return self.em.events()

    async def send_tool_result(self, tool_use_id: str, result: dict, is_error: bool = False) -> None:
        fut = self._tool_waiters.pop(tool_use_id, None)
        if fut and not fut.done():
            fut.set_result((result, is_error))

    # --- caller audio ----------------------------------------------------------
    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        self.stt.feed(pcm)
        edge = self.vad.feed(pcm)
        if edge == "start":
            self._last_activity = time.monotonic()
            if self._agent_busy():
                log.info("barge-in (vad)")
                await self._barge_in()
        elif edge == "end":
            self._speech_end_at = time.monotonic()

    def _agent_busy(self) -> bool:
        return (time.monotonic() < self._playing_until or self._audio_cid is not None
                or (self._resp is not None and not self._resp.done()))

    async def _on_partial(self, text: str) -> None:
        self._last_activity = time.monotonic()
        self.partial = text
        if self._eou:
            self._eou.cancel()
            self._eou = None
        if self._agent_busy() and len(text.replace(" ", "")) >= self.barge_in_chars and not self._finishing:
            log.info("barge-in (partial %dch)", len(text))
            await self._barge_in()

    async def _on_final(self, text: str) -> None:
        self._last_activity = time.monotonic()
        self.partial = ""
        if self._finishing:
            return
        if self._agent_busy() and len(text.replace(" ", "")) >= self.barge_in_chars:
            await self._barge_in()
        self.user_buf.append(text.strip())
        lag = (time.monotonic() - self._speech_end_at) * 1000 if self._speech_end_at else -1
        self._speech_end_at = 0.0
        log.info("stt final %dch (vad speech end -> final %.0f ms)", len(text), lag)
        if self._eou:
            self._eou.cancel()
        self._eou = asyncio.get_running_loop().call_later(self.turn_end_s, self._end_of_utterance)

    def _end_of_utterance(self) -> None:
        self._eou = None
        if self._finishing or not self.user_buf or self.partial:
            return
        if self.vad.in_speech:
            # Transcribe closed a segment on a short pause but the caller is still
            # talking: wait instead of answering half a sentence.
            self._eou = asyncio.get_running_loop().call_later(0.15, self._end_of_utterance)
            return
        text = " ".join(self.user_buf)
        self.user_buf.clear()
        self._resp = asyncio.create_task(self._respond(text, time.monotonic()))

    async def _barge_in(self) -> None:
        if time.monotonic() < self._playing_until or self._audio_cid is not None:
            await self.em.interrupted()
        self._playing_until = 0.0
        if self._resp and not self._resp.done():
            self._resp.cancel()
            try:
                await self._resp
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Words given back by a cancelled answer: answer them later even if the
        # barge-in was only noise and no new final result arrives.
        if self.user_buf and self._eou is None and not self._finishing:
            self._eou = asyncio.get_running_loop().call_later(1.2, self._end_of_utterance)

    # --- output helpers --------------------------------------------------------
    async def _open_audio(self) -> None:
        if self._audio_cid is None:
            self._audio_cid = f"a{next(_ids)}"
            await self.em.put({"contentStart": {"contentId": self._audio_cid, "type": "AUDIO", "role": "ASSISTANT"}})

    async def _emit_audio(self, pcm: bytes) -> None:
        await self._open_audio()
        if self._marks is not None:
            self._marks.setdefault("first_audio", time.monotonic())
        step = self.out_rate // 10 * 2                           # 100 ms
        for i in range(0, len(pcm), step):
            await self.em.put({"audioOutput": {"contentId": self._audio_cid,
                                               "content": base64.b64encode(pcm[i:i + step]).decode()}})
        self._playing_until = max(self._playing_until, time.monotonic()) + len(pcm) / 2 / self.out_rate

    async def _close_audio(self, stop: str) -> None:
        if self._audio_cid is not None:
            cid, self._audio_cid = self._audio_cid, None
            await self.em.put({"contentEnd": {"contentId": cid, "type": "AUDIO", "role": "ASSISTANT",
                                              "stopReason": stop}})

    async def _speak(self, sentence: str) -> None:
        text = clean_for_tts(sentence)
        if not text:
            return
        await self.em.text("ASSISTANT", "SPECULATIVE", text)
        # Stream Polly's audio as it arrives instead of waiting for the whole sentence.
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        step = self.out_rate // 10 * 2

        def pull():
            try:
                body = self.tts.client.synthesize_speech(**self.tts.request(text, self.out_rate))["AudioStream"]
                carry = b""
                while True:
                    data = body.read(step)
                    if not data:
                        break
                    data = carry + data
                    cut = len(data) - len(data) % 2
                    carry = data[cut:]
                    loop.call_soon_threadsafe(q.put_nowait, data[:cut])
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, e)
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=pull, daemon=True).start()
        while True:
            item = await q.get()
            if item is None:
                return
            if isinstance(item, Exception):
                log.warning("Polly failed: %s", item)
                return
            if item:
                await self._emit_audio(item)

    async def _say_fixed(self, text: str) -> None:
        await self._speak(text)
        await self._close_audio("END_TURN")

    # --- one answer ------------------------------------------------------------
    async def _respond(self, user_text: str, t_eou: float) -> None:
        self.turns += 1
        turn = self.turns
        # Merge into the last user message if the previous answer never got out.
        if self.history and self.history[-1]["role"] == "user" and _is_text(self.history[-1]):
            self.history[-1]["content"][0]["text"] += " " + user_text
        else:
            if self.history and self.history[-1]["role"] == "user":
                self.history.append({"role": "assistant", "content": [{"text": "."}]})
            self.history.append({"role": "user", "content": [{"text": user_text}]})
        await self.em.text("USER", "FINAL", user_text)
        spoken: list[str] = []
        marks = {"eou": t_eou}
        self._marks = marks
        usage = {"in": 0, "out": 0}
        end_call: Optional[dict] = None
        try:
            for _round in range(3):                              # tool loop (end_call ends it)
                text, tool_uses = await self._llm_round(spoken, marks, usage)
                assistant_content = []
                if text.strip():
                    assistant_content.append({"text": text.strip()})
                for tu in tool_uses:
                    assistant_content.append({"toolUse": tu})
                if not tool_uses:
                    if assistant_content:
                        self.history.append({"role": "assistant", "content": assistant_content})
                    break
                end = next((tu for tu in tool_uses if tu["name"] == "end_call"), None)
                if end is not None:
                    end_call = end
                    if not spoken:
                        outcome = (end["input"] or {}).get("outcome", "COMPLETE")
                        await self._speak(self.phr["escalate" if outcome == "ESCALATE" else "goodbye"])
                    self.history.append({"role": "assistant", "content": assistant_content or [{"text": "."}]})
                    break
                # Other tools: run through server.py, feed results back, ask again.
                results = []
                for tu in tool_uses:
                    res, err = await self._call_tool(tu)
                    results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": res}],
                                                   **({"status": "error"} if err else {})}})
                self.history.append({"role": "assistant", "content": assistant_content})
                self.history.append({"role": "user", "content": results})
            if end_call is not None:
                self._finishing = True
                await self._call_tool(end_call, wait=False)
            await self.em.put({"usageEvent": {"totalInputTokens": usage["in"], "totalOutputTokens": usage["out"]}})
            await self._close_audio("END_TURN")
        except asyncio.CancelledError:
            # Barge-in: keep what was actually said; if nothing was, give the words back.
            if self.history and self.history[-1]["role"] == "user" and not _is_text(self.history[-1]):
                self.history.append({"role": "assistant", "content": [{"text": " ".join(spoken) or "."}]})
            elif spoken:
                self.history.append({"role": "assistant", "content": [{"text": " ".join(spoken) + " …"}]})
            elif self.history and self.history[-1]["role"] == "user":
                last = self.history.pop()
                self.user_buf.insert(0, last["content"][0]["text"])
            await self._close_audio("INTERRUPTED")
            log.info("turn %d cancelled by barge-in (%d sentences out)", turn, len(spoken))
            raise
        except Exception:  # noqa: BLE001
            log.exception("turn %d failed", turn)
            if self.history and self.history[-1]["role"] == "user":
                self.history.append({"role": "assistant", "content": [{"text": "."}]})
            await self._speak("죄송합니다, 잠시 문제가 생겼어요. 다시 말씀해 주시겠어요?" if self.lang == "ko-KR"
                              else "Sorry, something went wrong. Could you say that again?")
            await self._close_audio("END_TURN")
        finally:
            eou = marks["eou"]
            log.info("latency turn=%d llm_first=%s first_audio=%s total=%.0fms sentences=%d end_call=%s",
                     turn, _ms(marks.get("llm_first"), eou), _ms(marks.get("first_audio"), eou),
                     (time.monotonic() - eou) * 1000, len(spoken), (end_call or {}).get("input"))

    async def _llm_round(self, spoken: list, marks: dict, usage: dict):
        """One ConverseStream call. Sentences are spoken as they complete."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        kw = dict(modelId=self.model_id, messages=self.history,
                  inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature})
        if self.system:
            kw["system"] = self.system
        if self.tool_config:
            kw["toolConfig"] = self.tool_config
        if self.extra_fields:
            kw["additionalModelRequestFields"] = self.extra_fields

        def worker():
            try:
                r = self.bedrock.converse_stream(**kw)
                stream = r["stream"]
                for ev in stream:
                    if stop.is_set():
                        with _quiet():
                            stream.close()
                        break
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, e)
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        tts_q: asyncio.Queue = asyncio.Queue()

        async def tts_worker():
            while True:
                s = await tts_q.get()
                if s is None:
                    return
                await self._speak(s)
                spoken.append(s)

        tts_task = asyncio.create_task(tts_worker())
        thinking = ThinkingFilter()
        splitter = SentenceSplitter()
        text_all = ""
        tools: dict[int, dict] = {}
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    break
                if isinstance(ev, Exception):
                    raise ev
                if "contentBlockStart" in ev:
                    st = ev["contentBlockStart"].get("start", {}).get("toolUse")
                    if st:
                        tools[ev["contentBlockStart"]["contentBlockIndex"]] = {
                            "toolUseId": st["toolUseId"], "name": st["name"], "raw": ""}
                elif "contentBlockDelta" in ev:
                    d = ev["contentBlockDelta"]["delta"]
                    if "text" in d:
                        marks.setdefault("llm_first", time.monotonic())
                        visible = thinking.feed(d["text"])
                        text_all += visible
                        for s in splitter.feed(visible):
                            tts_q.put_nowait(s)
                    elif "toolUse" in d:
                        idx = ev["contentBlockDelta"]["contentBlockIndex"]
                        tools.setdefault(idx, {"toolUseId": f"t{idx}", "name": "?", "raw": ""})
                        tools[idx]["raw"] += d["toolUse"].get("input", "")
                elif "metadata" in ev:
                    u = ev["metadata"].get("usage") or {}
                    usage["in"] += int(u.get("inputTokens", 0))
                    usage["out"] += int(u.get("outputTokens", 0))
            for s in splitter.flush():
                tts_q.put_nowait(s)
            tts_q.put_nowait(None)
            await tts_task
        except BaseException:
            stop.set()
            tts_task.cancel()
            raise
        tool_uses = []
        for idx in sorted(tools):
            t = tools[idx]
            try:
                args = json.loads(t["raw"]) if t["raw"].strip() else {}
            except ValueError:
                args = {}
            tool_uses.append({"toolUseId": t["toolUseId"], "name": t["name"], "input": args})
        return text_all, tool_uses

    async def _call_tool(self, tu: dict, wait: bool = True):
        fut = asyncio.get_running_loop().create_future()
        self._tool_waiters[tu["toolUseId"]] = fut
        await self.em.put({"toolUse": {"toolName": tu["name"], "toolUseId": tu["toolUseId"],
                                       "content": json.dumps(tu["input"], ensure_ascii=False)}})
        if not wait:
            return None, False
        try:
            return await asyncio.wait_for(fut, 10)
        except asyncio.TimeoutError:
            return {"error": "tool timeout"}, True

    # --- idle handling -----------------------------------------------------------
    async def _idle_watchdog(self) -> None:
        try:
            while not self._finishing and not self._closed:
                await asyncio.sleep(1.0)
                if self._agent_busy() or self.vad.in_speech or self.partial or self.user_buf:
                    continue
                idle = time.monotonic() - max(self._last_activity, self._playing_until)
                if idle >= self.idle_finish_s:
                    log.info("no caller speech for %.0fs; finishing", idle)
                    self._finishing = True
                    await self._speak(self.phr["goodbye"])
                    await self._call_tool({"toolUseId": f"idle-{next(_ids)}", "name": "end_call",
                                           "input": {"outcome": "COMPLETE", "summary": "caller silent"}},
                                          wait=False)
                    await self._close_audio("END_TURN")
                    return
                if idle >= self.idle_prompt_s and not self._reprompted:
                    self._reprompted = True
                    await self._say_fixed(self.phr["reprompt"])
        except asyncio.CancelledError:
            pass


def _is_text(msg: dict) -> bool:
    c = msg.get("content") or []
    return bool(c) and "text" in c[0]


def _ms(t: Optional[float], t0: float) -> str:
    return f"{(t - t0) * 1000:.0f}ms" if t else "-"


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
