"""Bridge configuration, read from the environment at startup.

Every value has a default that matches the README so the container runs with
the documented environment, and a local harness can override any of them.
Secrets are never read here; they are fetched lazily from Secrets Manager by
``secrets.py`` so the process starts even if the secret is briefly unreachable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))

    # Secrets (IDs only; values fetched at runtime).
    a2a_secret_id: str = field(
        default_factory=lambda: os.environ.get("A2A_SECRET_ID", "a2a-demo/voice-partner-api-key"))
    insurance_api_key_secret_id: str = field(
        default_factory=lambda: os.environ.get("INSURANCE_API_KEY_SECRET_ID", "a2a-demo/insurance-api-key"))

    # Insurance workshop API.
    insurance_api_url: str = field(default_factory=lambda: os.environ.get(
        "INSURANCE_API_URL", ""))  # set by deploy.py from demo.env

    # Nova Sonic.
    # The bridge calls Nova from NOVA_REGION, which defaults to the deployment
    # Region (AWS_REGION) so a single-Region event keeps the Nova call in-Region.
    # It can still be pinned to us-west-2 (or any supported Region) with
    # NOVA_REGION. Nova 2 Sonic bidirectional streams in us-east-1 failed about
    # half the time at stream start (ModelStreamErrorException) in the build
    # account on 2026-09-29, while the same code succeeded 6/6 in us-west-2;
    # Nova 2.5 Sonic is retested in us-east-1 under task 3.7 before the default
    # moves there. Only the Nova call uses NOVA_REGION; Connect, secrets and the
    # bridge stay in AWS_REGION.
    nova_region: str = field(default_factory=lambda: os.environ.get(
        "NOVA_REGION", os.environ.get("AWS_REGION", "us-east-1")))
    model_id: str = field(default_factory=lambda: os.environ.get("MODEL_ID", "amazon.nova-2-5-sonic"))
    voice_id: str = field(default_factory=lambda: os.environ.get("VOICE_ID", "tiffany"))
    # Polly PCM supports 8000/16000 only, so the bypass engines counter-propose 16 kHz.
    output_sample_rate: int = field(default_factory=lambda: _int(
        "OUTPUT_SAMPLE_RATE", 24000 if os.environ.get("A2A_ENGINE", "nova") == "nova" else 16000))

    # Engine selection (a2a-voice): loopback | cascade | nova
    engine: str = field(default_factory=lambda: os.environ.get("A2A_ENGINE", "nova"))
    language: str = field(default_factory=lambda: os.environ.get("A2A_STT_LANG", "ko-KR"))
    tts_voice: str = field(default_factory=lambda: os.environ.get("A2A_TTS_VOICE", "Seoyeon"))
    tts_engine: str = field(default_factory=lambda: os.environ.get("A2A_TTS_ENGINE_KIND", "neural"))
    llm_model_id: str = field(default_factory=lambda: os.environ.get("A2A_LLM_MODEL_ID", "")
                              or "apac.amazon.nova-lite-v1:0")
    llm_region: str = field(default_factory=lambda: os.environ.get("A2A_LLM_REGION", ""))
    # cascade engine knobs (P6-9 latency tuning)
    turn_end_ms: int = field(default_factory=lambda: _int("A2A_TURN_END_MS", 150))
    barge_in_chars: int = field(default_factory=lambda: _int("A2A_BARGE_IN_CHARS", 2))
    barge_in_vad_ms: int = field(default_factory=lambda: _int("A2A_BARGE_IN_VAD_MS", 400))
    llm_max_tokens: int = field(default_factory=lambda: _int("A2A_LLM_MAX_TOKENS", 300))
    llm_temperature: float = field(default_factory=lambda: float(os.environ.get("A2A_LLM_TEMPERATURE", "0")))
    llm_top_k: int = field(default_factory=lambda: _int("A2A_LLM_TOP_K", 1))
    greeting: str = field(default_factory=lambda: os.environ.get("A2A_GREETING", ""))
    persona_cascade_path: str = field(default_factory=lambda: os.environ.get(
        "A2A_PERSONA_PATH", os.path.join(os.path.dirname(__file__), "persona_ko.txt")))
    loopback_max_turns: int = field(default_factory=lambda: _int("LOOPBACK_MAX_TURNS", 3))
    vad_threshold: float = field(default_factory=lambda: float(os.environ.get("VAD_THRESHOLD", "600")))
    vad_end_ms: int = field(default_factory=lambda: _int("VAD_END_MS", 700))

    # Demo caller identity (pre-authenticated, mirrors Demo 2).
    demo_customer_id: str = field(default_factory=lambda: os.environ.get("DEMO_CUSTOMER_ID", "CUST-001"))
    demo_first_name: str = field(default_factory=lambda: os.environ.get("DEMO_FIRST_NAME", "Carlos"))
    demo_last_name: str = field(default_factory=lambda: os.environ.get("DEMO_LAST_NAME", "Salazar"))

    # Connect instance (DescribeContact for the caller's number on ESCALATE).
    connect_instance_id: str = field(default_factory=lambda: os.environ.get(
        "CONNECT_INSTANCE_ID", ""))  # set by deploy.py from demo.env
    # TMS endpoint for callback requests on ESCALATE; empty logs CALLBACK_NOT_FILED instead.
    tms_url: str = field(default_factory=lambda: os.environ.get("A2A_TMS_URL", ""))

    # Behaviour knobs (verify-on-build items).
    silence_keepalive_seconds: float = field(
        default_factory=lambda: float(os.environ.get("SILENCE_KEEPALIVE_SECONDS", "3.0")))
    tool_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("TOOL_TIMEOUT_SECONDS", "5.0")))
    goodbye_max_wait_seconds: float = field(
        default_factory=lambda: float(os.environ.get("GOODBYE_MAX_WAIT_SECONDS", "4.0")))
    frame_ms: int = field(default_factory=lambda: _int("FRAME_MS", 100))

    # Persona file (baked into the image next to the code).
    persona_path: str = field(default_factory=lambda: os.environ.get(
        "PERSONA_PATH", os.path.join(os.path.dirname(__file__), "persona.txt")))


CONFIG = Config()
