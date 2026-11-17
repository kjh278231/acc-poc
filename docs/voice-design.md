# 음성(STT/TTS) 확장 설계 — Phase 3

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
