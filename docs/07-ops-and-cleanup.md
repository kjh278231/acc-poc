# 07. 운영 준비, 싱가포르 복제, 정리

## 7.1 싱가포르 복제 체크

서울에서 끝까지 확인한 뒤 `A2A_REGION=ap-southeast-1`로 02~06을 반복한다. 리전마다 다른 점만 따로 확인한다.

| 항목 | 확인 내용 |
|---|---|
| Lex S2S locale | ko_KR이 되는지. 안 되면 en_US/en_SG 봇 + 한국어 에이전트 (D7) |
| Nova Sonic 호환 음성 | 흐름 "음성 설정" 블록에 고를 수 있는 음성 |
| Bedrock LLM | `A2A_LLM_MODEL_ID`로 쓸 수 있는 모델/추론 프로필 |
| Polly | Seoyeon/Jihye neural (문서상 지원) |
| 전화번호 | 싱가포르 번호 요건 (웹콜이면 불필요) |
| 지연 | 서울과 같은 T2 측정. 한국 사용자가 싱가포르에 걸면 네트워크 지연이 추가된다 |

## 7.2 운영 전 항목 (워크숍 Module 4 참고)

- [ ] **AWS 계정팀/SA 검토**: 공식 문서는 오디오 스트리밍을 운영에 올리기 전에 지연과 음질 검증을 권장한다.
- [ ] AI agent event logs 켜기(CloudWatch Logs), 워크숍의 쿼리 5종 적용: collaborator-errors, slow-calls, tool-failures, one-exchange-conversations, guardrail-interventions
- [ ] 동시 외부 에이전트 연결 한도 상향 요청(기본 5)
- [ ] ECS 오토스케일: 태스크당 동시 통화 수 기준
- [ ] 키 교체 절차: 에이전트 시크릿과 Connect 시크릿을 함께 바꾸는 순서(새 키를 둘 다 허용 → 교체 → 이전 키 제거)
- [ ] `instanceArn` 허용 목록: 내 인스턴스가 아닌 연결은 거부
- [ ] 개인정보: trace에 대화 원문이 남는다. 보존 기간과 마스킹 정책을 정한다.
- [ ] 장애 대비: 흐름의 Error 분기 → 대기열 전환 또는 안내 멘트

## 7.3 비용 발생 항목

| 리소스 | 과금 |
|---|---|
| Connect | 음성 분당, AI 에이전트/A2A 사용량, 전화번호 일 단위 |
| ECS Express | Fargate 태스크 + ALB (서비스가 떠 있는 동안 계속) |
| Transcribe / Bedrock / Polly | 사용량 |
| KMS, Secrets Manager | 키/시크릿 월 단위 |
| CloudWatch Logs | 수집/보존 |

> 테스트하지 않을 때는 ECS 서비스를 지워 두는 게 가장 큰 절약이다.

## 7.4 정리 순서 (리전마다)

1. `register_audio.py --teardown --name voice --apply` (워크숍 `teardown.py` 대응): 흐름 → AI 에이전트 버전/에이전트 → 보안 프로필 → 연결 → A2A 앱 → Connect용 시크릿(즉시 삭제) → KMS 키(삭제 예약)
2. ECS 서비스 삭제 → `--status`가 `not found`가 될 때까지 대기 (ALB 정리)
3. ECR 이미지, 로그 그룹, 에이전트 키 시크릿
4. Lex 봇, assistant, 전화번호 반납
5. Connect 인스턴스 삭제 (S3 녹음/기록은 먼저 백업)
6. IAM 역할 (다른 리전에서 아직 쓰는지 확인)
