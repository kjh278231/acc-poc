# 02. 리전 기반 구성 (리전마다 1회)

> 서울과 싱가포르에서 각각 실행한다. 모든 명령은 `A2A_REGION`만 바꿔서 그대로 쓴다.
> 워크숍에서 `event-stack`이 대신 만들어 주던 것들을 여기서 직접 만든다.

```bash
source config/${A2A_REGION}.env
export AWS_REGION=$A2A_REGION AWS_DEFAULT_REGION=$A2A_REGION
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
```

## 2.1 현재 상태 점검

| 항목 | 서울 (2026-11-19 확인) | 싱가포르 |
|---|---|---|
| Connect 인스턴스 | 없음 | 미확인 |
| ECS 클러스터 | 없음 | 미확인 |
| 기본 VPC | **없음** (`vpc-0b6a6fc27137459de` 192.168.0.0/16은 기본 VPC 아님) | 미확인 |
| 서브넷 | 1개 (ap-northeast-2d, 퍼블릭) | 미확인 |
| IGW | `osrm-poc-igw` 연결됨 | 미확인 |
| `ecsTaskExecutionRole` | 없음 (IAM은 글로벌) | 동일 |

## 2.2 AWS CLI 버전 확인

워크숍 "Your shell" 페이지의 확인 명령이다. `CLI ready`가 나와야 qconnect A2A 관련 형식을 쓸 수 있다.

```bash
aws qconnect create-ai-agent --generate-cli-skeleton yaml-input | grep -q multiAgentConfigurations \
  && aws appintegrations create-application --generate-cli-skeleton yaml-input | grep -q A2A_SERVER \
  && aws connect create-security-profile --generate-cli-skeleton yaml-input | grep -q AllowedAIAgents \
  && aws qconnect list-spans --generate-cli-skeleton yaml-input | grep -q assistantId \
  && echo "CLI ready"
```

아무것도 출력되지 않으면 AWS CLI v2를 최신으로 올린다. 워크숍은 2.39.3을 홈 디렉터리에 설치했다.

## 2.3 네트워크 (결정 D6)

ECS Express Mode는 기본적으로 **기본 VPC의 퍼블릭 서브넷**에 서비스를 올린다. 워크숍 조건은 "2개 AZ 이상의 퍼블릭 서브넷이 있는 기본 VPC"다.

- **추천안 A**: 기본 VPC 생성. 이미 있으면 생략한다.
  ```bash
  aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[].VpcId'
  aws ec2 create-default-vpc   # 없을 때만
  ```
- 대안 B: 기존 VPC에 다른 AZ의 퍼블릭 서브넷을 추가하고, Express Mode 서비스 생성 시 `networkConfiguration`으로 서브넷과 보안 그룹을 지정한다. `[검증]` 파라미터 이름과 동작을 확인해야 한다.
- 보안 그룹: ALB 인바운드 443은 0.0.0.0/0. Connect는 공용 인터넷으로 접속하고, 접근 제어는 Bearer 키로 한다.

## 2.4 KMS 키 (Connect가 시크릿을 읽기 위한 키)

AWS 관리형 키(`aws/secretsmanager`)로 암호화한 시크릿은 **Connect가 읽지 못한다**(워크숍 Troubleshooting). 고객 관리형 키를 만든다.

- 별칭: `alias/a2a-voice-connect`
- 키 정책 (워크숍 `register.py`의 `key_policy()`와 같음):
  - `EnableAccountAdministration`: `arn:aws:iam::${ACCOUNT}:root` → `kms:*`
  - `AllowConnectToDecryptSecret`: `connect.amazonaws.com` → `kms:Decrypt`, 조건 `kms:ViaService = secretsmanager.${A2A_REGION}.amazonaws.com`

> 등록 스크립트가 이 키를 자동으로 만들게 할 수도 있다(문서 06). 여기서는 리전 기반 리소스로 미리 만들어 둔다.

## 2.5 IAM 역할

IAM은 글로벌이다. 이름에 리전을 넣어 두 리전이 충돌하지 않게 한다.

| 역할 | 신뢰 주체 | 권한 | 비고 |
|---|---|---|---|
| `a2a-voice-ecs-exec` | `ecs-tasks.amazonaws.com` | `AmazonECSTaskExecutionRolePolicy` | 이미지 pull, 로그. 리전 공용 가능 |
| `a2a-voice-ecs-infra` | `ecs.amazonaws.com` | `AmazonECSInfrastructureRoleforExpressGatewayServices` | Express Mode가 ALB, 인증서 등을 만듦 `[검증: 정책 이름]` |
| `a2a-voice-task-${A2A_REGION}` | `ecs-tasks.amazonaws.com` (조건 `aws:SourceAccount`) | 아래 인라인 정책 | 컨테이너 권한 |
| (선택) 권한 경계 `a2a-voice-boundary` | - | 태스크 역할 최대 범위 | 워크숍의 `PermissionsBoundaryArn`에 해당 |

태스크 역할 인라인 정책 (엔진 `cascade-ko` 기준):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ReadOwnSecrets", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
     "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:a2a-voice-agent-key-*"},
    {"Sid": "Stt", "Effect": "Allow",
     "Action": ["transcribe:StartStreamTranscription", "transcribe:StartStreamTranscriptionWebSocket"],
     "Resource": "*"},
    {"Sid": "Llm", "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:REGION:ACCOUNT:inference-profile/*"]},
    {"Sid": "Tts", "Effect": "Allow", "Action": "polly:SynthesizeSpeech", "Resource": "*"},
    {"Sid": "CallerNumber", "Effect": "Allow", "Action": "connect:DescribeContact",
     "Resource": "arn:aws:connect:REGION:ACCOUNT:instance/INSTANCE_ID/contact/*"}
  ]
}
```

> 엔진을 `nova-tokyo`로 쓸 때만 `bedrock:InvokeModel` on `arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.nova-2-5-sonic`을 추가한다.
> Bedrock 리소스 범위는 모델이 정해지면 좁힌다.

## 2.6 로그, ECR, 시크릿

| 리소스 | 이름 | 설정 |
|---|---|---|
| CloudWatch 로그 그룹 | `/a2a-voice/agent` | 보존 14일. **API 키와 Authorization 헤더는 절대 기록하지 않는다** |
| ECR 리포지토리 | `a2a-voice-agent` | 태그 변경 불가(immutable), 푸시 시 스캔 |
| 에이전트 API 키 시크릿 | `a2a-voice-agent-key` | `token_urlsafe(36)`로 생성. 에이전트가 Bearer 비교에 사용 |

> Connect용 시크릿(`a2a-voice-connect-key`, CMK 암호화 + 리소스 정책)은 등록 단계(문서 06)에서 만든다.
> 같은 키 값이 시크릿 두 개에 들어간다. 하나는 에이전트용, 하나는 Connect용이다. 워크숍과 같은 방식이다.

## 2.7 기반 출력값 기록

이후 단계가 읽을 수 있게 `config/${A2A_REGION}.outputs.env`에 적어 둔다. CloudFormation으로 만들면 스택 출력으로 대신한다.

```
A2A_ACCOUNT_ID=
A2A_KMS_KEY_ARN=
A2A_ECS_EXEC_ROLE_ARN=
A2A_ECS_INFRA_ROLE_ARN=
A2A_TASK_ROLE_ARN=
A2A_LOG_GROUP=/a2a-voice/agent
A2A_ECR_URI=
A2A_AGENT_KEY_SECRET_ARN=
A2A_SUBNET_IDS=       # 대안 B일 때
A2A_SECURITY_GROUP_ID=
```

## 완료 기준
- [ ] `CLI ready` 출력
- [ ] 퍼블릭 서브넷 2개 AZ 이상 확보
- [ ] KMS CMK, IAM 역할 3개, 로그 그룹, ECR, 에이전트 키 시크릿 생성
- [ ] outputs 파일 작성
