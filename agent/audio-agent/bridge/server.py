"""The bridge server: one aiohttp process, one port, /a2a WebSocket + /health.

Lifecycle (README "Connection lifecycle"):
  Upgrade -> validate bearer (401 on mismatch) -> AwaitInit -> INIT_SESSION_RESPONSE
  -> AwaitReady (CHANNEL_STATE READY opens the Nova session, primes the greeting)
  -> Live (audio <-> Nova, TRACING_SPAN + statusUpdate per turn, no finish)
  -> Finishing (end_call: goodbye, trace, FINISH) -> close.

Kept deliberately small; the audio/tracing/tool/nova mechanics live in their
own modules so this file reads as the protocol state machine.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from typing import Optional

from aiohttp import web, WSMsgType

from a2a_connect.fragments import FragmentReassembler

from . import a2a, audio, testpage, tracing
from .bearer import bearer_matches
from .config import CONFIG
from .secrets import SecretCache
from .tools import Tools, TOOL_SPECS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bridge.server")


class Session:
    """One caller's A2A session on the persistent voice WebSocket."""

    def __init__(self, ws: web.WebSocketResponse, app_state: "AppState", traceparent: Optional[str],
                 tracestate: Optional[str] = None):
        self.ws = ws
        self.state = app_state
        self.faults: frozenset[str] = getattr(app_state, "faults", frozenset())
        self.context_id: Optional[str] = None
        self.contact_arn: Optional[str] = None
        self.supported_finish_types: list[str] = ["COMPLETE", "ESCALATE"]
        self.input_sample_rate = 8000       # Connect proposes; overwritten by INIT_SESSION
        self.nova: Optional[NovaSonicSession] = None
        self._content_kinds: dict = {}
        self._nova_blocks: dict = {}     # contentId -> audio events seen, for the event log
        self._nova_last_audio: Optional[str] = None
        self._text_stage: dict = {}      # TEXT contentId -> generationStage (SPECULATIVE or FINAL)
        self._audio_open = False         # an assistant audio block is streaming
        self._awaiting_continuation = False  # the turn's last audio block ended PARTIAL_TURN
        self.tools: Optional[Tools] = None
        self.frag = FragmentReassembler()
        # Trace propagation: Connect sends traceparent and tracestate once, on the
        # upgrade, so every turn of the call uses the same trace id and parent span.
        # The root's parent is the tracestate UUID, copied unchanged, as the kit does.
        self.trace_id, self.parent_span_id = tracing.parse_traceparent(traceparent)
        ts_parent = tracing.parse_tracestate_parent(tracestate)
        if ts_parent:
            self.parent_span_id = ts_parent
        if self.trace_id is None:
            self.trace_id = tracing._hex(16)
        self.turn_index = 0
        # Turn state.
        self.current_task_id: Optional[str] = None
        self.current_artifact_id: Optional[str] = None
        self.turn_trace: Optional[tracing.TurnTrace] = None
        self.rechunker: Optional[audio.ReChunker] = None
        self.first_frame_sent = False
        self.audio_seconds_sent = 0.0
        self.turn_first_frame_wall = 0.0
        self.caller_text = ""
        self.agent_text = ""
        self.in_tokens = 0
        self.out_tokens = 0
        self.last_audio_out = time.monotonic()
        self.last_speech_out = 0.0       # last Nova audio frame (not keepalive silence)
        self.playback_end = 0.0          # wall time Connect finishes playing what we sent
        self._finish_task: Optional[asyncio.Task] = None
        self._closing = False
        self._pending_tool_ids: dict[str, str] = {}
        self._early_frames = 0
        self._audio_in_frames = 0

    # --- inbound from Connect ----------------------------------------------
    async def handle_frame(self, frame: dict) -> None:
        kind = a2a.message_kind(frame)
        if kind is None:
            await self._on_parts(frame)
            return
        log.info("inbound %s %s", kind, a2a.summarize(frame, 2000 if kind == "ERROR" else 400))
        if kind == "INIT_SESSION":
            await self._on_init(frame)
        elif kind == "CHANNEL_STATE":
            await self._on_channel_state(frame)
        elif kind == "FRAGMENT":
            await self._on_fragment(frame)
        elif kind == "INTERRUPTION":
            pass  # Nova does its own barge-in detection on the audio.
        # ERROR / DTMF_INPUT_COMPLETE: logged above, not acted on.

    async def _on_init(self, frame: dict) -> None:
        msg = a2a.message_of(frame)
        init = a2a.data_of(frame, "initSession") or {}
        self.context_id = msg.get("contextId") or a2a.new_id()
        self.contact_arn = init.get("contactArn")
        self.supported_finish_types = init.get("supportedFinishTypes") or self.supported_finish_types
        in_cfg = init.get("audioInputConfiguration") or {}
        self.input_sample_rate = int(in_cfg.get("sampleRateHertz") or self.input_sample_rate)
        if not self.trace_id:
            self.trace_id = tracing._hex(16)
        # bad-audio-output-config fault: counter-propose an unsupported output rate (ART-04).
        out_rate = 44100 if "bad-audio-output-config" in self.faults else CONFIG.output_sample_rate
        await self.ws.send_str(a2a.init_session_response(
            frame.get("id"), self.context_id, out_rate))
        log.info("INIT_SESSION ctx=%s in=%dHz finish=%s tracing=%s", self.context_id,
                 self.input_sample_rate, self.supported_finish_types,
                 init.get("subscribeToTracingEvents"))

    async def _on_channel_state(self, frame: dict) -> None:
        state = (a2a.data_of(frame, "channelState") or {}).get("state")
        if state != "READY":
            log.info("CHANNEL_STATE %s (ignored)", state)
            return
        if self.nova is None:
            await self._start_nova()

    async def _on_fragment(self, frame: dict) -> None:
        f = a2a.data_of(frame, "fragment") or {}
        if not f.get("fragmentId"):
            return
        # The kit's reassembler buffers by fragmentId, orders by partNumber and
        # base64-decodes into the original JSON frame (Samples A.9).
        completed = self.frag.add(f)
        if completed is not None:
            await self.handle_frame(completed)

    async def _start_nova(self) -> None:
        persona = self.state.persona.format(
            first_name=CONFIG.demo_first_name, last_name=CONFIG.demo_last_name,
            customer_id=CONFIG.demo_customer_id,
            today=_today())
        if not getattr(self.state, "stub_nova", False) and CONFIG.engine == "loopback":
            # a2a-voice: no-AI engine for verifying the Connect <-> ECS audio path.
            from .engines.loopback import LoopbackSession
            from .local_tools import LocalTools
            self.tools = LocalTools(region=CONFIG.region, instance_id=CONFIG.connect_instance_id,
                                    contact_arn=self.contact_arn, tms_url=CONFIG.tms_url)
            self.nova = LoopbackSession(
                region=CONFIG.region, voice_id=CONFIG.tts_voice, tts_engine=CONFIG.tts_engine,
                output_sample_rate=CONFIG.output_sample_rate, input_sample_rate=self.input_sample_rate,
                language=CONFIG.language, max_turns=CONFIG.loopback_max_turns,
                vad_threshold=CONFIG.vad_threshold, end_ms=CONFIG.vad_end_ms)
        elif not getattr(self.state, "stub_nova", False) and CONFIG.engine == "cascade":
            # a2a-voice P6: Transcribe -> Bedrock -> Polly (Nova Sonic bypass for ko-KR / id-ID).
            from .engines.cascade import CascadeSession
            from .local_tools import END_CALL_SPEC, LocalTools
            self.tools = LocalTools(region=CONFIG.region, instance_id=CONFIG.connect_instance_id,
                                    contact_arn=self.contact_arn, tms_url=CONFIG.tms_url)
            with open(CONFIG.persona_cascade_path, encoding="utf-8") as f:
                prompt = f.read().replace("{today}", _today())
            self.nova = CascadeSession(
                region=CONFIG.region, voice_id=CONFIG.tts_voice, tts_engine=CONFIG.tts_engine,
                output_sample_rate=CONFIG.output_sample_rate, input_sample_rate=self.input_sample_rate,
                language=CONFIG.language, llm_model_id=CONFIG.llm_model_id,
                llm_region=CONFIG.llm_region or None, system_prompt=prompt, tool_specs=[END_CALL_SPEC],
                greeting=CONFIG.greeting or None, turn_end_ms=CONFIG.turn_end_ms,
                barge_in_chars=CONFIG.barge_in_chars, barge_in_vad_ms=CONFIG.barge_in_vad_ms,
                vad_threshold=CONFIG.vad_threshold, max_tokens=CONFIG.llm_max_tokens,
                temperature=CONFIG.llm_temperature, top_k=CONFIG.llm_top_k)
        elif getattr(self.state, "stub_nova", False):
            # Offline stub mode for the Tester's audio scenario: a scripted Nova
            # session and canned tools, no Secrets Manager and no HTTP.
            from .stubnova import StubNovaSession, StubTools
            self.tools = StubTools()
            self.nova = StubNovaSession(
                region=CONFIG.nova_region, model_id=CONFIG.model_id, voice_id=CONFIG.voice_id,
                output_sample_rate=CONFIG.output_sample_rate, input_sample_rate=self.input_sample_rate,
                system_prompt=persona, tool_specs=TOOL_SPECS, faults=self.faults)
        else:
            api_key = self.state.secrets.get(CONFIG.insurance_api_key_secret_id, label="insurance key")
            self.tools = Tools(
                api_url=CONFIG.insurance_api_url, api_key=api_key,
                customer_id=CONFIG.demo_customer_id, region=CONFIG.region,
                instance_id=CONFIG.connect_instance_id, contact_arn=self.contact_arn)
            from .nova import NovaSonicSession  # imports the Bedrock CRT SDK; stub mode avoids it
            self.nova = NovaSonicSession(
                region=CONFIG.nova_region, model_id=CONFIG.model_id, voice_id=CONFIG.voice_id,
                output_sample_rate=CONFIG.output_sample_rate, input_sample_rate=self.input_sample_rate,
                system_prompt=persona, tool_specs=TOOL_SPECS)
        await self.nova.open(prime_greeting=True)
        asyncio.create_task(self._nova_loop())
        asyncio.create_task(self._keepalive_loop())
        log.info("Nova live for ctx=%s", self.context_id)

    async def _on_parts(self, frame: dict) -> None:
        if not self.nova:
            self._early_frames += 1
            if self._early_frames <= 3:
                log.info("frame before Nova is live: %s", a2a.summarize(frame, 300))
            return
        for part in a2a.iter_parts(frame):
            pcm = a2a.audio_of(part)
            if pcm:
                self._audio_in_frames += 1
                if self._audio_in_frames == 1:
                    log.info("first caller audio frame: %d bytes", len(pcm))
                await self.nova.send_audio(pcm)
            elif part.get("text"):
                log.info("text turn (ignored on voice): %r", part["text"][:120])

    # --- outbound driven by Nova -------------------------------------------
    async def _nova_loop(self) -> None:
        try:
            async for event in self.nova.events():
                await self._on_nova_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one send failure must not kill the loop
            if self._closing:
                log.info("Nova loop ended with the call")
            else:
                log.exception("Nova receive loop error")

    async def _begin_turn(self) -> None:
        self.turn_index += 1
        log.info("turn %d begins with %dch of assistant text", self.turn_index, len(self.agent_text))
        self.current_task_id = a2a.new_id()
        self.current_artifact_id = a2a.new_id()
        self.turn_trace = tracing.TurnTrace(self.trace_id, self.parent_span_id, CONFIG.model_id)
        self.rechunker = audio.ReChunker(audio.frame_bytes(CONFIG.output_sample_rate, CONFIG.frame_ms))
        self.first_frame_sent = False
        self.audio_seconds_sent = 0.0
        self.turn_first_frame_wall = 0.0
        self._awaiting_continuation = False
        # Neither text is reset here. Nova sends the caller's transcript (role USER) and
        # the assistant's SPECULATIVE text before the audio contentStart that opens the
        # turn; _finalize_turn clears both once the turn's trace has used them.

    def _log_nova_event(self, event: dict) -> None:
        """Log Nova's events in the order they arrive: one line per content block start,
        text output, tool call and content block end. Only kinds, roles, lengths and stop
        reasons are logged, never the words or the audio. The order decides which words
        reach each turn's trace (dry run 2026-10-30: most turns had no assistant text)."""
        turn = f"turn={self.turn_index}{'' if self.turn_trace is not None else '(closed)'}"
        if "contentStart" in event:
            cs = event["contentStart"]
            cid = cs.get("contentId")
            self._nova_blocks[cid] = 0
            if cs.get("type") == "AUDIO":
                self._nova_last_audio = cid
            stage = _generation_stage(cs.get("additionalModelFields"))
            log.info("nova start %s %s %s%s %s", _short(cid), cs.get("type"), cs.get("role"),
                     f" stage={stage}" if stage else "", turn)
        elif "audioOutput" in event:
            cid = event["audioOutput"].get("contentId") or self._nova_last_audio
            if cid in self._nova_blocks:
                self._nova_blocks[cid] += 1
        elif "textOutput" in event:
            to = event["textOutput"]
            log.info("nova text %s %s %dch %s", _short(to.get("contentId")), to.get("role"),
                     len(to.get("content") or ""), turn)
        elif "toolUse" in event:
            tu = event["toolUse"]
            log.info("nova toolUse %s %s", tu.get("toolName") or tu.get("name"), turn)
        elif "contentEnd" in event:
            ce = event["contentEnd"]
            cid = ce.get("contentId")
            audio_events = self._nova_blocks.pop(cid, 0)
            kind = self._content_kinds.get(cid, (None, None))
            log.info("nova end %s %s %s stop=%s audio_events=%d %s", _short(cid), ce.get("type") or kind[0],
                     ce.get("role") or kind[1], ce.get("stopReason") or "-", audio_events, turn)

    async def _on_nova_event(self, event: dict) -> None:
        self._log_nova_event(event)
        if "contentStart" in event:
            cs = event["contentStart"]
            # Nova's contentEnd omits role (and sometimes type); remember both by id.
            if cs.get("contentId"):
                self._content_kinds[cs["contentId"]] = (cs.get("type"), cs.get("role"))
            if cs.get("type") == "TEXT":
                self._text_stage[cs.get("contentId")] = _generation_stage(cs.get("additionalModelFields"))
            if cs.get("type") == "AUDIO" and cs.get("role") == "ASSISTANT":
                self._audio_open = True
                self._awaiting_continuation = False
                if self.current_task_id is None or self.turn_trace is None:
                    await self._begin_turn()
        elif "audioOutput" in event:
            await self._on_audio_output(event["audioOutput"])
        elif "textOutput" in event:
            await self._on_text_output(event["textOutput"])
        elif "toolUse" in event:
            await self._on_tool_use(event["toolUse"])
        elif "usageEvent" in event:
            self._on_usage(event["usageEvent"])
        elif "contentEnd" in event:
            await self._on_content_end(event["contentEnd"])

    async def _on_audio_output(self, ao: dict) -> None:
        if self.turn_trace is None:
            await self._begin_turn()
        pcm = _b64(ao.get("content", ""))
        self.last_speech_out = time.monotonic()
        for frame in self.rechunker.push(pcm):
            await self._send_audio_frame(frame, last=False)

    async def _send_audio_frame(self, frame: bytes, last: bool) -> None:
        append = self.first_frame_sent
        await self.ws.send_str(self._audio_chunk(
            self.current_artifact_id, frame, append=append, last=last))
        if not self.first_frame_sent:
            self.turn_first_frame_wall = time.monotonic()
        self.first_frame_sent = True
        dur = len(frame) / 2 / CONFIG.output_sample_rate
        self.audio_seconds_sent += dur
        self.playback_end = max(self.playback_end, time.monotonic()) + dur
        self.last_audio_out = time.monotonic()

    def _audio_chunk(self, artifact_id: str, pcm: bytes, *, append: bool, last: bool) -> str:
        """Build an AUDIO_RESPONSE_CHUNK frame, applying any active audio fault.

        audio-no-last-chunk never closes an artifact; audio-bad-media-type
        relabels the audio part. Both are test-only (server.main refuses faults
        off loopback)."""
        if "audio-no-last-chunk" in self.faults:
            last = False
        text = a2a.audio_chunk_message(
            self.context_id, self.current_task_id, artifact_id, pcm, append=append, last=last)
        if "audio-bad-media-type" in self.faults:
            import json as _json
            frame = _json.loads(text)
            for part in frame["result"]["artifactUpdate"]["artifact"]["parts"]:
                if "raw" in part:
                    part["mediaType"] = "text/plain"
            text = _json.dumps(frame)
        return text

    async def _on_text_output(self, to: dict) -> None:
        role = to.get("role")
        content = to.get("content", "")
        # Barge-in: an ASSISTANT text carrying {"interrupted": true}.
        if role == "ASSISTANT" and _is_interrupted(content):
            await self._handle_barge_in()
            return
        # Look the stage up without removing it: Nova can send a TEXT block's contentEnd
        # before its textOutput (d572fde0), and a block may carry more than one output.
        stage = self._text_stage.get(to.get("contentId"))
        if role == "USER":
            if stage == "SPECULATIVE":
                return
            # New caller words close a turn whose last audio block ended PARTIAL_TURN:
            # Nova is not continuing that answer.
            if self.turn_trace is not None and self._awaiting_continuation:
                await self._finalize_turn("PARTIAL_TURN")
            self.caller_text = _join(self.caller_text, content)
            testpage.add_line(self.contact_arn, "USER", content)
        elif role == "ASSISTANT":
            # Nova sends each answer's text twice: SPECULATIVE just before its audio block,
            # and FINAL seconds after the audio ends, when the turn's trace has gone out.
            # Keep the SPECULATIVE text for the turn the audio opens, and drop the late
            # FINAL copy so it cannot land in the next turn (dry run 2026-10-30, d572fde0).
            if stage == "FINAL":
                return
            self.agent_text = _join(self.agent_text, content)
            testpage.add_line(self.contact_arn, "ASSISTANT", content)

    async def _handle_barge_in(self) -> None:
        log.info("barge-in on ctx=%s", self.context_id)
        # Stop this artifact without lastChunk, tell Connect to flush, start fresh id.
        await self.ws.send_str(a2a.interruption_message(self.context_id, self.current_task_id))
        self.current_artifact_id = a2a.new_id()
        self.first_frame_sent = False
        if self.rechunker:
            self.rechunker.flush(pad=False)
        # An answer whose audio ended PARTIAL_TURN will not continue after a barge-in.
        if self.turn_trace is not None and self._awaiting_continuation and not self._audio_open:
            await self._finalize_turn("INTERRUPTED")

    async def _on_tool_use(self, tu: dict) -> None:
        name = tu.get("toolName") or tu.get("name")
        tool_use_id = tu.get("toolUseId") or tu.get("toolUseContentId") or a2a.new_id()
        raw_args = tu.get("content") or tu.get("input") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except ValueError:
            args = {}
        if self.turn_trace is None:
            await self._begin_turn()
        span = self.turn_trace.start_tool(name, tool_use_id, json.dumps(args))
        try:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, self.tools.run, name, args),
                timeout=CONFIG.tool_timeout_seconds)
            self.turn_trace.finish_tool(span, json.dumps(result), ok=True)
            await self.nova.send_tool_result(tool_use_id, result, is_error=False)
            if name == "end_call" and self._finish_task is None:
                self._finish_task = asyncio.create_task(self._finish_when_quiet())
        except Exception as e:  # noqa: BLE001
            log.warning("tool %s failed: %s", name, e)
            self.turn_trace.finish_tool(span, str(e), ok=False)
            await self.nova.send_tool_result(tool_use_id, {"error": str(e)}, is_error=True)

    def _on_usage(self, usage: dict) -> None:
        # Nova's usageEvent carries running session totals (totalInputTokens /
        # totalOutputTokens); details.* are nested per-modality dicts, not numbers.
        self.in_tokens = int(usage.get("totalInputTokens", self.in_tokens) or 0)
        self.out_tokens = int(usage.get("totalOutputTokens", self.out_tokens) or 0)

    async def _on_content_end(self, ce: dict) -> None:
        ctype, role = self._content_kinds.pop(ce.get("contentId"), (None, None))
        ctype = ce.get("type") or ctype
        role = ce.get("role") or role
        if ctype != "AUDIO" or role != "ASSISTANT":
            return
        self._audio_open = False
        stop = (ce.get("stopReason") or "").upper()
        if stop == "TOOL_USE":
            await self._close_artifact()
            return
        if stop == "PARTIAL_TURN":
            # Nova continues the same answer in another audio block, with its own
            # SPECULATIVE text (d572fde0). Keep the turn AND its audio artifact open: the
            # next block appends to the same artifact. Closing the artifact here and
            # starting another in the same task made Connect answer "Artifact conflict on
            # audio" and close the call with 1011 (v3 calls 9d73592e and 1b0fa6de). The
            # next END_TURN block, the caller's next words or a barge-in close the turn.
            self._awaiting_continuation = True
            return
        if stop not in ("END_TURN", "INTERRUPTED", ""):
            return
        await self._finalize_turn(stop)

    async def _finalize_turn(self, stop_reason: str) -> None:
        if self.turn_trace is None or self.current_task_id is None:
            return
        # Flush the last partial audio frame with lastChunk=true.
        if self.rechunker:
            tail = self.rechunker.flush(pad=True)
            if tail is not None:
                await self._send_audio_frame(tail, last=True)
            elif self.first_frame_sent:
                await self.ws.send_str(self._audio_chunk(
                    self.current_artifact_id, b"", append=True, last=True))
        pending = self.tools.pending if self.tools else None
        # Emit the turn trace.
        log.info("turn %d trace: caller %dch, assistant %dch, stop=%s", self.turn_index,
                 len(self.caller_text), len(self.agent_text), stop_reason)
        otlp = self.turn_trace.finish(
            caller_text=self.caller_text, agent_text=self.agent_text,
            input_tokens=self.in_tokens, output_tokens=self.out_tokens)
        await self.ws.send_str(a2a.tracing_span_message(
            self.context_id, self.current_task_id, a2a.new_id(), otlp))
        if pending is not None:
            await self._finish(pending)
        else:
            # Per-turn completion WITHOUT a finish keeps the call alive.
            await self.ws.send_str(a2a.status_update(
                self.context_id, self.current_task_id, "TASK_STATE_COMPLETED"))
        # Reset for the next turn.
        self.turn_trace = None
        self.current_task_id = None
        self.caller_text = ""
        self.agent_text = ""
        self._awaiting_continuation = False
        self.in_tokens = 0
        self.out_tokens = 0

    async def _close_artifact(self) -> None:
        """End the open audio artifact with lastChunk, keeping the task (turn) open."""
        if self.current_task_id is None or not self.first_frame_sent:
            return
        tail = self.rechunker.flush(pad=True) if self.rechunker else None
        if tail is not None:
            await self._send_audio_frame(tail, last=True)
        else:
            await self.ws.send_str(self._audio_chunk(
                self.current_artifact_id, b"", append=True, last=True))
        self.current_artifact_id = a2a.new_id()
        self.first_frame_sent = False

    async def _finish_when_quiet(self) -> None:
        """end_call ran: send FINISH once Sofía stops speaking (max ~8 s).

        Without this, a goodbye whose audio turn closed before end_call arrived
        left FINISH waiting for the next turn, i.e. the caller's second goodbye.
        """
        deadline = time.monotonic() + 8.0
        await asyncio.sleep(0.8)
        while not self._closing and time.monotonic() < deadline:
            if time.monotonic() - self.last_speech_out >= 1.0:
                break
            await asyncio.sleep(0.2)
        if self._closing or self.tools is None or self.tools.pending is None:
            return
        log.info("end_call: finishing without waiting for another turn")
        if self.current_task_id is not None and self.turn_trace is not None:
            await self._finalize_turn("END_TURN")
        else:
            await self._begin_turn()
            await self._finish(self.tools.pending)

    async def _finish(self, pending) -> None:
        if self._closing:
            return
        self._closing = True
        # Wait until Connect has played what we sent so the goodbye is not clipped.
        remaining = self.playback_end - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(remaining, CONFIG.goodbye_max_wait_seconds))
        finish_type = pending.outcome if pending.outcome in self.supported_finish_types else "COMPLETE"
        await self.ws.send_str(a2a.status_update(
            self.context_id, self.current_task_id, "TASK_STATE_COMPLETED",
            finish={"type": finish_type, "reason": pending.reason or pending.summary}))
        log.info("FINISH %s ctx=%s", finish_type, self.context_id)
        with contextlib.suppress(Exception):
            await self.ws.close()

    async def _keepalive_loop(self) -> None:
        """Send a 100 ms silence frame if no audio has gone out for a while (V3)."""
        frame = audio.silence_frame(audio.frame_bytes(CONFIG.output_sample_rate, CONFIG.frame_ms))
        try:
            while not self._closing:
                await asyncio.sleep(1.0)
                if self.current_task_id is None:
                    continue
                if time.monotonic() - self.last_audio_out >= CONFIG.silence_keepalive_seconds:
                    with contextlib.suppress(Exception):
                        if self.first_frame_sent:
                            # An artifact is open: Connect rejects starting another
                            # ("Artifact conflict on audio"), so pad the open one.
                            await self._send_audio_frame(frame, last=False)
                        else:
                            await self.ws.send_str(a2a.audio_chunk_message(
                                self.context_id, self.current_task_id, a2a.new_id(),
                                frame, append=False, last=True))
                    self.last_audio_out = time.monotonic()
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self._closing = True
        if self.nova:
            await self.nova.close()


class AppState:
    def __init__(self, faults: "frozenset[str]" = frozenset(), stub_nova: bool = False) -> None:
        self.secrets = SecretCache(CONFIG.region)
        with open(CONFIG.persona_path, "r", encoding="utf-8") as f:
            self.persona = f.read()
        # Fault flags and the offline stub-Nova mode are test-only affordances
        # for the Tester's audio scenario; server.main refuses them unless the
        # bridge is bound to a loopback host.
        self.faults = faults
        self.stub_nova = stub_nova


def _today() -> str:
    """Caller-local date for the persona (container clock is UTC)."""
    import datetime as dt
    import os
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("DEMO_TIMEZONE", "America/Chicago"))
    except Exception:  # noqa: BLE001 - slim images may lack tzdata
        tz = dt.timezone(dt.timedelta(hours=-5))
    return dt.datetime.now(tz).strftime("%A, %B %d, %Y")


def _b64(s: str) -> bytes:
    import base64
    if not s:
        return b""
    return base64.b64decode(s)


def _is_interrupted(content) -> bool:
    if isinstance(content, str) and "interrupted" in content:
        try:
            return bool(json.loads(content).get("interrupted"))
        except ValueError:
            return False
    if isinstance(content, dict):
        return bool(content.get("interrupted"))
    return False


def _short(content_id) -> str:
    """The first 8 characters of a Nova content ID, enough to pair a block's start and end."""
    return str(content_id or "-")[:8]


def _join(earlier: str, more: str) -> str:
    """Text for one turn, gathered from several Nova text outputs."""
    return " ".join(part for part in ((earlier or "").strip(), (more or "").strip()) if part)


def _generation_stage(fields) -> Optional[str]:
    """generationStage (SPECULATIVE or FINAL) from a contentStart's additionalModelFields,
    which Nova sends as a JSON string."""
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except ValueError:
            return None
    return fields.get("generationStage") if isinstance(fields, dict) else None


# --- HTTP / WS routes -------------------------------------------------------
async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def a2a_ws(request: web.Request) -> web.WebSocketResponse:
    state: AppState = request.app["state"]
    secret = state.secrets.get(CONFIG.a2a_secret_id, label="a2a bearer secret")
    ok, form = bearer_matches(request.headers.get("Authorization"), secret)
    log.info("upgrade attempt bearer_form=%s ok=%s", form, ok)
    if not ok:
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(protocols=("a2a",))
    # Echo A2A-Extensions.
    ext = request.headers.get("A2A-Extensions")
    if ext:
        ws.headers["A2A-Extensions"] = ext
    await ws.prepare(request)
    session = Session(ws, state, request.headers.get("traceparent"), request.headers.get("tracestate"))
    log.info("ws open ext=%s subprotocol=%s", ext, request.headers.get("Sec-WebSocket-Protocol"))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await session.handle_frame(a2a.parse_frame(msg.data))
                except Exception:  # noqa: BLE001
                    log.exception("frame handling error: %s", msg.data[:300])
            elif msg.type == WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
            else:
                log.info("ws non-text frame type=%s len=%s", msg.type,
                         len(msg.data) if isinstance(msg.data, (bytes, str)) else "-")
    finally:
        log.info("ws closed code=%s ctx=%s audio_in_frames=%d", ws.close_code,
                 session.context_id, session._audio_in_frames)
        await session.close()
    return ws


def make_app(faults: "frozenset[str]" = frozenset(), stub_nova: bool = False) -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app["state"] = AppState(faults=faults, stub_nova=stub_nova)
    app.router.add_get("/health", health)
    app.router.add_get("/a2a", a2a_ws)
    testpage.add_routes(app)  # a2a-voice: browser web-call test page (D5)
    return app


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def main(argv: "list[str] | None" = None) -> None:
    import argparse

    from .stubnova import AUDIO_FAULTS

    parser = argparse.ArgumentParser(description="Demo 1 audio bridge")
    # Default host 0.0.0.0 matches the Fargate container, where the ECS service
    # (not the bind) is the network boundary. Offline test runs pass
    # --host 127.0.0.1; fault and stub modes require a loopback host.
    parser.add_argument("--host", default=os.environ.get("BRIDGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=CONFIG.port)
    parser.add_argument("--stub-nova", action="store_true",
                        help="use the scripted offline Nova stub (loopback only)")
    parser.add_argument("--fault", action="append", default=[], metavar="NAME",
                        help="activate an audio fault for the Tester's negative suite (loopback only)")
    args = parser.parse_args(argv)

    faults = frozenset(args.fault or [])
    unknown = [f for f in faults if f not in AUDIO_FAULTS]
    if unknown:
        parser.error("unknown fault name(s): " + ", ".join(sorted(unknown))
                     + "; known: " + ", ".join(sorted(AUDIO_FAULTS)))
    # Fault and stub modes are test affordances and must never run on a public
    # bind: refuse them unless the host is loopback (mirrors the kit's rule).
    if (faults or args.stub_nova) and args.host not in _LOOPBACK_HOSTS:
        parser.error(f"--fault/--stub-nova require --host in {sorted(_LOOPBACK_HOSTS)}, got {args.host!r}")
    if faults:
        log.info("ACTIVE FAULTS: %s", ", ".join(sorted(faults)))
    if args.stub_nova:
        log.info("stub-nova mode: scripted offline Nova session")

    # a2a-voice: no request line in the access log, so the test-page token (?k=) never reaches CloudWatch.
    web.run_app(make_app(faults=faults, stub_nova=args.stub_nova), host=args.host, port=args.port,
                access_log_format='%a %t %s %b %Tf')


if __name__ == "__main__":
    main()
