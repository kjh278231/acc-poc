# 06. Connect 등록, 통화 테스트, 트레이스

## 6.1 적합성 테스트 (등록 전 필수)

```bash
cd reference/a2a-tester
read -rs A2A_API_KEY; export A2A_API_KEY      # 에이전트 키 (화면에 표시되지 않음)
python3 -m a2a_tester --url "wss://$HOST/a2a" --key-env A2A_API_KEY --suite core --mode talk
python3 -m a2a_tester --url "wss://$HOST/a2a" --key-env A2A_API_KEY --suite all  --report report.json
```

- exit 0이면 통과, 1이면 error 수준 실패, 2면 사용법 또는 연결 오류다.
- **TRACE-* 실패는 반드시 고친다.** Connect가 엔드포인트를 비활성화한다.
- 통과해도 Connect가 받아 준다는 보장은 아니다. 실제 통화로 마무리한다.

## 6.2 등록 스크립트 `scripts/register_audio.py` (결정 D4)

워크숍 `register.py --mode handoff-audio`의 8단계를 그대로 하되, `event-stack` 출력 대신 `config/${A2A_REGION}.outputs.env`를 읽는다.
바꾸는 부분:
- `locale: "en_US"` 고정값 → `A2A_LOCALE`
- 시작 흐름 복제: 워크숍은 "Pat의 ARN과 일반 봇 별칭을 찾아 바꾸기"를 한다. 우리는 3.5의 템플릿 흐름에서 **자리표시자 ARN을 바꿔 넣는 방식**으로 한다.
- `REGION` 기본값 `us-east-1` → `A2A_REGION` 필수

| 단계 | API | 내용 |
|---|---|---|
| 1/8 KMS 키 | `kms:CreateKey`, `CreateAlias` | 2.4의 키 재사용 가능. Connect `kms:Decrypt` 정책 확인 |
| 2/8 시크릿 | `secretsmanager:CreateSecret`, `PutResourcePolicy` | `a2a-voice-connect-key`, CMK 암호화. 정책: `connect.amazonaws.com`에 `GetSecretValue`, 조건 `aws:SourceAccount`와 `aws:SourceArn = 인스턴스 ARN` |
| 3/8 A2A 앱 | `app-integrations:CreateApplication` | `ApplicationType: A2A_SERVER`(**생성 후 변경 불가**), `AccessUrl: wss://$HOST/a2a`, `AuthConfig`에 시크릿 ARN, 태그 `AmazonConnectEnabled=True` |
| 4/8 인스턴스 연결 | `connect:CreateIntegrationAssociation` | `IntegrationType: APPLICATION` |
| 5/8 보안 프로필 | `connect:CreateSecurityProfile` | `AllowedAIAgents: [{Arn: 앱 ARN, Type: THIRD_PARTY}]`, 채널 정보가 필요하면 `Permissions: [BasicAgentAccess]` |
| 6/8 orchestrator | `qconnect:CreateAIAgent` (ORCHESTRATION) | 아래 설정 참고 |
| 7/8 게시 | 보안 프로필 연결 + `qconnect:CreateAIAgentVersion` | 버전 없는 ARN과 **모든 게시 버전**에 보안 프로필을 연결 |
| 8/8 흐름 | `connect:CreateContactFlow` | 템플릿 복제 → `ai-agent-arn = <agent>:$LATEST`, Sonic 봇 |

orchestrator 설정 (워크숍 `agent_configuration()` 기준):

```json
{
  "orchestrationAIAgentConfiguration": {
    "connectInstanceArn": "<INSTANCE_ARN>",
    "locale": "<A2A_LOCALE>",
    "toolConfigurations": [
      {"toolName": "Complete", "toolType": "RETURN_TO_CONTROL",
       "description": "Close the conversation when the customer has no more questions",
       "instruction": {"instruction": "Mark the conversation complete when the customer is done."},
       "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
       "userInteractionConfiguration": {"isUserConfirmationRequired": false}}
    ],
    "multiAgentConfigurations": [{
      "handoffAgentConfiguration": {
        "agentTarget": {"applicationId": "<APP_ID>"},
        "instruction": {"instruction": "Hand off immediately. This agent handles the whole call in its own voice and finishes with COMPLETE when the call is over."},
        "audioStreamingEnabled": true,
        "immediateHandoff": true
      }
    }]
  }
}
```

> `orchestrationAIPromptId`와 guardrail은 넣지 않는다. 넣으면 검증에 실패한다.

실행 순서:
```bash
python3 scripts/register_audio.py --name voice --url "wss://$HOST/a2a" --key-from secret:a2a-voice-agent-key --dry-run
python3 scripts/register_audio.py --name voice --url "wss://$HOST/a2a" --key-from secret:a2a-voice-agent-key
```

필요한 IAM 권한(실행하는 사용자): `secretsmanager:CreateSecret/PutResourcePolicy`, `kms:CreateKey/CreateAlias`, `app-integrations:CreateApplication`, `connect:CreateIntegrationAssociation/CreateSecurityProfile/CreateContactFlow/...`, `qconnect:CreateAIAgent/CreateAIAgentVersion/...`, `iam:CreateRole/PassRole` 등. `[검증]` user-coco 권한은 dry-run으로 확인한다.

## 6.3 통화 테스트 시나리오

헤드셋을 끼고 웹콜(또는 전화)로 흐름 `a2a-voice-voice`에 건다.

| # | 시나리오 | 기대 결과 |
|---|---|---|
| T1 | 연결 | Connect 멘트 없이 바로 에이전트 인사 (연결까지 무음 구간 기록) |
| T2 | 일반 대화 2~3턴 | 끊김 없이 응답. 발화 끝에서 첫 음성까지 시간 기록 |
| T3 | 끼어들기 | 에이전트가 말할 때 끼어들면 1초 안에 멈춤 |
| T4 | 느린 도구 | 5초 이상 걸리는 도구에서도 통화가 유지됨(keepalive) |
| T5 | 정상 종료 | "끝났어요, 감사합니다" → 에이전트 작별 인사가 끝까지 재생된 뒤 COMPLETE → 흐름이 바로 연결 끊기(Connect 종료 멘트 없음) |
| T6 | 연결 오류 | ECS 서비스를 0개로 줄이고 건다 → Lex 블록 Error → "연결이 원활하지 않습니다" 멘트 후 끊기 |
| T7 | 재배포 | 통화 중 새 태스크로 교체 → 1012 → 재연결되어 통화 유지 |
| T8 | 동시 통화 | 2~5건 동시 → 모두 정상 (기본 한도 5) |
| T9 | 콜백 요청 | "사람이랑 통화하고 싶어요" → "담당자가 다시 연락드리겠습니다" → FINISH COMPLETE → 끊기. ECS 로그에 `callback request filed with TMS: {"status": "received", ...}` (`A2A_TMS_URL=mock`) |

## 6.4 트레이스 확인

```bash
cd reference/a2a-connect-setup/setup
export A2A_REGION=... A2A_EVENT_STACK=...   # 또는 스크립트가 outputs.env를 읽도록 수정
read -rp "Contact ID: " CONTACT_ID
python3 ../tools/list_spans_tree.py --contact-id "$CONTACT_ID" --messages
python3 ../tools/spans_insight.py   --contact-id "$CONTACT_ID"
```

- 즉시 오디오 핸드오프라 `invoke_collaborator` span이 없다. 에이전트의 `invoke_agent` span이 연락처 trace 바로 아래에 붙는다.
- span이 보이기까지 최대 1분 정도 걸린다.
- `list_spans_tree.py`와 `spans_insight.py`는 `load_event_stack()`에 의존하므로 env 기반으로 바꾸는 패치가 필요하다(P6-5).

## 완료 기준
- [ ] tester all 통과
- [ ] 등록 dry-run, 실제 실행 모두 성공(읽기 확인 포함)
- [ ] T1~T6, T9 통과, T7~T8은 운영 전까지
- [ ] trace에 턴별 입력/출력/도구 기록 확인
