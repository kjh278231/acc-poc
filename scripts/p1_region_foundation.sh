#!/usr/bin/env bash
# P1 리전 기반 리소스 (멱등). 사용: A2A_REGION=ap-northeast-2 bash p1_region_foundation.sh
# 만드는 것: 기본 VPC(없으면), KMS CMK(alias/a2a-voice-connect), IAM 역할 3개, 로그 그룹, ECR, 에이전트 키 시크릿
set -u
R=${A2A_REGION:?A2A_REGION 필요}
export AWS_REGION=$R AWS_DEFAULT_REGION=$R
ACC=$(aws sts get-caller-identity --query Account --output text)
OUT=~/a2a-voice/$R.outputs.env; mkdir -p ~/a2a-voice
touch $OUT
# upsert: 기존 값(P2 이후 항목 포함)을 지우지 않고 키만 갱신
kv(){ grep -v "^$1=" $OUT > $OUT.tmp; echo "$1=$2" >> $OUT.tmp; mv $OUT.tmp $OUT; }
kv A2A_REGION $R; kv A2A_ACCOUNT_ID $ACC

# 기본 VPC
DVPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
if [ "$DVPC" = "None" ]; then DVPC=$(aws ec2 create-default-vpc --query Vpc.VpcId --output text); fi
kv A2A_DEFAULT_VPC "$DVPC"

# KMS (Connect가 시크릿을 복호화할 수 있는 CMK)
KEY=$(aws kms describe-key --key-id alias/a2a-voice-connect --query KeyMetadata.Arn --output text 2>/dev/null)
if [ -z "$KEY" ] || [ "$KEY" = "None" ]; then
cat > /tmp/kp.json <<J
{"Version":"2012-10-17","Statement":[
{"Sid":"EnableAccountAdministration","Effect":"Allow","Principal":{"AWS":"arn:aws:iam::$ACC:root"},"Action":"kms:*","Resource":"*"},
{"Sid":"AllowConnectToDecryptSecret","Effect":"Allow","Principal":{"Service":"connect.amazonaws.com"},"Action":"kms:Decrypt","Resource":"*","Condition":{"StringEquals":{"kms:ViaService":"secretsmanager.$R.amazonaws.com"}}}]}
J
KEY=$(aws kms create-key --description "a2a-voice: Connect reads the A2A key secret" --policy file:///tmp/kp.json --tags TagKey=project,TagValue=a2a-voice --query KeyMetadata.Arn --output text)
aws kms create-alias --alias-name alias/a2a-voice-connect --target-key-id $KEY
fi
kv A2A_KMS_KEY_ARN "$KEY"

# IAM (글로벌: exec/infra는 공용, task는 리전별)
T='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"'$ACC'"}}}]}'
TI='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
mkrole(){ aws iam get-role --role-name $1 >/dev/null 2>&1 || aws iam create-role --role-name $1 --assume-role-policy-document "$2" --tags Key=project,Value=a2a-voice >/dev/null; }
mkrole a2a-voice-ecs-exec "$T";  aws iam attach-role-policy --role-name a2a-voice-ecs-exec  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
mkrole a2a-voice-ecs-infra "$TI"; aws iam attach-role-policy --role-name a2a-voice-ecs-infra --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
mkrole a2a-voice-task-$R "$T"
cat > /tmp/tp.json <<J
{"Version":"2012-10-17","Statement":[
{"Sid":"ReadOwnSecrets","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"arn:aws:secretsmanager:$R:$ACC:secret:a2a-voice-*"},
{"Sid":"Stt","Effect":"Allow","Action":["transcribe:StartStreamTranscription","transcribe:StartStreamTranscriptionWebSocket"],"Resource":"*"},
{"Sid":"Llm","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":["arn:aws:bedrock:*::foundation-model/*","arn:aws:bedrock:*:$ACC:inference-profile/*"]},
{"Sid":"Tts","Effect":"Allow","Action":"polly:SynthesizeSpeech","Resource":"*"},
{"Sid":"CallerNumber","Effect":"Allow","Action":"connect:DescribeContact","Resource":"arn:aws:connect:$R:$ACC:instance/*/contact/*"},
{"Sid":"TestPageWebCall","Effect":"Allow","Action":["connect:StartWebRTCContact","connect:StopContact"],"Resource":["arn:aws:connect:$R:$ACC:instance/*","arn:aws:connect:$R:$ACC:instance/*/contact/*","arn:aws:connect:$R:$ACC:instance/*/contact-flow/*"]}]}
J
aws iam put-role-policy --role-name a2a-voice-task-$R --policy-name a2a-voice-agent --policy-document file:///tmp/tp.json
kv A2A_ECS_EXEC_ROLE_ARN "$(aws iam get-role --role-name a2a-voice-ecs-exec --query Role.Arn --output text)"
kv A2A_ECS_INFRA_ROLE_ARN "$(aws iam get-role --role-name a2a-voice-ecs-infra --query Role.Arn --output text)"
kv A2A_TASK_ROLE_ARN "$(aws iam get-role --role-name a2a-voice-task-$R --query Role.Arn --output text)"

# 로그, ECR
aws logs create-log-group --log-group-name /a2a-voice/agent 2>/dev/null
aws logs put-retention-policy --log-group-name /a2a-voice/agent --retention-in-days 14
kv A2A_LOG_GROUP "/a2a-voice/agent"
ECR=$(aws ecr describe-repositories --repository-names a2a-voice-agent --query 'repositories[0].repositoryUri' --output text 2>/dev/null || aws ecr create-repository --repository-name a2a-voice-agent --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true --tags Key=project,Value=a2a-voice --query repository.repositoryUri --output text)
kv A2A_ECR_URI "$ECR"

# 에이전트 API 키 (값은 출력하지 않음)
SEC=$(aws secretsmanager describe-secret --secret-id a2a-voice-agent-key --query ARN --output text 2>/dev/null || aws secretsmanager create-secret --name a2a-voice-agent-key --description "API key for the a2a-voice audio agent" --secret-string "$(python3 -c 'import secrets;print(secrets.token_urlsafe(36))')" --tags Key=project,Value=a2a-voice --query ARN --output text)
kv A2A_AGENT_KEY_SECRET_ARN "$SEC"
cat $OUT
