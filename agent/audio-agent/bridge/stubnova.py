"""An offline, scripted stand-in for the Nova Sonic session, plus fault flags.

The real bridge talks to Amazon Nova Sonic over a bidirectional Bedrock stream,
so it cannot run without AWS. For the Tester's audio scenario (task 3.7/3.8) the
bridge runs on 127.0.0.1 with this stub swapped in for ``NovaSonicSession``: it
emits the same event dicts ``server.py`` consumes (``contentStart``,
``audioOutput``, ``textOutput``, ``toolUse``, ``usageEvent``, ``contentEnd``) on
a fixed script, so the bridge produces real A2A audio frames, traces and a
FINISH without any model call.

It also carries the audio fault flags the Tester's negative suite pairs with the
ART checks. Like the kit, the bridge refuses to start with any fault unless it
is bound to a loopback host (enforced in ``server.main``); the faults only ever
perturb the scripted behaviour, never the auth or bind.

The script, per connected call:
* greeting turn (after ``open``): assistant audio + transcript, no tool;
* caller turn 1 (after the first ``send_audio``): a ``get_policies`` tool call,
  then assistant audio + transcript;
* caller turn 2 (after the second ``send_audio``): an ``end_call`` COMPLETE tool
  call so the bridge sends FINISH.

The transcripts match the Tester's audio scenario so TRACE-06 can match the
turn input.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import AsyncIterator, Optional

# The fault names this stub understands. Each perturbs exactly one thing.
AUDIO_FAULTS: dict[str, str] = {
    "no-audio": "send no AUDIO_RESPONSE_CHUNK artifact (empty assistant audio)",
    "audio-no-last-chunk": "never set lastChunk on audio artifacts",
    "audio-bad-media-type": "label audio parts text/plain instead of audio/lpcm",
    "bad-audio-output-config": "counter-propose an unsupported 44100 Hz audioOutputConfiguration",
}

# Caller transcripts the stub reports, matching scenarios/audio.py.
_CALLER_TURN_TEXT = "I need to check on my insurance claim"
_GOODBYE_TURN_TEXT = "No thanks, goodbye"
_GREETING_TEXT = "Hello, this is the claims assistant. How can I help?"
_RESPONSE_TEXT = "I found your active auto policy. Your claim is being processed."


def _pcm(ms: int, rate: int) -> bytes:
    """Silence PCM of ``ms`` milliseconds (16-bit mono)."""
    return b"\x00\x00" * int(rate * ms / 1000)


class StubNovaSession:
    """A scripted Nova session for offline runs. Same surface as NovaSonicSession."""

    def __init__(self, *, region: str, model_id: str, voice_id: str, output_sample_rate: int,
                 input_sample_rate: int, system_prompt: str, tool_specs: list[dict],
                 faults: frozenset[str] = frozenset()):
        self.region = region
        self.model_id = model_id
        self.voice_id = voice_id
        self.output_sample_rate = output_sample_rate
        self.input_sample_rate = input_sample_rate
        self.system_prompt = system_prompt
        self.tool_specs = tool_specs
        self.faults = faults
        self._events: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._caller_turns = 0
        self._content_counter = 0

    # --- lifecycle ----------------------------------------------------------
    async def open(self, prime_greeting: bool = True) -> None:
        if prime_greeting:
            await self._emit_turn(caller_text="", agent_text=_GREETING_TEXT, tool=None)

    async def close(self) -> None:
        self._closed = True
        await self._events.put(None)  # unblock events()

    async def events(self) -> AsyncIterator[dict]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    # --- inbound (driven by the bridge as caller audio arrives) -------------
    async def send_audio(self, pcm: bytes) -> None:
        # The bridge streams many frames per caller turn; script one turn per
        # burst by only acting on the first frame after the previous turn ended.
        if getattr(self, "_turn_in_flight", False):
            return
        self._turn_in_flight = True
        self._caller_turns += 1
        if self._caller_turns == 1:
            await self._emit_turn(caller_text=_CALLER_TURN_TEXT, agent_text=_RESPONSE_TEXT,
                                  tool=("get_policies", {}, {"policies": [{"id": "POL-1", "type": "auto"}]}))
        else:
            await self._emit_turn(caller_text=_GOODBYE_TURN_TEXT, agent_text="Goodbye.",
                                  tool=("end_call", {"outcome": "COMPLETE", "summary": "caller done"},
                                        {"acknowledged": True}))

    async def send_tool_result(self, tool_use_id: str, result: dict, is_error: bool = False) -> None:
        # The stub already decided the turn's content; nothing to feed back.
        self._turn_in_flight = False

    # --- the script ---------------------------------------------------------
    async def _emit_turn(self, *, caller_text: str, agent_text: str,
                         tool: Optional[tuple]) -> None:
        """Queue one assistant turn's worth of Nova events.

        Same order as Nova 2.5 Sonic on a live call (dry run 2026-10-30, d572fde0):
        the caller's transcript (a FINAL USER text block) comes first, before the
        tool call or the assistant audio contentStart that opens the bridge's turn.
        The assistant's words come twice: a SPECULATIVE text block just before the
        audio block, and a FINAL text block after the audio ends.
        """
        self._content_counter += 1
        cid = f"c{self._content_counter}"
        if caller_text:
            await self._text_block(f"{cid}-user", "USER", "FINAL", caller_text)
        # A tool call, if any, opens the turn first.
        if tool is not None:
            name, args, result = tool
            await self._events.put({"toolUse": {
                "toolName": name, "toolUseId": f"tool-{self._content_counter}",
                "content": json.dumps(args)}})
        await self._text_block(f"{cid}-spec", "ASSISTANT", "SPECULATIVE", agent_text)
        # Assistant audio content block (also opens the turn if there was no tool).
        await self._events.put({"contentStart": {"contentId": cid, "type": "AUDIO", "role": "ASSISTANT"}})
        if "no-audio" not in self.faults:
            await self._events.put({"audioOutput": {
                "content": base64.b64encode(_pcm(200, self.output_sample_rate)).decode()}})
        await self._events.put({"usageEvent": {"totalInputTokens": 12, "totalOutputTokens": 34}})
        await self._events.put({"contentEnd": {"contentId": cid, "type": "AUDIO",
                                               "role": "ASSISTANT", "stopReason": "END_TURN"}})
        await self._text_block(f"{cid}-final", "ASSISTANT", "FINAL", agent_text)

    async def _text_block(self, content_id: str, role: str, stage: str, text: str) -> None:
        await self._events.put({"contentStart": {
            "contentId": content_id, "type": "TEXT", "role": role,
            "additionalModelFields": json.dumps({"generationStage": stage})}})
        await self._events.put({"textOutput": {"contentId": content_id, "role": role, "content": text}})
        await self._events.put({"contentEnd": {"contentId": content_id, "type": "TEXT",
                                               "stopReason": "PARTIAL_TURN"}})


class StubTools:
    """Offline tool executor for stub mode: canned results, no HTTP, no AWS.

    Mirrors ``tools.Tools`` surface (``run`` plus a ``pending`` finish) so the
    server dispatches tools the same way it does against the real Insurance API.
    """

    def __init__(self) -> None:
        self.pending = None

    def run(self, name: str, args: dict) -> dict:
        if name == "get_policies":
            return {"policies": [{"id": "POL-1", "type": "auto", "status": "active"}]}
        if name == "file_claim":
            return {"claimId": "CLAIM-STUB", "status": "received"}
        if name == "end_call":
            from .tools import PendingFinish
            self.pending = PendingFinish(outcome=args.get("outcome", "COMPLETE"),
                                         summary=args.get("summary", ""), reason=args.get("reason"))
            return {"acknowledged": True, "outcome": args.get("outcome", "COMPLETE")}
        return {"ok": True}
