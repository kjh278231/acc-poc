"""Amazon Nova 2 Sonic bidirectional session over the experimental SDK.

Confirmed against aws-sdk-bedrock-runtime 0.11.0 (introspected, not guessed), and
re-checked against 0.12.0 with smithy-core 0.9.0, smithy-aws-core 0.12.0 and
smithy-http 0.6.0 on Python 3.15 (same names and signatures):

  cfg    = await AsyncBedrockRuntimeConfig.resolve(...)      # default cred chain
  client = AsyncBedrockRuntimeClient(config=cfg)
  stream = await client.invoke_model_with_bidirectional_stream(
             InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID))
  await stream.input_stream.send(
      InvokeModelWithBidirectionalStreamInputChunk(
          value=BidirectionalInputPayloadPart(bytes_=<json-or-audio bytes>)))
  output, receiver = await stream.await_output()
  async for out in receiver:                                 # out.value.bytes_ is a JSON event

Every Nova event is a JSON object ``{"event": {...}}`` serialized to bytes. The
session keeps one USER AUDIO content block open for the whole call (promptEnd is
never sent) and interleaves tool results as their own content blocks.

The default credential chain resolves the Fargate task role from the
``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` the ECS agent injects (README risk:
"explicit container credential resolver"); no keys live in the image.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import AsyncIterator, Optional

from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
    InvokeModelWithBidirectionalStreamOperationInput,
)

log = logging.getLogger("bridge.nova")


def _new_id() -> str:
    return str(uuid.uuid4())


class _BotocoreCredentialsResolver:
    """Bridges botocore credential resolution to the SDK's identity resolver API.

    ``AsyncBedrockRuntimeConfig.resolve`` did NOT wire a credential resolver for
    the CRT SigV4 signer in this environment, so the stream went out unsigned and
    Nova timed out with no response. botocore's chain resolves the Fargate task
    role (container credentials), the shared-credentials file the sandbox writes,
    and env vars, and refreshes short-lived credentials transparently. We read a
    frozen snapshot on each ``get_identity`` so a rotated/refreshed credential is
    always picked up.
    """

    def __init__(self) -> None:
        import botocore.session

        self._session = botocore.session.get_session()
        self._creds = self._session.get_credentials()
        if self._creds is None:
            raise RuntimeError("no AWS credentials found by the botocore chain")

    async def get_identity(self, *, properties=None):  # noqa: ANN001
        from smithy_aws_core.identity import AWSCredentialsIdentity

        frozen = self._creds.get_frozen_credentials()
        return AWSCredentialsIdentity(
            access_key_id=frozen.access_key,
            secret_access_key=frozen.secret_key,
            session_token=frozen.token,
        )

    async def invalidate(self, *, identity=None, **_kwargs):  # noqa: ANN001
        return None


class NovaSonicSession:
    """Manages one Nova Sonic bidirectional stream for the length of a call."""

    def __init__(self, *, region: str, model_id: str, voice_id: str, output_sample_rate: int,
                 input_sample_rate: int, system_prompt: str, tool_specs: list[dict]):
        self.region = region
        self.model_id = model_id
        self.voice_id = voice_id
        self.output_sample_rate = output_sample_rate
        # Nova 2 Sonic fails the stream (ModelStreamErrorException) when an 8 kHz audio
        # input is combined with a tool configuration. Connect telephony sends 8 kHz,
        # so feed Nova 16 kHz and upsample the caller's audio on the way in.
        self.source_sample_rate = input_sample_rate
        self.input_sample_rate = max(input_sample_rate, 16000)
        self.system_prompt = system_prompt
        self.tool_specs = tool_specs
        self.prompt_name = _new_id()
        self.audio_content_name = _new_id()
        self._client: Optional[AsyncBedrockRuntimeClient] = None
        self._stream = None
        self._receiver = None
        self._output_task = None
        self._send_lock = asyncio.Lock()

    async def open(self, prime_greeting: bool = True) -> None:
        # The default AIOHTTPClient transport does NOT support duplex (bidirectional)
        # event streaming; the SDK raises UnsupportedTransportError. Nova Sonic needs
        # the CRT transport (README top risk: "CRT transport for the stream").
        from smithy_http.aio.crt import AWSCRTHTTPClient

        cfg = await AsyncBedrockRuntimeConfig.resolve(
            region=self.region, transport=AWSCRTHTTPClient(),
            aws_credentials_identity_resolver=_BotocoreCredentialsResolver())
        self._client = AsyncBedrockRuntimeClient(config=cfg)
        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id))
        # await_output() resolves only once Nova sends its first frame, and Nova sends
        # nothing until it has received input. Awaiting it BEFORE sending sessionStart
        # deadlocks until the server times out (~8 min, then ModelTimeoutException).
        # Start it concurrently, send the setup events, then collect the receiver
        # (same order as the AWS Nova Sonic sample).
        output_task = asyncio.create_task(self._stream.await_output())
        await self._send_event({"sessionStart": {
            "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7},
            "turnDetectionConfiguration": {"endpointingSensitivity": "HIGH"}}})
        await self._send_prompt_start()
        await self._send_system_prompt()
        # Prime the greeting as a COMPLETE user text turn BEFORE opening the audio
        # block: Nova generates its first response to it, so the agent greets first.
        # The audio input block then stays open for the rest of the call.
        if prime_greeting:
            await self._prime_greeting_turn()
        await self._open_audio_input()
        self._output_task = output_task
        self._last_audio_in = 0.0
        self._feeder = asyncio.create_task(self._silence_feeder())
        log.info("Nova session open (prompt=%s, voice=%s, out=%dHz, primed=%s)",
                 self.prompt_name, self.voice_id, self.output_sample_rate, prime_greeting)

    async def _send_event(self, event: dict) -> None:
        payload = json.dumps({"event": event}).encode()
        async with self._send_lock:
            await self._stream.input_stream.send(
                InvokeModelWithBidirectionalStreamInputChunk(
                    value=BidirectionalInputPayloadPart(bytes_=payload)))

    async def _send_prompt_start(self) -> None:
        prompt_start = {
            "promptName": self.prompt_name,
            "textOutputConfiguration": {"mediaType": "text/plain"},
            "audioOutputConfiguration": {
                "mediaType": "audio/lpcm",
                "sampleRateHertz": self.output_sample_rate,
                "sampleSizeBits": 16,
                "channelCount": 1,
                "voiceId": self.voice_id,
                "encoding": "base64",
                "audioType": "SPEECH",
            },
        }
        if self.tool_specs:
            prompt_start["toolUseOutputConfiguration"] = {"mediaType": "application/json"}
            prompt_start["toolConfiguration"] = {"tools": self.tool_specs}
        await self._send_event({"promptStart": prompt_start})

    async def _send_system_prompt(self) -> None:
        content_name = _new_id()
        await self._send_event({"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "type": "TEXT", "role": "SYSTEM",
            "textInputConfiguration": {"mediaType": "text/plain"}}})
        await self._send_event({"textInput": {
            "promptName": self.prompt_name, "contentName": content_name,
            "content": self.system_prompt}})
        await self._send_event({"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name}})

    async def _open_audio_input(self) -> None:
        await self._send_event({"contentStart": {
            "promptName": self.prompt_name, "contentName": self.audio_content_name,
            "type": "AUDIO", "role": "USER", "interactive": True,
            "audioInputConfiguration": {
                "mediaType": "audio/lpcm",
                "sampleRateHertz": self.input_sample_rate,
                "sampleSizeBits": 16,
                "channelCount": 1,
                "audioType": "SPEECH",
                "encoding": "base64",
            }}})

    async def _prime_greeting_turn(self) -> None:
        """Inject a COMPLETE USER text turn so Nova greets first (README CHANNEL_STATE).

        Sent before the audio block opens: contentStart/textInput/contentEnd is a
        finished user turn, which triggers Nova's first generation.
        """
        content_name = _new_id()
        await self._send_event({"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "type": "TEXT", "interactive": True, "role": "USER",
            "textInputConfiguration": {"mediaType": "text/plain"}}})
        await self._send_event({"textInput": {
            "promptName": self.prompt_name, "contentName": content_name,
            "content": "The caller has just connected. Greet them now."}})
        await self._send_event({"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name}})

    async def _silence_feeder(self) -> None:
        """Keep Nova's audio input flowing while the caller is silent.

        Nova Sonic generates only while input audio streams in (measured: a primed
        greeting with no audio produced usage events and nothing else). Connect may
        pause inbound audio, and the local harness waits for the greeting before
        speaking, so send 100 ms of silence whenever no caller audio arrived in 200 ms.
        """
        loop = asyncio.get_running_loop()
        frame = b"\x00\x00" * (self.input_sample_rate // 10)
        try:
            while self._stream is not None:
                await asyncio.sleep(0.1)
                if loop.time() - self._last_audio_in >= 0.2 and self._stream is not None:
                    await self._send_raw_audio(frame)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - stream closing
            log.debug("silence feeder stopped", exc_info=True)

    async def _send_raw_audio(self, pcm: bytes) -> None:
        await self._send_event({"audioInput": {
            "promptName": self.prompt_name, "contentName": self.audio_content_name,
            "content": base64.b64encode(pcm).decode()}})

    async def send_audio(self, pcm: bytes) -> None:
        """Forward raw PCM from Connect as a base64 audioInput event."""
        self._last_audio_in = asyncio.get_running_loop().time()
        if self.source_sample_rate != self.input_sample_rate:
            from .audio import upsample_linear
            pcm = upsample_linear(pcm, self.source_sample_rate, self.input_sample_rate)
        await self._send_raw_audio(pcm)

    async def send_tool_result(self, tool_use_id: str, result: dict, is_error: bool = False) -> None:
        """Return a tool result to Nova as its own TOOL content block."""
        content_name = _new_id()
        await self._send_event({"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "type": "TOOL", "role": "TOOL",
            "toolResultInputConfiguration": {
                "toolUseId": tool_use_id, "type": "TEXT",
                "textInputConfiguration": {"mediaType": "text/plain"}}}})
        await self._send_event({"toolResult": {
            "promptName": self.prompt_name, "contentName": content_name,
            "content": json.dumps(result),
            "status": "error" if is_error else "success"}})
        await self._send_event({"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name}})

    async def events(self) -> AsyncIterator[dict]:
        """Yield decoded Nova events (the inner ``event`` object)."""
        if self._receiver is None and self._output_task is not None:
            _output, self._receiver = await self._output_task
        async for out in self._receiver:
            value = getattr(out, "value", None)
            data = getattr(value, "bytes_", None) if value is not None else None
            if not data:
                continue
            try:
                parsed = json.loads(data)
            except ValueError:
                log.warning("non-JSON Nova chunk (%d bytes)", len(data))
                continue
            yield parsed.get("event", parsed)

    async def close(self) -> None:
        try:
            if getattr(self, "_feeder", None) is not None:
                self._feeder.cancel()
            if self._stream is not None:
                # Close the open audio block and prompt, then the stream.
                try:
                    await self._send_event({"contentEnd": {
                        "promptName": self.prompt_name, "contentName": self.audio_content_name}})
                    await self._send_event({"promptEnd": {"promptName": self.prompt_name}})
                    await self._send_event({"sessionEnd": {}})
                except Exception:  # noqa: BLE001
                    pass
                await self._stream.close()
        finally:
            self._stream = None
            self._receiver = None
            log.info("Nova session closed")
