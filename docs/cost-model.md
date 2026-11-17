# 비용 모델

## 모델 API (벤치마크에서 측정)

| 모델 | 경로 | 단가 (USD / 1M tokens) | 출처 |
|---|---|---|---|
| Claude Haiku 5.5 | OpenRouter `anthropic/claude-haiku-5.5` | 입력 0.10, 출력 0.50 | OpenRouter models API, 2026-11-17 |
| Jev 1.14 / 1.13 | OpenRouter `/api/v1/systemone` (`typesafe/jev-1.14`) | 입력 0.042, 출력 무료 | docs.typesafe.ai/models, 2026-11-17 |

- 비용은 OpenRouter 응답의 `usage.cost`(실제 청구액)를 우선 사용하고, 없을 때만 위 단가로 추정한다.
  리포트의 "Share of cost from provider-reported usage"가 실제값 비율이다.
- 1,000 / 10,000 통화 비용 = (벤치마크 시나리오 평균 통화당 비용) × N. 시나리오 구성(정상/예외 비율)이 실제 운영 분포와 다르면 그만큼 달라진다.

## 별도 계산 항목 (이 PoC에서 측정하지 않음)

| 항목 | 산정 방법 |
|---|---|
| Amazon Connect 음성 사용료 | 분당 요금 × 평균 통화 시간 (리전·인바운드/아웃바운드별 요금표) |
| Connect AI agent / 셀프서비스 요금 | Connect 요금표의 AI agent 상호작용 단가 × 턴 수 |
| 전화 회선(DID/outbound 통신료) | 통신사·리전별 분당 요금 |
| STT/TTS | Connect 내장(Lex) 사용 시 Connect 요금에 포함/별도 확인. 외부 STT/TTS(Phase 3)는 공급자 분당/문자당 요금 |
| 인프라 | ALB/ECS(또는 AgentCore) 시간당 + 데이터 전송, Secrets Manager·KMS 소액 |
| 관측 | CloudWatch Logs / X-Ray 등 |

모델 비용은 이 항목들에 비해 매우 작을 가능성이 높으므로, 아키텍처 선택은 비용보다 **응답 속도와 정확도**가 좌우할 가능성이 크다.
