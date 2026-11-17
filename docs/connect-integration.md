# Amazon Connect Customer 연동 가이드 (A2A)

> 상태: **로컬 프로토콜 검증 완료, 실제 Amazon Connect와의 통합 테스트는 미수행.**
> 이 문서의 "검증됨"은 우리 서버 ↔ 로컬 Mock Connect 클라이언트(`app/adapters/mock_connect.py`) 기준이다.
> AWS 연동 완료를 주장하려면 아래 [Phase 2 체크리스트](#phase-2-체크리스트)를 실제 인스턴스에서 수행해야 한다.

참고 문서 (2026-11-17 확인):
[A2A protocol contract](https://docs.aws.amazon.com/connect/latest/devguide/a2a-developer-guide.html) ·
[Sample events](https://docs.aws.amazon.com/connect/latest/devguide/a2a-sample-events.html) ·
[Build an external AI agent server](https://docs.aws.amazon.com/connect/latest/adminguide/a2a-external-server.html) ·
[Set up an external AI agent integration](https://docs.aws.amazon.com/connect/latest/adminguide/a2a-setup-external.html)

## 1. 엔드포인트

| 경로 | 모드 | 전송 |
|---|---|---|
| `POST /a2a` | Talk to the customer (`handoffAgentConfiguration`) | HTTPS JSON-RPC (`SendMessage`, `GetTask`, `CancelTask`, `SendStreamingMessage`=SSE) |
| `WS /a2a/ws` | Talk to the customer | WebSocket, 이벤트마다 JSON-RPC result 프레임 push (음성 스트리밍은 이것만 가능) |
| `POST /a2a/delegate` | Behind the scenes (`delegateAgentConfiguration`) | HTTPS |
| `WS /a2a/delegate/ws` | Behind the scenes | WebSocket |
| `GET /.well-known/agent-card.json` | A2A Agent Card (발견용. Connect 등록은 AppIntegrations `AccessUrl`을 쓰며 카드는 읽지 않음) | |

모드는 경로로 구분한다. Connect에서는 모드마다 별도의 AppIntegrations 애플리케이션(`AccessUrl`)으로 등록한다.

## 2. 프로토콜 매핑 (구현 상태)

| Connect 요구사항 | 구현 | 비고 |
|---|---|---|
| `Authorization: Bearer <apiKey>` 매 요청/업그레이드 검사 | ✅ `A2A_API_KEY`, 불일치 시 HTTP 401 / WS close 1008 | WS는 Starlette 특성상 업그레이드 전 401 대신 accept 전 close. 실제 Connect가 이 경우를 어떻게 처리하는지 Phase 2 확인 필요 |
| `A2A-Extensions` 헤더 echo | ✅ HTTP 응답 헤더, WS accept 헤더 | |
| 이벤트 타입 = metadata `<EXT>/eventType` | ✅ | |
| `INIT_SESSION` → `INIT_SESSION_RESPONSE` (요청 id echo) | ✅ `statusCode: "200"` | 같은 `contextId`로 재전송되면 상태를 유지하고 채널 상태만 재설정 (voice 재연결, chat 메시지별 연결) |
| `supportedFinishTypes` | ✅ 목록에 없는 FINISH는 보내지 않음 (ESCALATE 미지원 시 COMPLETE + 사유) | |
| `history` | ✅ 새 contextId일 때 대화 이력으로 적재 | 이력으로 슬롯을 재구성하지는 않음(서버 측 상태가 정본) |
| 오디오 포맷 협상 | ✅ 8k/16k/24k 이외 제안 시 16k로 역제안 (전체 재선언) | 오디오 자체는 Phase 3 |
| `CHANNEL_STATE READY` 게이트 | ✅ READY 전 턴은 최대 5초 대기 후 오류. READY 수신 시 아웃바운드 인사를 먼저 발화 (`speak_first`) | 재연결 시에는 다시 인사하지 않음 |
| 응답 artifact 스트리밍 | ✅ 문장 단위 `TEXT_RESPONSE_CHUNK`, 첫 청크 `append:false`, 이후 `true`, 마지막 `lastChunk:true` | 1청크 lookahead로 마지막 청크에 lastChunk 표시 |
| 두 번째 메시지는 새 artifact | ✅ (TMS 실패 안내) | |
| 턴 종료 = terminal `statusUpdate` `TASK_STATE_COMPLETED` | ✅ | |
| `FINISH` (COMPLETE / ESCALATE + reason) | ✅ terminal statusUpdate의 `status.message` data part | FINISH 후 해당 세션 메시지 거부 |
| 구조화 오류 `TASK_STATE_FAILED` + `error{errorCode, errorReason, errorCategory}` | ✅ 턴 타임아웃 408/TIMEOUT, 내부 오류 500/INTERNAL_ERROR | FAILED는 협업 종료이므로 모델 오류는 실패로 올리지 않고 템플릿 재질문으로 흡수 |
| JSON-RPC 오류 + `google.rpc.ErrorInfo` | ✅ 알 수 없는 메서드, INIT 전 메시지, 파싱 오류 | |
| `INTERRUPTION` (USER_AUDIO_INPUT / DTMF_INPUT) | ✅ 진행 중 턴 취소, 열린 artifact에 lastChunk 안 보냄, 턴은 COMPLETED로 닫음 | 새 고객 턴이 진행 중 턴에 겹쳐도 같은 처리 |
| `DTMF_INPUT_COMPLETE` | ✅ 숫자를 "N분"으로 해석 | 업무 정책 확정 필요 |
| `<EMPTY_USER_INPUT>` (침묵) | ✅ 같은 항목 재질문 (재질문 한도에 포함) | |
| 정보성 `ERROR` 수신 | ✅ 로그만 남김 (턴 경계 아님) | |
| `FRAGMENT` 재조립 | ✅ 수신 재조립 | 송신 분할은 미구현 (현재 프레임이 작음) |
| `TRACING_SPAN` 매 턴, 응답 후·종료 전 | ✅ OTLP/JSON 1개 artifact, `invoke_agent` 루트 + `chat <model>` + `execute_tool <tool>`, `traceparent` 전파, `aws.connect.span.message` input/output 이벤트 | OpenTelemetry SDK 대신 직접 직렬화. **Connect의 trace 강제 규칙(필수 필드)을 실제 환경에서 검증해야 함** — 미충족 시 엔드포인트가 비활성화됨 |
| HTTPS에서 task 반환 후 `GetTask` 폴링 | ✅ 턴 결과를 완료된 Task로 동기 반환, `GetTask`로 재조회 가능 | 긴 턴을 비동기(WORKING)로 반환하는 모드는 미구현 |
| `CancelTask` | ✅ `TASK_STATE_CANCELED` | Connect가 실제로 호출하는지 미확인 |
| Behind-the-scenes 결과 | ✅ text part (`next_action=…; collected=…; suggested_reply=…`) + data part(JSON) | Connect AI agent의 instruction에 결과 형식 설명 필요 |
| WS 재연결 close code | ⚠ 서버 재시작 시 1012로 닫아야 Connect가 재연결 | uvicorn 기본 종료는 1012를 보내지 않음 → 배포 시 graceful shutdown 훅 필요 |
| 오디오 입출력 (`audio/lpcm` raw parts, `AUDIO_RESPONSE_CHUNK`) | ❌ Phase 3 | 현재 오디오 part 수신 시 JSON-RPC 오류 |

## 3. 지연시간 계측 포인트

| 구간 | 측정 위치 | 상태 |
|---|---|---|
| Connect → 서버 네트워크 | `traceparent` 부모 스팬 시작(Connect) vs 우리 `invoke_agent` 시작 | Phase 2 (Connect 측 trace 필요) |
| A2A 세션 초기화 | Mock 클라이언트 `init_ms`, 서버 `A2ASession.timings.init_ms` | ✅ 로컬 |
| 모델 판단 | `chat <model>` 스팬, 턴 지표 `decision_ms`, `ttft_ms` | ✅ |
| 도구 호출 | `execute_tool` 스팬, `tool_ms` | ✅ |
| 첫 응답 전송 | `first_response_ready_ms` (서버), `first_chunk_ms` (클라이언트) | ✅ |
| A2A → Connect 전달 | 클라이언트 관측 턴 시간 − 서버 `invoke_agent` 스팬 길이 = A2A 오버헤드 | ✅ 로컬, WAN은 Phase 2 |

## 4. AWS 설정 절차 (Phase 2)

1. **공개 TLS 엔드포인트** 준비: 예) ALB(+ACM 인증서) → ECS/Fargate, 또는 Bedrock AgentCore. WS idle timeout을 통화 길이 이상으로.
2. **API 키 저장**: 고객 관리형 KMS 키(키 정책에 `connect.amazonaws.com` `kms:Decrypt`, `kms:ViaService=secretsmanager.<region>.amazonaws.com`) → Secrets Manager 시크릿 → 리소스 정책(`secretsmanager:GetSecretValue`, `aws:SourceAccount`, `aws:SourceArn`=Connect 인스턴스). 같은 값을 서버의 `A2A_API_KEY`로.
3. **애플리케이션 등록** (모드별 하나씩):
   ```bash
   aws appintegrations create-application --name eta-check-agent --namespace eta-check \
     --application-source-config '{"ExternalUrlConfig":{"AccessUrl":"wss://agent.example.com/a2a/ws"}}' \
     --application-type A2A_SERVER \
     --auth-config '{"AuthType":"API_KEY","CredentialProviderIdentifier":"<SECRET_ARN>"}' --region <REGION>
   ```
   behind-the-scenes용은 `AccessUrl`을 `https://agent.example.com/a2a/delegate`로.
4. `aws connect create-integration-association --integration-type APPLICATION --integration-arn <APP_ARN>`
5. **Orchestrator AI agent 생성** (`aws qconnect create-ai-agent`):
   - 텍스트 handoff: `handoffAgentConfiguration { agentTarget.applicationId, instruction, audioStreamingEnabled:false, immediateHandoff:true }`
     (아웃바운드 ETA 확인 콜은 첫 턴부터 우리 에이전트가 말하므로 `immediateHandoff:true` 권장)
   - behind-the-scenes: `delegateAgentConfiguration { agentTarget, instruction }` — instruction에 입력(기사 발화 전달)과 결과 형식(`next_action`, `collected`)을 명시.
6. **보안 프로필**: `update-security-profile --allowed-ai-agents '[{"Arn":"<APP_ARN>","Type":"THIRD_PARTY"}]'`, 채널 정보를 받으려면 *Access Contact Control Panel* 권한 추가. `associate-security-profiles`를 버전 없는/있는 agent ARN 각각에.
7. **Lex V2 봇**: `AMAZON.QInConnectIntent` 포함 봇을 인스턴스에 연결 (오디오 스트리밍 시 Nova Sonic S2S 로케일, 단일 인텐트).
8. `aws qconnect create-ai-agent-version`으로 게시.
9. **아웃바운드 콜 플로우**: 기사에게 거는 outbound campaign / `StartOutboundVoiceContact` → 플로우에서 Connect AI agent 호출 → Complete/Escalate 분기(Escalate → 배차 담당 큐). 이 플로우 구성은 이 PoC 범위 밖.

## 5. 미구현·확인 필요 사항

- 실제 Connect와의 메시지 교환 전부 (특히 HTTPS에서 CHANNEL_STATE 응답 형태, 빈 ack 메시지 허용 여부, trace 강제 규칙 충족 여부).
- 세션 상태 외부 저장 (다중 인스턴스 / chat 메시지별 연결 / 재연결이 다른 인스턴스로 갈 때).
- 송신 FRAGMENT, 비동기 Task(WORKING → GetTask 폴링) 모드.
- 서버 재시작 시 WS close 1012.
- 오디오 스트리밍 (docs/voice-design.md).
- 운영 보안: 키 로테이션(Secrets Manager 다중 키 허용), rate limit(429), 요청 크기 제한.
- 한국어 리전/지원 여부와 음성(Connect TTS/STT) 품질은 Phase 2에서 확인.

## Phase 2 체크리스트

- [ ] HTTPS 모드 텍스트 handoff: INIT → READY → 3턴 → FINISH COMPLETE, Connect 콘솔/로그에서 확인
- [ ] WSS 모드 voice(Connect STT/TTS) handoff, 끼어들기 시 INTERRUPTION 수신·처리 확인
- [ ] ESCALATE → 플로우 Escalate 분기 → 큐 전환
- [ ] behind-the-scenes 호출 결과를 Connect AI agent가 사용하는지
- [ ] TRACING_SPAN이 Connect observability에 보이고 엔드포인트가 비활성화되지 않는지
- [ ] `traceparent` 기반 Connect↔서버 네트워크 지연, Connect 측 처리 시간 측정
- [ ] 재연결(서버 1012 종료) 시 새 INIT_SESSION 수신, 상태 유지
- [ ] A/B 엔진을 `A2A_ENGINE`으로 바꿔 통합 환경 응답속도 비교
