"""Settings from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api"
    llm_mode: str = "live"  # live | mock

    haiku_model: str = "anthropic/claude-haiku-5.5"
    haiku_reasoning: str = "none"  # none | low | medium
    haiku_max_tokens: int = 400
    # OpenRouter list prices (USD / 1M tokens, checked 2026-11-17). Used only when the response has no usage.cost.
    haiku_price_in: float = 0.10
    haiku_price_out: float = 0.50

    jev_model: str = "typesafe/jev-1.14"
    jev_fallback_model: str = "typesafe/jev-1.13"
    jev_confidence_threshold: float = 0.8
    jev_price_in: float = 0.042  # docs.typesafe.ai/models: $0.042 / 1M input tokens, output free

    http_timeout_s: float = 15.0
    http_max_retries: int = 2

    tms_latency_ms: int = 150
    tms_failure_rate: float = 0.0

    turn_timeout_s: float = 10.0
    max_reprompts_per_slot: int = 2
    max_off_topic: int = 3

    a2a_api_key: str = "local-dev-key"
    a2a_agent_name: str = "eta-check-agent"
    a2a_engine: str = "jev_haiku"  # engine used for Connect sessions

    bench_max_cost_usd: float = 2.0
    allow_test_runner: bool = True  # set false on any network-exposed deployment (it runs pytest in a subprocess)

    # ---- voice (web mic conversation; STT/TTS/VAD owned by this server) ----
    voice_mode: str = "aws"  # aws (Transcribe streaming + Polly) | mock (silent TTS, STT unavailable; tests inject fakes)
    aws_region: str = "ap-northeast-2"
    voice_sample_rate: int = 16000  # browser sends / receives 16-bit mono PCM at this rate (Polly pcm: 8000|16000)
    polly_voice: str = "Seoyeon"  # ko-KR: Seoyeon (neural/generative/standard), Jihye (neural), Hyunduk (generative)
    polly_engine: str = "generative"  # generative: more natural (first audio ~220 ms vs ~80 ms for neural)
    transcribe_vocabulary: str = ""  # optional pre-created Transcribe custom vocabulary name (phrase hints)
    vad_model_url: str = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2/src/silero_vad/data/silero_vad.onnx"
    vad_model_sha256: str = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
    vad_threshold: float = 0.5        # speech prob to count a frame as speech
    vad_neg_threshold: float = 0.35   # prob below which a frame counts as silence (hysteresis)
    vad_min_speech_ms: int = 160      # speech needed to open a turn
    vad_barge_in_ms: int = 400        # speech needed to interrupt the agent while it is talking (echo / cough guard)
    vad_silence_ms: int = 700         # silence that ends a turn
    vad_short_silence_ms: int = 350   # ... when the partial transcript already ends like a sentence (요/다/까/.)
    stt_final_timeout_s: float = 3.0
    filler_after_ms: int = 900        # play a filler if no reply text this long after the transcript (when enabled)
    # Approximate list prices for the voice cost estimate (USD; us-east-1, verify for ap-northeast-2 before quoting)
    transcribe_price_per_min: float = 0.024
    polly_price_per_mchar: dict[str, float] = {"standard": 4.0, "neural": 16.0, "generative": 30.0}

    @property
    def is_mock(self) -> bool:
        return self.llm_mode == "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
