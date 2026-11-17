# acc-poc — Haiku Only vs Jev + Haiku Agentic Call Center

택배/화물 기사에게 전화해 **도착 예정시간(ETA)·운송 특이사항·지연시간**을 확인하고 TMS에 기록하는 에이전트를
두 아키텍처로 구현해 비교하는 PoC. Amazon Connect Customer가 **A2A(v1.0 + Connect 확장)**로 호출할 수 있는 독립 서비스 형태.

| | A. Haiku Only | B. Jev + Haiku |
|---|---|---|
| 판단 | Claude Haiku 5.5가 매 턴 intent·슬롯·행동·응답 생성 | Jev 1.14(System One)가 intent·특이사항 분류, 공통 파서가 시간 추출 |
| 응답 | 생성 | 템플릿 즉시 반환, 복잡/저신뢰 턴만 Haiku fallback (임계값 0.8, 설정 가능) |
| 도구 | 도구 실행 후 Haiku가 결과 반영해 마무리 | 발화와 병렬 실행 |
| 공통 | 상태 머신·규칙 엔진·템플릿 저장소·TMS Mock·오케스트레이터·A2A 어댑터 | |

## 결과 (run 20261117-183723, 실제 API, 5회 반복)

| | A. Haiku Only | B. Jev + Haiku |
|---|---:|---:|
| 첫 응답 준비 P50 / P95 | 1,945 / 3,809 ms | **415 / 2,071 ms** |
| 통화당 모델 비용 (1만 통화) | $0.000670 ($6.70) | **$0.000145 ($1.45)** |
| 판단 정확도 / 통화 성공률 | 96.7% / 100% | 100% / 100% |
| A2A 어댑터 오버헤드 P50 (로컬) | 1.1 ms | 0.9 ms |

해석과 한계: [results/20261117-183723/findings.md](results/20261117-183723/findings.md) · 전체 표: [report.md](results/20261117-183723/report.md)

## 실행

```bash
uv sync
cp .env.example .env          # OPENROUTER_API_KEY 입력 (Haiku와 Jev 모두 OpenRouter 경유)

uv run pytest -q                                       # 단위·통합·A2A 프로토콜 테스트 (오프라인 mock)
uv run uvicorn app.main:create_app --factory --port 8080   # http://127.0.0.1:8080  A/B 나란히 대화하는 UI
LLM_MODE=mock uv run uvicorn app.main:create_app --factory --port 8080   # 키 없이 UI/프로토콜 확인

# 로컬 Mock Connect 클라이언트로 A2A 호출 (WS 또는 HTTPS)
uv run python -m app.adapters.mock_connect --url ws://127.0.0.1:8080/a2a/ws --key local-dev-key --say "한 시간 정도요" --say "없어요"
uv run python -m app.adapters.mock_connect --url http://127.0.0.1:8080/a2a --key local-dev-key

# 벤치마크 (실제 API, 비용 상한 BENCH_MAX_COST_USD)
uv run python -m app.benchmark.runner --reps 5
uv run python -m app.benchmark.runner --mock --reps 1   # 파이프라인 점검용 (결과는 mock으로 표시)
```

결과: `results/<run_id>/report.md`, `summary.json`, `charts/*.png` (원시 JSONL은 `results/raw/<run_id>/`).

## 구조

```text
app/
  adapters/  rest.py (UI/REST/WS) · a2a.py (A2A 프로토콜) · a2a_server.py (HTTPS/WSS 전송) · mock_connect.py (Connect 대역 클라이언트)
  core/      orchestrator.py · state.py · rules.py (결정적 규칙) · events.py (공통 입출력 계약) · decision.py
  engines/   haiku_only.py · jev_haiku.py
  services/  llm.py (OpenRouter: Haiku 스트리밍 + Jev systemone) · llm_mock.py · parser.py (한국어 시간 파서) · responses.py · tms_mock.py
  voice/     stt.py · tts.py (인터페이스만, Phase 3)
  benchmark/ cases.py (결정 30건, 통화 시나리오 20건) · runner.py · metrics.py (요약·차트·리포트)
  web/       index.html
docs/        architecture.md · connect-integration.md · voice-design.md · cost-model.md
```

## 벤치마크 구성

1. **순수 판단** — 고정 상태 + 한 발화 → `engine.decide()` + 규칙 엔진. 30건 × 엔진 2 × reps. intent / 최종 행동 / 슬롯 정확도, 판단시간, TTFT, 토큰, 비용, fallback.
2. **전체 대화** — 20개 시나리오(정상, 체증·지연, 고장, 사고, ETA 불명확, 모호 답변·재질문, 무관한 질문, 담당자 요청, 종료 요청, 기상, 복합 문장, TMS 장애)를
   기사 시뮬레이터가 에이전트 행동에 반응해 진행. 성공률(결과+슬롯+TMS), 잘못된 종료, 재질문, 첫 응답 준비 시간, 턴 시간, 시나리오 완료시간, 통화당 비용, 1k/10k 통화 비용.
3. **A2A 경유 vs 직접** — 실제 로컬 서버의 WebSocket A2A 엔드포인트를 Mock Connect가 호출. 프로토콜 위반 검사, INIT 시간, 첫 청크, A2A 오버헤드(클라이언트 관측 − 서버 `invoke_agent` 스팬).

두 엔진은 같은 입력·상태를 받고, (케이스, 엔진, 반복) 단위를 고정 시드로 섞어 실행한다.

## 범위와 한계

- STT/TTS는 인터페이스만 (docs/voice-design.md). 실제 Amazon Connect 통합 테스트는 수행하지 않았다 (docs/connect-integration.md의 Phase 2 체크리스트).
- Haiku는 사용자 요구대로 OpenRouter의 OpenAI 호환 chat completions로 호출한다 (Anthropic SDK 직접 호출 아님). 프롬프트 캐싱은 시스템 프롬프트가 최소 캐시 길이보다 짧아 적용하지 않았다.
- 세션 상태는 프로세스 메모리. 비용은 모델 API만 (docs/cost-model.md).
