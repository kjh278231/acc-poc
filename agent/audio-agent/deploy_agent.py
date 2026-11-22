#!/usr/bin/env python3
"""Deploy the a2a-voice audio agent on Amazon ECS Express Mode (a2a-voice fork of deploy_lab.py).

Reads every ARN from <outputs.env> (written by scripts/p1_region_foundation.sh and the
P2 steps) instead of the workshop's event-stack, so it runs in any Region.

  python3 deploy_agent.py --tag v1 [--engine loopback]   # build, push, create/update
  python3 deploy_agent.py --status                        # state + health
  HOST=$(python3 deploy_agent.py --host)
  python3 deploy_agent.py --delete [--apply]              # service only (P1 resources stay)

Run from the audio-agent folder (Docker needed, e.g. CloudShell). The endpoint is public by
design; the bearer key (secret a2a-voice-agent-key) is its access control.
"""
from __future__ import annotations

import argparse
import base64
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

AGENT_DIR = Path(__file__).resolve().parent
CONTEXT_DIR = AGENT_DIR.parent
DOCKERFILE = AGENT_DIR / "Dockerfile"
NAME = "a2a-voice-agent"


def load_env(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def host_arch() -> str:
    return "ARM64" if platform.machine().lower() in ("arm64", "aarch64") else "X86_64"


def build_and_push(ecr, uri: str, tag: str, arch: str) -> str:
    image = f"{uri}:{tag}"
    repo = uri.split("/", 1)[1]
    try:
        ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
        print(f"  image {image} already in ECR (immutable tags: use a new --tag to rebuild)")
        return image
    except ClientError as e:
        if e.response["Error"]["Code"] != "ImageNotFoundException":
            raise
    tool = next((t for t in ("docker", "finch") if shutil.which(t)), None)
    if not tool:
        sys.exit("docker or finch is required")
    auth = ecr.get_authorization_token()["authorizationData"][0]
    password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)[1]
    subprocess.run([tool, "login", "--username", "AWS", "--password-stdin", uri.split("/")[0]],
                   input=password, text=True, check=True, capture_output=True)
    plat = "linux/arm64" if arch == "ARM64" else "linux/amd64"
    subprocess.run([tool, "build", "--platform", plat, "-f", str(DOCKERFILE), "-t", image, str(CONTEXT_DIR)],
                   check=True)
    subprocess.run([tool, "push", image], check=True)
    print(f"  pushed {image}")
    return image


def container(image: str, out: dict, cfg: dict, engine: str) -> dict:
    env = {
        "AWS_REGION": out["A2A_REGION"],
        "A2A_ENGINE": engine,
        "A2A_SECRET_ID": out["A2A_AGENT_KEY_SECRET_ARN"],
        "CONNECT_INSTANCE_ID": out.get("A2A_INSTANCE_ID", ""),
        "A2A_STT_LANG": cfg.get("A2A_STT_LANG", "ko-KR"),
        "A2A_TTS_VOICE": cfg.get("A2A_TTS_VOICE", "") or "tone",
        "A2A_LLM_MODEL_ID": cfg.get("A2A_LLM_MODEL_ID", "") or "apac.amazon.nova-lite-v1:0",
        "A2A_TTS_ENGINE_KIND": cfg.get("A2A_TTS_ENGINE_KIND", "neural"),
        "A2A_TTS_RATE": cfg.get("A2A_TTS_RATE", ""),  # SSML prosody rate %, e.g. 115
        "A2A_TURN_END_MS": cfg.get("A2A_TURN_END_MS", "150"),
        "A2A_TMS_URL": cfg.get("A2A_TMS_URL", ""),  # callback requests on ESCALATE
        # trace chat span model name: the real LLM for cascade
        "MODEL_ID": (cfg.get("A2A_LLM_MODEL_ID", "") or "apac.amazon.nova-lite-v1:0")
        if engine == "cascade" else f"a2a-voice-{engine}",
        "OUTPUT_SAMPLE_RATE": "16000",
        "SILENCE_KEEPALIVE_SECONDS": "3.0",
        "GOODBYE_MAX_WAIT_SECONDS": "4.0",
        "PYTHONUNBUFFERED": "1",
        # D5 web-call test page (served at /test?k=TOKEN); empty token disables it
        "A2A_TEST_PAGE_TOKEN": out.get("A2A_TEST_PAGE_TOKEN", ""),
        "A2A_TEST_FLOW_ID": out.get("A2A_FLOW_ID", ""),
    }
    return {"image": image, "containerPort": 8080,
            "awsLogsConfiguration": {"logGroup": out["A2A_LOG_GROUP"], "logStreamPrefix": NAME},
            "environment": [{"name": k, "value": v} for k, v in env.items()]}


def service_arn(region: str, account: str) -> str:
    return f"arn:aws:ecs:{region}:{account}:service/default/{NAME}"


def describe(ecs, arn: str):
    try:
        svc = ecs.describe_express_gateway_service(serviceArn=arn)["service"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ServiceNotFoundException", "ResourceNotFoundException",
                                           "ClusterNotFoundException", "ClientException",
                                           "InvalidParameterException"):
            return None
        raise
    return None if svc.get("status", {}).get("statusCode") == "INACTIVE" else svc


def endpoint(svc: dict):
    for cfg in svc.get("activeConfigurations", []) or []:
        for path in cfg.get("ingressPaths", []) or []:
            if path.get("endpoint"):
                return path["endpoint"].replace("https://", "").rstrip("/")
    return None


def not_answering(host: str):
    try:
        with urllib.request.urlopen(f"https://{host}/health", timeout=5) as r:
            return None if r.status == 200 else f"/health returned HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"/health returned HTTP {e.code}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            return "certificate not in place yet"
        if isinstance(e.reason, socket.gaierror):
            return "host name does not resolve yet"
        return f"no answer yet ({e.reason})"
    except OSError as e:
        return f"no answer yet ({e})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outputs", default=None, help="outputs env (default ~/a2a-voice/<region>.outputs.env)")
    ap.add_argument("--config", default=None, help="region config env (A2A_STT_LANG, A2A_TTS_VOICE, ...)")
    ap.add_argument("--engine", default=os.environ.get("A2A_ENGINE", "loopback"),
                    choices=["loopback", "cascade", "nova"])
    ap.add_argument("--tag")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--host", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--arch", choices=["ARM64", "X86_64"])
    ap.add_argument("--region")
    a = ap.parse_args(argv)

    session = boto3.session.Session(region_name=a.region)
    region = session.region_name
    account = session.client("sts").get_caller_identity()["Account"]
    ecs = session.client("ecs")
    arn = service_arn(region, account)

    if a.delete:
        svc = describe(ecs, arn)
        if not svc:
            print(f"{NAME}: not found")
            return 0
        if a.apply:
            ecs.delete_express_gateway_service(serviceArn=arn)
            print(f"{NAME}: delete requested. Wait for --status 'not found' before removing IAM roles.")
        else:
            print(f"{NAME}: exists. Rerun with --delete --apply to delete.")
        return 0

    if a.host or a.status:
        svc = describe(ecs, arn)
        if not svc:
            print(f"{NAME}: not found", file=sys.stderr if a.host else sys.stdout)
            return 1
        host = endpoint(svc)
        if a.host:
            if not host:
                return 1
            print(host)
            return 0
        state = svc["status"].get("statusCode")
        if not host:
            print(f"{NAME}: {state}, no URL yet")
            return 1
        why = not_answering(host)
        print(f"{NAME}: {state}, " + ("ready" if why is None else f"not ready: {why}"))
        print(f"  health: https://{host}/health")
        print(f"  a2a:    wss://{host}/a2a")
        return 0 if why is None else 1

    if not a.tag:
        ap.error("pass --tag vN to deploy, or --status / --host / --delete")
    out = load_env(a.outputs or f"~/a2a-voice/{region}.outputs.env")
    cfg = load_env(a.config) if a.config else {}
    arch = a.arch or host_arch()
    print(f"Image ({arch})")
    image = build_and_push(session.client("ecr"), out["A2A_ECR_URI"], a.tag, arch)
    primary = container(image, out, cfg, a.engine)
    print("Service")
    if describe(ecs, arn):
        ecs.update_express_gateway_service(serviceArn=arn, primaryContainer=primary,
                                           taskRoleArn=out["A2A_TASK_ROLE_ARN"])
        print(f"  updated {NAME} -> {image} (engine={a.engine})")
        return 0
    kw = dict(serviceName=NAME, executionRoleArn=out["A2A_ECS_EXEC_ROLE_ARN"],
              infrastructureRoleArn=out["A2A_ECS_INFRA_ROLE_ARN"], taskRoleArn=out["A2A_TASK_ROLE_ARN"],
              healthCheckPath="/health", primaryContainer=primary, cpu="1024", memory="2048",
              cpuArchitecture=arch, scalingTarget={"minTaskCount": 1, "maxTaskCount": 1},
              tags=[{"key": "project", "value": "a2a-voice"}])
    for attempt in range(6):
        try:
            ecs.create_express_gateway_service(**kw)
            break
        except ClientError as e:
            err = e.response.get("Error", {})
            if err.get("Code") == "InvalidParameterException" and "service linked role" in \
                    err.get("Message", "").lower() and attempt < 5:
                print("  waiting for the ECS service-linked role")
                time.sleep(10)
                continue
            raise
    print(f"  created {NAME} (engine={a.engine}). First service takes ~6 minutes; poll --status.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
