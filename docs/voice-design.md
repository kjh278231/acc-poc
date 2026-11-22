# 음성(STT/TTS) 확장 설계 — Phase 3

## 구현 현황 (웹 마이크 통화, 2026-11-17)

브라우저 마이크로 기사 역할을 하며 A/B 엔진과 실제로 통화한다 (`/voice`). STT·TTS·VAD는 모두 이 서버가 소유한다
(Connect `audioStreamingEnabled=true`와 같은 책임 분담). Connect 오디오 경로(A2A `audio/lpcm`)는 아직 미연결.

| 구성 | 구현 | 비고 |
|---|---|---|
| 전송 | `adapters/voice_ws.py` `/voice/ws` | 브라우저 → 16 kHz PCM16 연속 스트림, 서버 → JSON 이벤트 + PCM16 오디오 |
| VAD·턴 감지 | `voice/vad.py` Silero VAD v6.2 ONNX (서버) | 32 ms 프레임, 히스테리시스(0.5/0.35), 시작 160 ms, 무음 700 ms, 문장 끝(요/다/까/죠/네·구두점)이면 350 ms |
| STT | `voice/aws.py` Amazon Transcribe 스트리밍 ko-KR | 발화마다 스트림 1개 (VAD 시작 시 열어 연결 지연을 발화와 겹침), 프리롤 400 ms |
| TTS | `voice/aws.py` Amazon Polly (pcm 16 kHz) | 서연/지혜 Neural, 서연/현덕 Generative. 응답 본문을 스레드에서 100 ms 단위로 읽어 바로 송신 |
| 캐시 | `voice/cache.py` 메모리 + `.cache/tts/` | 문장 단위. 고정 템플릿 문장·대기 멘트는 세션 시작 시 미리 합성 |
| 파이프라인 | `voice/pipeline.py` `VoiceSession` | 오케스트레이터는 변경 없음 (최종 전사 → `InputMessage(TEXT)`, `TEXT_CHUNK` → 문장별 TTS) |
| 시뮬레이터 | `voice/simulate.py` | 합성 기사 음성을 실시간으로 흘려 넣어 브라우저 없이 같은 경로를 측정 |

**끼어들기**: 에이전트가 말하는 중(또는 응답 준비 중) 400 ms 이상 발화 → `audio_clear`로 브라우저 재생 중단.
아직 말을 시작하지 않은 턴은 취소하고 그 발화를 다음 발화 앞에 합친다(기사가 말을 이어 간 경우).
이미 말하기 시작한 턴은 소리만 끊고 오케스트레이터 턴은 끝까지 진행한다(TMS 기록·FINISH 보존).
끼어들기를 끄면 반이중: 에이전트가 말하는 동안 마이크 입력을 무시한다. 스피커 사용 시 에코로 스스로 끊길 수 있어 이어폰 권장.

**워크플로 패널** (`/voice`): 통화 흐름(인사 → 도착 예정 → 특이사항 → 지연 → TMS 등록·완료 / 담당자 연결 / 통화 종료)에서 현재 질문 중인 단계,
채워진 슬롯, 재질문·업무 외 횟수, 지나온 행동 경로를 표시하고, 이번 턴 처리(듣기 → 턴 종료 → STT → 판단(Jev/Haiku 호출별 진행·fallback)
→ 규칙 엔진(override) → 응답(템플릿/생성) → TMS → TTS → 재생)를 단계별 경과 시간과 함께 실시간으로 켠다.

**측정** (턴마다 `voice_metrics` 이벤트 + `results/voice/YYYYMMDD.jsonl`):
`endpoint_ms`(말 끝 → 턴 종료 판정) · `stt_final_ms`(→ 최종 전사) · `engine_ms`(→ 첫 응답 문장) · `tts_first_audio_ms`(→ 첫 오디오 송신)
· 합계 `response_ms` = 기사가 말을 멈춘 뒤 에이전트 첫 소리까지 (네트워크·브라우저 재생 버퍼 제외). STT/TTS 비용은 us-east-1 정가 기준 추정.

첫 실측 (실제 AWS·OpenRouter, 시뮬레이터 1회 + 브라우저 1회, 표본 작음):

| | 턴 종료 | STT 확정 | 엔진 | TTS 첫 소리 | **응답 P50** |
|---|---:|---:|---:|---:|---:|
| A. Haiku Only | 352 | ~105 | 1,400–3,100 | 70–160 (캐시 미스) | **~2.9–3.2 s** |
| B. Jev + Haiku | 352 | ~105 | ~400 | 0–130 (캐시 적중 시 ~1 ms) | **~0.9 s** |

## 책임 소유권

| 모드 | STT | TTS | 턴 감지/끼어들기 | 우리 서버가 받는 것 |
|---|---|---|---|---|
| Connect handoff, `audioStreamingEnabled=false` | Connect (Lex) | Connect | Connect (`INTERRUPTION` 이벤트로 알려줌) | 텍스트 턴 — **현재 구현으로 동작** |
| Connect handoff, `audioStreamingEnabled=true` (WSS 전용, immediateHandoff 필수, 단일 collaborator) | **우리** | **우리** | **우리** (VAD; 입력 오디오에 턴 경계 없음) | `audio/lpcm` raw 청크 연속 스트림 |
| Web UI / 자체 SIP | 우리 | 우리 | 우리 | – |

## 파이프라인 (인터페이스: `app/voice/stt.py`, `app/voice/tts.py`)

```text
A2A raw audio/lpcm (8/16/24 kHz, 16-bit mono, 세션 고정)
  → TurnDetector (VAD, end-of-turn) ─┐
  → SpeechToText.stream (partial/final) ─→ final transcript
  → InputMessage(kind=TEXT)  ── 기존 Orchestrator.handle() (변경 없음)
  → TEXT_CHUNK (문장)  → AudioCache.get() or TextToSpeech.stream()
  → AUDIO_RESPONSE_CHUNK artifactUpdate (응답 1개 = artifact 1개, 작은 프레임, FRAGMENT 금지, lastChunk로 종료)
  → 턴마다 TRACING_SPAN → terminal statusUpdate
```

판단 엔진(engines/), 규칙(core/rules.py), 템플릿, TMS 도구는 텍스트 계약만 보므로 바뀌지 않는다 (완료 조건 6).
추가로 필요한 것은 A2A 어댑터의 오디오 분기(현재는 오디오 part를 거부)와 `AudioPipeline` 구현뿐이다.

## 튜닝 항목

**STT**: 스트리밍 STT, 한국어 숫자·시간 표기 정규화(`services/parser.py` 재사용; "한 시간 반", "삼십 분"), 업무 용어 phrase hint,
VAD 무음 창·최소 발화 길이, 문장 끝 어미(요/다/까)로 end-of-turn 단축, partial 전사로 Jev 사전 판단(투기적 실행).

**TTS**: 템플릿 응답 사전 합성 캐시 — B 아키텍처는 응답 대부분이 템플릿이라 캐시 적중률이 높고 첫 오디오까지 시간이 크게 준다.
가변 부분(분/시간)만 조각 합성하거나 숫자 클립 결합. 스트리밍 TTS, 발화 속도·문장 간격, 도구 실행·Haiku fallback 중 filler("네, 확인해 볼게요").

**끼어들기**: 우리 VAD가 발화 시작을 감지하면 `INTERRUPTION`(USER_AUDIO_INPUT) 송신 → TTS 중단 → 열린 artifact를 lastChunk 없이 버리고
턴 task와 trace를 닫은 뒤 새 응답. 오케스트레이터 턴 취소는 이미 구현(`A2AProtocol._interrupt`), TMS 쓰기는 shield 되어 롤백되지 않는다.

## 계측할 지연 구간

`speech_end → stt_final`, `stt_final → decision`, `tool`, `decision → tts_first_audio`, `tts_first_audio → Connect 재생`(network).
각 구간은 `invoke_agent` 루트 아래 자식 스팬(`stt`, `chat <model>`, `execute_tool`, `tts`)으로 같은 TRACING_SPAN에 싣는다.
