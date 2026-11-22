"""Speech engines that replace the Nova Sonic session.

Every engine exposes the same surface as ``nova.NovaSonicSession`` and emits the
same Nova-style event dicts (contentStart / audioOutput / textOutput / toolUse /
usageEvent / contentEnd), so ``server.py`` stays a pure A2A protocol state machine.

  loopback  - no AI. Polly greeting, energy VAD, plays the caller's words back.
  cascade   - Transcribe Streaming -> Bedrock ConverseStream -> TTS (P6).
  nova      - the workshop's Nova Sonic session (NOVA_REGION, e.g. ap-northeast-1).
"""
