# 03. Connect 쪽 구성 (리전마다 1회)

> 워크숍의 "A tour of your account"에 나오는 구성 중 오디오 핸드오프에 필요한 것만 직접 만든다.
> 데모 API, Pat(청구 에이전트), 참조 텍스트 에이전트처럼 오디오 핸드오프에 필요 없는 리소스는 만들지 않는다.

## 3.1 Connect Customer 인스턴스

- [ ] 인스턴스 생성: 별칭 `a2a-voice-<region-short>` (예: `a2a-voice-sel`, `a2a-voice-sin`)
  - ID 관리: Connect에 사용자 저장
  - 텔레포니: 인바운드와 아웃바운드 허용
  - 데이터 저장: 기본 S3 (통화 녹음, 채팅 기록)
- [ ] **Customer 티어인지 확인**한다. Customer Basic에서는 A2A를 쓸 수 없다. `[검증]` 티어 선택과 확인 위치는 2026년 요금 체계 기준으로 콘솔에서 확인한다.
- [ ] 관리자 사용자를 만들고 admin 웹사이트에 로그인한다.
- [ ] 서비스 할당량 확인:
  - 계정당 인스턴스 수
  - **동시 외부 에이전트 연결: 인스턴스당 5개(기본값, 조정 가능)**
  - 협업자 수: AI 에이전트당 5개

> 과금: 인스턴스 자체는 사용량 기반이다. 전화번호를 받으면 일 단위 요금이 붙는다.

## 3.2 AI agents (Connect assistant)

- [ ] Connect 콘솔 → 인스턴스 → AI agents → 도메인(assistant) 추가
  - 암호화: 고객 관리형 KMS 키 (2.4의 키를 재사용하거나 따로 생성)
  - 결과: qconnect assistant가 생기고, 인스턴스와 `WISDOM_ASSISTANT`로 연결된다.
- [ ] `ASSISTANT_ID`와 `ASSISTANT_ARN`을 기록한다.
- [ ] **지식 베이스 연결 필수** (CLI로 assistant만 만들면 빠짐. 콘솔 "도메인 추가"는 자동 생성). 없으면 Lex QInConnect 인텐트가
  `Could not find Active Association of type KNOWLEDGE_BASE`로 실패하고, 흐름에서는 LexRuntimeV2 404(ResourceNotFound)로 보인다(2026-11-19 서울에서 겪음).
  ```bash
  KB=$(aws qconnect create-knowledge-base --name a2a-voice-kb --knowledge-base-type CUSTOM --query knowledgeBase.knowledgeBaseId --output text)
  aws qconnect create-assistant-association --assistant-id $ASSISTANT_ID --association-type KNOWLEDGE_BASE --association knowledgeBaseId=$KB
  ```
- [ ] 흐름에서 `CreateWisdomSession` 다음에 반드시 `UpdateContactData`(`WisdomSessionArn = $.Wisdom.SessionArn`)를 둔다. 콘솔의 Connect assistant 블록은 이 둘을 묶은 것이다.
- 확인 명령:
  ```bash
  aws qconnect list-assistants --query 'assistantSummaries[].{id:assistantId,name:name}'
  aws connect list-integration-associations --instance-id $INSTANCE_ID --integration-type WISDOM_ASSISTANT
  ```

## 3.3 Lex V2 봇 (Nova Sonic S2S)

오디오 핸드오프 조건: **봇 locale이 Nova Sonic speech-to-speech이고, `AMAZON.QInConnectIntent` 하나만 있어야 한다.**
이 봇은 에이전트의 음성을 처리하지 않는다. 등록 조건을 맞추기 위해 둔다.

- [ ] Connect admin 웹사이트 → Conversational AI → Bots → 봇 생성: `a2a-voice-sonic`
- [ ] locale 추가: 기본 `ko_KR` (결정 D7)
  - 서울: 한국어 S2S 사용 사례가 확인됨(2026-04 업데이트)
  - 싱가포르: `[검증]` ko_KR S2S가 되는지 확인한다. 안 되면 `en_US` 또는 `en_SG`로 만들고, 에이전트가 한국어로 말하게 한다. 음성은 직접 에이전트로 가므로 영향은 오류 멘트 정도다.
- [ ] 인텐트: `AMAZON.QInConnectIntent` 하나만 두고 3.2의 assistant에 연결한다. 다른 인텐트는 지운다.
- [ ] Configuration → Speech model → **Speech-to-Speech: Amazon Nova Sonic** → Build language
- [ ] 별칭(alias)을 만들고 인스턴스에 연결한다. `SONIC_BOT_ALIAS_ARN`을 기록한다.
- [ ] (선택) 텍스트 핸드오프 비교 테스트용 일반 봇 `a2a-voice-standard`
  - 주의: Nova Sonic 봇에서 Connect 자체 AI 에이전트를 쓰면 Bedrock 424 오류가 난다. 일반 봇에서 오디오 핸드오프를 하면 Lex 단계가 바로 실패한다(워크숍 Troubleshooting). **봇과 모드를 섞지 않는다.**

## 3.4 큐와 운영 시간 (사용 안 함)

상담원 연결(Escalate) 경로를 두지 않는다. 통화는 처음부터 끝까지 ECS 에이전트가 처리하고, 흐름은 끝나면 끊기만 한다(2026-11-20 결정). 큐와 상담원 사용자는 만들지 않아도 된다.
고객이 사람을 요청하면 에이전트가 "담당자가 다시 연락드리겠습니다"라고 말하고 TMS에 콜백 요청을 남긴다(문서 04 종료 처리).

## 3.5 시작 흐름 `a2a-voice-audio` (최소 구성)

통화 중 대화는 Lex 블록 안에서 Connect ↔ ECS 웹소켓으로 오가고, 흐름은 그동안 Lex 블록에서 기다리기만 한다. 그래서 흐름에는 Lex에 붙기 위한 준비와 끝났을 때의 처리만 둔다.

```
[시작]
 → 로깅 동작 설정 (흐름 로그 켜기)
 → Connect assistant (3.2 assistant ARN)     ─ AI 세션 생성 + 세션 ARN을 연락처에 기록
 → 음성 설정: Seoyeon                         ─ 아래 오류 멘트용. Connect가 읽는 멘트가 없으면 빼도 된다
 → 연락처 속성 설정: LanguageCode = ko-KR      ─ Lex 봇 locale 선택. 없으면 en-US로 찾다가 404
 → 고객 입력 가져오기: Lex 봇 a2a-voice-sonic
      프롬프트 SSML <speak><break time="1ms"/></speak>   ─ 들리는 멘트 없음. 프롬프트가 필수라 무음으로 둔다
                                                            (2026-11-20 웹콜 확인: 멘트·오류 없음, 인사까지 무음 약 0.5초)
      세션 속성 x-amz-lex:q-in-connect:ai-agent-arn = <orchestrator ARN>:$LATEST
   ├─ Default → 연결 끊기          (에이전트가 FINISH를 보내면 여기로 나온다)
   └─ Error   → 프롬프트 "연결이 원활하지 않습니다. 잠시 후 다시 걸어 주세요." → 연결 끊기
```

| 블록 | 필요 여부 | 이유 |
|---|---|---|
| 로깅 동작 설정 | 선택 | 흐름 로그는 인스턴스 설정 `CONTACTFLOW_LOGS=true`(서울 확인됨)일 때만 `/aws/connect/<인스턴스 별칭>`에 남는다. Lex 블록에 들어간 뒤의 대화는 흐름 로그가 아니라 ECS 로그(`/a2a-voice/agent`)에 남는다 |
| Connect assistant | 필수 | 세션이 없으면 QInConnect 인텐트가 바로 실패해 ECS까지 가지 못한다 |
| 음성 설정 | 조건부 | Connect가 직접 읽는 오류 멘트에만 적용된다. 통화 본문은 ECS의 Polly 설정을 쓴다 |
| 연락처 속성 설정(ko-KR) | 필수 | 봇 alias에는 `ko_KR` locale만 있다 |
| 고객 입력 가져오기(Lex) | 필수 | 오디오 핸드오프 진입점 |

- [ ] orchestrator ARN은 등록 단계(문서 06)에서 정해진다.
- [ ] 콘솔에서 저장하면 Connect assistant 블록이 `CreateWisdomSession` + `UpdateContactData(WisdomSessionArn)`로 묶이고, 내용 없는 "연락처 속성 설정" 블록이 남을 수 있다. 빈 블록은 지운다.
- [ ] `register_audio.py`의 `flow_content()`가 위 구조를 그대로 만든다(오류 멘트 `--error-text`). 콘솔에서 흐름을 고쳤다면 스크립트에도 반영해야 다시 등록할 때 덮어쓰지 않는다.

## 3.6 테스트 진입점 (결정 D5)

| 방식 | 장점 | 준비 |
|---|---|---|
| **통신 위젯 웹콜 (추천)** | 전화번호 불필요, 브라우저+헤드셋으로 바로 테스트 | 채널 → Communications widget에서 음성(웹 통화) 켜기, 허용 도메인 등록, 흐름 `a2a-voice-<name>` 연결, 간단한 HTML 테스트 페이지 |
| 전화번호 | 실제 전화 품질로 테스트 | 번호 신청. 한국과 싱가포르 번호의 서류 요건은 `[검증]` |
| Test chat | 텍스트 경로만 확인 가능 | 오디오는 협상하지 않으므로 오디오 테스트에는 못 쓴다 |

> 워크숍도 헤드셋을 권장한다. 스피커폰이면 에이전트가 자기 목소리를 끼어들기로 인식한다.

## 3.7 출력값 기록

`config/${A2A_REGION}.outputs.env`에 추가한다.

```
A2A_INSTANCE_ID=
A2A_INSTANCE_ARN=
A2A_ASSISTANT_ID=
A2A_ASSISTANT_ARN=
A2A_SONIC_BOT_ALIAS_ARN=
A2A_START_FLOW_ID=        # 템플릿 흐름
A2A_ESCALATION_QUEUE_ARN=
A2A_WIDGET_ID=            # 웹콜 위젯을 쓸 때
```

## 완료 기준
- [ ] 인스턴스 Customer 티어 확인
- [ ] assistant 연결 확인
- [ ] Sonic 봇 빌드 성공, 인스턴스에 연결
- [ ] 시작 흐름 템플릿 저장
- [ ] 웹콜(또는 전화번호)로 걸어 Connect 멘트 없이 에이전트 인사까지 들리는지 확인
