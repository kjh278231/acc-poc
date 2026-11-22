# 05. ECS Express Mode 배포 (리전마다)

> 워크숍 `deploy_lab.py`의 흐름을 따른다. 단, `event-stack` 출력 대신 `config/${A2A_REGION}.outputs.env`를 읽도록 바꾼 `scripts/deploy_agent.py`를 쓴다.

## 5.1 이미지 빌드와 푸시

- [ ] 빌드 플랫폼: `linux/amd64`(X86_64) 또는 `linux/arm64`. 태스크 아키텍처와 맞춘다.
- [ ] 태그는 매번 새로 붙인다(ECR immutable): `v1`, `v2`, ...
- [ ] 리전마다 따로 푸시한다(ECR은 리전 리소스). 같은 이미지를 두 리전 ECR에 올리거나 ECR 복제 규칙을 쓴다.

```bash
aws ecr get-login-password | docker login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.${A2A_REGION}.amazonaws.com
docker build --platform linux/amd64 -t ${A2A_ECR_URI}:v1 .
docker push ${A2A_ECR_URI}:v1
```

## 5.2 서비스 생성

워크숍은 `ecs.create_express_gateway_service(...)`로 아래 구성을 만든다.

| 항목 | 값 |
|---|---|
| 서비스 이름 | `a2a-voice-agent` (클러스터 `default`, 첫 Express 서비스가 자동 생성) |
| 태스크 | 1개, 1 vCPU, 2GB |
| 컨테이너 포트 | 8080 |
| 헬스체크 | `/health` |
| 실행 역할 | `A2A_ECS_EXEC_ROLE_ARN` |
| 인프라 역할 | `A2A_ECS_INFRA_ROLE_ARN` |
| 태스크 역할 | `A2A_TASK_ROLE_ARN` |
| 로그 | `A2A_LOG_GROUP`, prefix `a2a-voice-agent` |
| 네트워크 | 기본 VPC 퍼블릭 서브넷 (D6 대안 B일 때는 서브넷/SG 지정 `[검증]`) |

컨테이너 환경변수:

```
AWS_REGION=${A2A_REGION}
A2A_ENGINE=loopback|cascade-ko|nova-tokyo
A2A_SECRET_ID=${A2A_AGENT_KEY_SECRET_ARN}
CONNECT_INSTANCE_ID=${A2A_INSTANCE_ID}
A2A_STT_LANG=ko-KR
A2A_TTS_VOICE=Seoyeon
A2A_LLM_MODEL_ID=...
A2A_TMS_URL=mock         # ESCALATE 콜백 요청을 받을 TMS. mock이면 0.5초 뒤 접수 응답, 비우면 로그만 남김
SILENCE_KEEPALIVE_SECONDS=3.0
GOODBYE_MAX_WAIT_SECONDS=4.0
PYTHONUNBUFFERED=1
```

주의할 점(워크숍 코드 주석 기준):
- 계정에서 ECS 서비스를 처음 만들면 서비스 연결 역할이 막 생겨서 첫 호출이 실패할 수 있다. 10초 간격으로 몇 번 재시도한다.
- 첫 서비스는 ALB와 인증서 생성까지 **약 6분**, 이후 서비스는 1~2분 걸린다.
- 호스트 이름이 DNS에 잡히기 전이나 인증서가 붙기 전에는 `/health`가 실패한다. 준비될 때까지 상태만 확인한다.

## 5.3 준비 확인

```bash
python3 scripts/deploy_agent.py --status   # ready 가 나올 때까지
HOST=$(python3 scripts/deploy_agent.py --host)
curl -s -o /dev/null -w "%{http_code}\n" https://$HOST/health          # 200
curl -s -o /dev/null -w "%{http_code}\n" https://$HOST/a2a              # 401 이어야 정상(키 없음)
```

## 5.4 웹소켓 관련 점검

- [ ] ALB 유휴 타임아웃: 통화 중에는 20ms마다 프레임이 오므로 문제없다. 연결 직후 대기 구간도 60초 기본값 안에 끝나는지 확인한다.
- [ ] 배포 교체: ECS가 태스크를 내리면(SIGTERM) 열린 웹소켓을 **1012로 닫는다**. Connect가 1회 재연결한다. `stopTimeout`과 ALB deregistration delay를 통화 길이에 맞게 조정한다.
- [ ] 로그에 API 키와 Authorization 헤더가 남지 않는지 확인한다(워크숍 `logredact.py` 참고).

## 5.5 삭제

- 워크숍: `deploy_lab.py --delete --apply`가 서비스부터 지운다.
- ECS가 ALB를 지우는 작업은 delete 명령이 끝난 뒤에도 계속된다. **`--status`가 `not found`를 보일 때까지 기다린 뒤** 인프라 역할을 지운다. 먼저 지우면 ALB가 남는다.

## 완료 기준
- [ ] `/health` 200, `/a2a` 키 없이 401
- [ ] 배포된 `wss://$HOST/a2a`로 tester core와 all 통과
