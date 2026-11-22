# 아키텍처

## 전체 구조

```text
[Channel]  Web UI ──REST/WS──┐          Amazon Connect Customer (Phase 2) ──A2A JSON-RPC (HTTPS / WSS)──┐
                             ▼                                                                         ▼
                 app/adapters/rest.py                                      app/adapters/a2a_server.py (전송)
                             │                                             app/adapters/a2a.py        (프로토콜)
                             └───────────── InputMessage ─────────────┬────────────────────────────────┘
                                                                     ▼
                                              app/core/orchestrator.py  (세션·턴·도구·이벤트·지표·스팬)
                                                     │            │
                                       engine.decide()            │ rules.resolve()  (app/core/rules.py, 결정적)
                                ┌────────────┴───────────┐        │
                     engines/haiku_only.py    engines/jev_haiku.py │
                     (Haiku: 판단+응답 생성)  (Jev 분류 + parser    │
                                               → 템플릿, 필요 시    │
                                               Haiku fallback)     │
                                                     │             ▼
                                services/parser.py (공통 시간 파서)   services/responses.py (템플릿)
                                                                   services/tms_mock.py (TMS 도구)
                                                     │
                                              OutputEvent 스트림  (TEXT_CHUNK / TEXT_DONE / TOOL_* / FINISH / TURN_END / ERROR)
                                                     │
                              ┌──────────────────────┴───────────────────────┐
                        텍스트 (Phase 1)                         오디오 파이프라인 (Phase 3, app/voice 인터페이스)
```

## 공통 계약 (`app/core/events.py`)

| 타입 | 필드 | 설명 |
|---|---|---|
| `InputMessage` | `session_id, turn_id, trace_id, parent_span_id, kind(text/silence/dtmf/start), text` | 어댑터가 와이어 포맷에서 변환 |
| `OutputEvent` | `type, session_id, turn_id, trace_id, message_id, text, data` | 모든 이벤트에 세 ID가 붙는다 |

- `TEXT_CHUNK`는 문장 단위. 같은 `message_id`의 청크가 하나의 메시지 → A2A에서 artifact 하나.
- `TURN_END.data.metrics`는 턴 지표, `data.spans`는 OTLP로 변환될 스팬(`invoke_agent`, `chat <model>`, `execute_tool <tool>`).
- `STAGE`는 턴 안의 실시간 진행(`decide_start`, `model_start/end`, `decided`, `rules`, `reply`). LLM 클라이언트가 `core/progress.py`의 ContextVar로 보고하고,
  오케스트레이터가 판단 중에도 흘려보낸다. 음성 UI의 워크플로 패널용이며 A2A 어댑터는 무시한다.

## 턴 처리 (`Orchestrator._turn`)

1. **판단**: `engine.decide(state, text)` → `Decision(intent, action, slots, reply?, calls[])`
2. **규칙**: `rules.resolve()` 가 슬롯 검증, 다음 질문 순서(ETA → 특이사항 → 지연), 종료 승인, 담당자 연결을 결정한다.
   엔진이 제안한 행동과 다르면 `overridden=true` 로 기록하고 템플릿 응답을 쓴다.
3. **응답 + 도구**
   - **A (`parallel_tools=False`)**: TMS 도구 실행 → Haiku가 도구 결과로 마무리 문장 생성(2번째 호출) → 발화.
   - **B (`parallel_tools=True`)**: 템플릿을 즉시 발화하면서 도구를 동시에 실행. 도구가 실패하면 **별도 메시지**로 안내한다
     (이미 보낸 메시지를 다시 열지 않음 → A2A artifact 규칙 준수).
4. **지표/스팬**: `decision_ms`, `ttft_ms`, `first_response_ready_ms`, `tool_ms`, `turn_total_ms`, 토큰, 비용, 호출 수, fallback 원인.

TMS 쓰기는 `asyncio.shield`로 보호되어, 끼어들기·취소로 턴이 중단되어도 반쯤 쓰인 상태가 생기지 않는다.
TMS 업데이트는 `idempotency_key`(세션 + 내용 해시)로 중복 기록을 막는다.

## 두 엔진

| | A. Haiku Only | B. Jev + Haiku |
|---|---|---|
| 판단 | Haiku 1회 (JSON: intent, slots, action, reply), 스트리밍 | Jev System One 1회: `intent`(6지선다), `issue`(7지선다) + 확률 |
| 숫자 | Haiku 추출 → 공통 파서가 있으면 파서 값 우선 | 공통 파서 |
| 응답 | Haiku 생성 (규칙 override 시 템플릿) | 템플릿 |
| Fallback | – | 신뢰도 < 임계값(기본 0.8, `JEV_CONFIDENCE_THRESHOLD`), off_topic, 시간 표현 2개 이상, Jev 오류 → Haiku가 그 턴 처리 |
| 도구 | 순차 (도구 → Haiku 마무리 생성) | 병렬 (발화와 동시) |
| 종료/연결 | 공통 rule engine | 공통 rule engine |

## 결정적 규칙 (`app/core/rules.py`)

- 필수 슬롯: `eta_minutes`, `issue`, 그리고 `issue != none`이면 `delay_minutes`. 범위 0–1440분.
- 숫자가 하나뿐인 발화에서 모델 값과 파서 값이 다르면 파서 값을 쓴다.
- `COMPLETE`는 모든 필수 슬롯이 있을 때만. 아니면 다음 질문으로 바꾼다(`blocked_premature_complete`).
- 차량 고장·사고: 지연시간을 한 번 묻고(모르면 바로) 기록 후 `ESCALATE(severe_issue)`.
- 같은 슬롯 재질문 2회 초과 → `ESCALATE(unresolved_<slot>)`. 업무 외 대화 3회 초과 → `ESCALATE`.
- 기사 종료 요청: 필수 정보가 다 있으면 `COMPLETE`, 아니면 `END_CALL`(부분 기록, status=partial).

## 확장 포인트

- 새 채널: `InputMessage`를 만들고 `Orchestrator.handle()`의 이벤트를 렌더링하는 어댑터만 추가.
- 음성: `app/voice/` 인터페이스 구현 → 최종 전사문을 `InputMessage(kind=TEXT)`로, `TEXT_CHUNK`를 TTS로. 엔진·규칙 변경 없음 (docs/voice-design.md).
- 세션 저장소: 지금은 프로세스 메모리. Connect chat은 메시지마다 새 연결이므로 다중 인스턴스 배포 시 Redis/DynamoDB 등 외부 저장소로 교체 필요.
