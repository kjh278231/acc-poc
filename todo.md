# TODO: Connect Customer 음성 A2A (서울 / 싱가포르, Nova Sonic 우회)

> 설계 문서: [docs/00-overview.md](docs/00-overview.md)부터 읽기
> 표기: `SEL` = 서울 ap-northeast-2, `SIN` = 싱가포르 ap-southeast-1, `[검증]` = 구성하면서 확인할 항목, `($)` = 과금 시작
> 진행 원칙: **서울에서 P1~P7을 끝까지 통과한 뒤 싱가포르(P8)에 복제**한다.

## 진행 현황

| 단계 | 내용 | 문서 | SEL | SIN |
|---|---|---|---|---|
| P0 | 결정 사항 확정 | 00 | 진행 중 | (공통) |
| P1 | 리전 기반 (네트워크, IAM, KMS, ECR, 로그) | 02 | 완료 | ☐ |
| P2 | Connect 기반 (인스턴스, AI agents, Sonic 봇, 흐름, 웹콜) | 03 | 완료 | ☐ |
| P3 | 에이전트 로컬 개발: loopback + tester | 04 | 완료 | (공통) |
| P4 | ECS 배포 + 원격 tester | 05 | 완료 | ☐ |
| P5 | Connect 등록 + loopback 통화 | 06 | 완료 (11-19 21:18 웹콜: 인사+에코 확인) | ☐ |
| P6 | 우회 엔진 cascade-ko + 한국어 통화 | 04, 06 | v5 배포·원격 검증 완료, 사람 웹콜 검증 대기 | ☐ |
| P7 | 운영 점검 | 07 | ☐ | ☐ |
| P8 | 싱가포르 복제 | 07 | - | ☐ |
| P9 | 정리 | 07 | ☐ | ☐ |

---

## P0. 결정 사항 (docs/00 §5)

- [x] D1 음성 엔진: `cascade-ko` (Transcribe → Bedrock → Polly) 확정 (engines/cascade.py)
- [x] D2 첫 검증 엔진: `loopback` 확정 (P5 통과)
- [x] D3 LLM: `apac.amazon.nova-lite-v1:0` (APAC 내 처리, 첫 토큰 0.3~0.46s). Claude Haiku 4.5(global)는 Anthropic 사용 사례 양식 미제출로 막힘, 3.5 Sonnet v2는 EOL
- [x] D4 등록 방식: `register.py` 포크(`agent/audio-agent/register_audio.py`) 확정
- [x] D5 테스트 진입점: 통신 위젯 웹콜 (2026-11-19 확정)
- [x] D6 네트워크: 기본 VPC 생성 (vpc-0f13fc927bddabd67)
- [ ] D7 Lex locale: 서울 ko_KR / 싱가포르 id_ID (P8에서 재확인)
- [ ] D8 싱가포르 인도네시아어 TTS (Polly 미지원): 자체 TTS 모델 / 외부 TTS / handoff-text 중 선택
- [x] 워크숍 다운로드 4종을 `reference/`에 확보 (2026-11-19)

## P1. 리전 기반 (docs/02) — SEL / SIN 각각

- [ ] P1-1 `config/<region>.env` 작성
- [ ] P1-2 AWS CLI `CLI ready` 확인 (CloudShell 또는 로컬)
- [ ] P1-3 실행 사용자(user-coco) 권한 확인: Connect, qconnect, appintegrations, KMS, Secrets Manager, IAM 역할 생성/PassRole, ECS, ECR `[검증]`
- [ ] P1-4 네트워크: 2개 AZ 이상 퍼블릭 서브넷 확보
  - SEL 현재: 기본 VPC 없음, 서브넷 1개(2d) → 보완 필요
- [ ] P1-5 KMS CMK `alias/a2a-voice-connect` (Connect Decrypt 정책) `($)`
- [ ] P1-6 IAM: `a2a-voice-ecs-exec`, `a2a-voice-ecs-infra`, `a2a-voice-task-<region>` (+ 선택 권한 경계)
- [ ] P1-7 로그 그룹 `/a2a-voice/agent`, ECR `a2a-voice-agent`
- [ ] P1-8 에이전트 키 시크릿 `a2a-voice-agent-key` `($)`
- [ ] P1-9 `config/<region>.outputs.env` 기록
- [ ] P1-10 Bedrock LLM 후보 모델의 리전 사용 가능 여부 확인 `[검증]`

## P2. Connect 기반 (docs/03) — SEL / SIN 각각

- [ ] P2-1 Connect 인스턴스 생성 `($)`, **Customer 티어 확인** `[검증: 티어 확인 위치]`
- [ ] P2-2 관리자 로그인, 할당량 확인 (동시 외부 에이전트 연결 5)
- [ ] P2-3 AI agents 도메인(assistant) 생성 + 인스턴스 연결 확인
- [ ] P2-4 Lex 봇 `a2a-voice-sonic`: locale, `AMAZON.QInConnectIntent` 하나, Speech-to-Speech Nova Sonic, 빌드, 별칭, 인스턴스 연결
- [ ] P2-5 큐/운영 시간/테스트 상담원 준비 (Escalate 경로)
- [ ] P2-6 시작 흐름 템플릿 `a2a-voice-start` 작성
  - [ ] Complete/Escalate 분기 속성 확인 `[검증]`
  - [ ] 한국어 Nova Sonic 호환 음성 확인 `[검증]`
  - [ ] Lex 에스컬레이션 속성 복사 단계 넣지 않기
- [ ] P2-7 테스트 진입점: 통신 위젯 웹콜 설정 + 테스트 HTML 페이지 (또는 전화번호 `($)`)
- [ ] P2-8 웹콜로 안내 멘트까지 들리는지 확인

## P3. 에이전트 로컬 개발 (docs/04) — 공통 코드

- [ ] P3-1 `agent/` 뼈대: 워크숍 `a2a-audio-bridge/audio-agent/bridge`에서 server/a2a/tracing/bearer/secrets/audio 가져오기
- [ ] P3-2 `SpeechEngine` 인터페이스 정의 및 server.py를 엔진 이벤트 기반으로 분리 (nova.py 직접 의존 제거)
- [ ] P3-3 `engines/loopback.py`: 인사말 재생, 에너지 VAD, 에코, 끼어들기, DTMF/턴 수 기반 FINISH
- [ ] P3-4 INIT_SESSION_RESPONSE: 입력은 수락, 출력 16000Hz 역제안
- [ ] P3-5 keepalive(3초 → 100ms 무음), 1012 종료, contextId 기반 상태 보관
- [ ] P3-6 TRACING_SPAN: 루트 span에 입력/출력 메시지 이벤트, traceparent/tracestate 반영
- [ ] P3-7 로컬 tester: `--suite core` 통과
- [ ] P3-8 로컬 tester: `--suite all` 통과 (오디오, 끼어들기, 무음, 재연결)
- [ ] P3-9 Dockerfile, 로컬 컨테이너 실행 확인

## P4. ECS 배포 (docs/05) — SEL / SIN 각각

- [ ] P4-1 `scripts/deploy_agent.py`: `deploy_lab.py` 포크, outputs.env 기반, 리전 하드코딩 제거
- [ ] P4-2 이미지 빌드/푸시 (`v1`)
- [ ] P4-3 Express 서비스 생성 `($)` (`A2A_ENGINE=loopback`)
- [ ] P4-4 `--status` ready, `/health` 200, `/a2a` 키 없이 401
- [ ] P4-5 원격 tester `wss://$HOST/a2a` core + all 통과
- [ ] P4-6 로그에 키나 Authorization 헤더가 없는지 확인

## P5. Connect 등록 + loopback 통화 (docs/06) — SEL / SIN 각각

- [ ] P5-1 `scripts/register_audio.py`: `register.py` 포크 (locale 변수화, 템플릿 흐름 치환, outputs.env)
- [ ] P5-2 `--dry-run` 통과 (권한 문제도 여기서 확인)
- [ ] P5-3 실제 등록 8단계 + 각 단계 읽기 확인
- [x] P5-4 웹콜 T1: 안내 멘트 → 에이전트 인사(목소리 전환) 확인 (21:18 통화, 인사 + 에코 1턴)
- [ ] P5-5 T3 끼어들기, T5 COMPLETE, T6 ESCALATE (loopback 기준)
- [ ] P5-6 contact trace 확인 (`list_spans_tree.py` / `spans_insight.py` env 기반 패치)

## P6. 우회 엔진 cascade-ko (docs/04 §4.4) — 코드 공통, 검증은 리전별

- [x] P6-1 Transcribe Streaming ko-KR 연동 (engines/transcribe_ws.py: aiohttp WebSocket + SigV4, 의존성 추가 없음. 발화 끝→final 약 0.65s)
- [x] P6-2 Bedrock ConverseStream 연동 + 문장 단위 분할 (첫 조각은 쉼표에서도 끊음, <thinking> 제거)
- [x] P6-3 Polly neural pcm 16k (Seoyeon) 연동, 100ms 청크, 응답 스트림 순차 읽기
- [x] P6-4 끼어들기: partial 2글자 또는 VAD 400ms → INTERRUPTION + LLM/TTS 취소 (CloudShell 실시간 테스트 통과)
- [x] P6-5 도구 + `end_call`(COMPLETE/ESCALATE) + ESCALATE 시 `UpdateContactAttributes` (v5 원격 실시간 테스트로 COMPLETE, ESCALATE 모두 확인. temperature 0 + topK 1, 페르소나: ESCALATE는 명시적 상담원 요청일 때만, <thinking> 금지. 도구 판단 프로브 14/14)
- [x] P6-6 trace: chat / execute_tool span (TRACING_SPAN에 chat, end_call 턴에 execute_tool 포함 확인)
- [ ] P6-7 로컬 tester all 재통과 (대신 tests/live_cascade_client.py 실시간 시나리오 통과: 질문 2회, 끼어들기, 종료 FINISH COMPLETE)
- [ ] P6-8 `v5` 배포 완료(태스크 정의 :6, A2A_ENGINE=cascade, 원격 wss 실시간 테스트 COMPLETE/ESCALATE 통과), 사람이 거는 웹콜 T1~T6 한국어 재검증 대기
- [ ] P6-9 지연 측정 (발화 끝 → 첫 음성, 목표 1.5초 이내) 및 턴 종료 타이머 조정. v5 원격 측정(고객 발화 끝 → 첫 음성): 1.09~1.68s (STT final ~0.65s + 타이머 0.15s + LLM 첫 토큰 ~0.3s + 첫 조각 TTS). 남은 개선안: VAD 무음 기반 추측 LLM 시작
- [ ] P6-10 (선택) `nova-tokyo` 엔진으로 영어 비교 측정

## P7. 운영 점검 (docs/07 §7.2)

- [ ] P7-1 T4 느린 도구, T7 재배포 1012 재연결, T8 동시 통화
- [ ] P7-2 AI agent event logs + 쿼리 5종
- [ ] P7-3 동시 연결 한도 상향 요청
- [ ] P7-4 키 교체 절차, instanceArn 허용 목록
- [ ] P7-5 trace 개인정보 보존 정책
- [ ] P7-6 AWS 계정팀/SA 오디오 스트리밍 검토 요청

## P8. 싱가포르 복제 (docs/07 §7.1)

- [ ] P8-1 `config/ap-southeast-1.env` 작성 후 P1 → P2 → P4 → P5 → P6-8 반복
- [ ] P8-2 Lex S2S id_ID 가능 여부 `[검증]` (불가 시 D7 대안)
- [ ] P8-3 LLM 모델/프로필, 호환 음성 차이 반영
- [ ] P8-3a STT `id-ID` (Transcribe 스트리밍) 연동
- [ ] P8-3b D8에 따른 인도네시아어 TTS 엔진 구현 및 지연/품질 측정
- [ ] P8-4 서울과 같은 T1~T8 결과표 작성

## P9. 정리 (테스트를 멈출 때마다)

- [ ] P9-1 ECS 서비스 삭제 → `not found`까지 대기
- [ ] P9-2 (완전 정리 시) 등록 해제 → 봇/assistant → 번호 반납 → 인스턴스 → IAM

---

## 열린 질문 (`[검증]` 모음)

1. 인스턴스 Customer 티어 선택/확인 위치 (P2-1)
2. 시작 흐름의 Complete/Escalate 분기 키 (P2-6)
3. 한국어 Nova Sonic 호환 음성과 싱가포르 ko_KR S2S 지원 (P2-6, P8-2)
4. Express Mode에서 기본이 아닌 VPC/서브넷 지정 방법 (P1-4)
5. Express 인프라 역할 관리형 정책 정확한 이름 (P1-6)
6. 리전별 Bedrock LLM 모델/추론 프로필 (P1-10)
7. user-coco IAM 권한 범위 (P1-3)
8. 한국/싱가포르 전화번호 요건 (P2-7, 웹콜이면 생략)
