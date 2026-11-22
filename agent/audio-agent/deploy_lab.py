# SPDX-License-Identifier: MIT-0
"""Deploy Sofía, the reference audio agent, on Amazon ECS Express Mode in your team account.

The optional audio lab in Module 2. Sofía is a partner's claims agent: an audio bridge
between Amazon Connect Customer and Amazon Nova 2.5 Sonic, with tools that call the
event's insurance API. Connect reaches her over one secure WebSocket connection, so she
runs on ECS Express Mode, which gives the service a public HTTPS load balancer with a
certificate. You register wss://<host>/a2a with register.py --mode handoff-audio.

Every step checks what exists and creates only what is missing, so you can rerun it.

  python3 deploy_lab.py --tag v1           # build, push and deploy (or update)
  python3 deploy_lab.py --status           # service state, URL and the register command
  HOST=$(python3 deploy_lab.py --host)     # the service's host name, for later commands
  python3 deploy_lab.py --delete           # list what --delete --apply removes
  python3 deploy_lab.py --delete --apply   # remove everything below, service first

Run it from the audio-agent folder of the download with your participant credentials and
a container builder: Docker in CloudShell, or Finch or Docker on your computer. The image
is built for the machine you run on (X86_64 in CloudShell, ARM64 on Apple silicon);
--arch overrides that. What it creates, all named a2a-<name> (default name: audio):

  ECR repository   a2a-<name>                 immutable tags, scan on push
  secret           a2a-<name>-key             the bridge's API key, generated here
  IAM role         a2a-<name>-task            the container's identity, with the
                                              workshop permissions boundary
  ECS Express      a2a-<name>                 1 task, 1 vCPU, 2 GB, health check /health

The Event_Stack provides the two service roles, the log group, the insurance API, the
secret holding that API's key (InsuranceApiKeySecretArn) and the Connect instance. No key value appears in the output.

The service bills while it runs. Remove it with --delete --apply when you are done (it
asks first; set A2A_YES=1 to skip the question). Without --apply, --delete only lists
what it would remove.

The endpoint is public by design, as Connect reaches agents over the internet; the
bridge's bearer check is its access control.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import secrets as pysecrets
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
CONTEXT_DIR = AGENT_DIR.parent          # holds audio-agent/ and starter-kit/a2a_connect/
DOCKERFILE = AGENT_DIR / "Dockerfile"
NOVA_MODEL_ID = os.environ.get("A2A_NOVA_MODEL_ID", "amazon.nova-2-5-sonic")
DEMO_CUSTOMER_ID = "CUST-001"           # Carlos Salazar in the insurance seed data
OUTPUTS = ("EcsExpressInfrastructureRoleArn", "EcsTaskExecutionRoleArn", "EcsLogGroupName",
           "PermissionsBoundaryArn", "InsuranceApiUrl", "InsuranceApiKeySecretArn", "ConnectInstanceId")
TASK_POLICY = "audio-bridge"            # the task role's one inline policy


def event_stack_outputs(cfn, stack: str) -> dict[str, str]:
    """Outputs of the Event_Stack, read by name (the participant policy allows
    DescribeStacks on a named stack but not ListStacks)."""
    try:
        found = cfn.describe_stacks(StackName=stack)["Stacks"]
    except ClientError as e:
        sys.exit(f"Cannot read stack {stack}: {e.response['Error']['Message']}. Pass --stack NAME.")
    out = {o["OutputKey"]: o["OutputValue"] for o in found[0].get("Outputs", [])}
    missing = [k for k in OUTPUTS if k not in out]
    if missing:
        sys.exit(f"Stack {stack} has no output(s) {missing}. Is it the workshop Event_Stack?")
    return out


def host_arch() -> str:
    """The CPU architecture of this machine, so the image builds natively."""
    return "ARM64" if platform.machine().lower() in ("arm64", "aarch64") else "X86_64"


def ensure_repo(ecr, name: str) -> str:
    repo = f"a2a-{name}"
    try:
        return ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]["repositoryUri"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    print(f"  creating ECR repository {repo}")
    return ecr.create_repository(
        repositoryName=repo, imageTagMutability="IMMUTABLE",
        imageScanningConfiguration={"scanOnPush": True},
        tags=[{"Key": "project", "Value": "a2a-workshop"}])["repository"]["repositoryUri"]


def _secret_arn(sm, secret: str) -> str | None:
    try:
        return sm.describe_secret(SecretId=secret)["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        return None


def ensure_key_secret(sm, name: str) -> str:
    """The bridge's API key. Stored raw, because the bridge compares the presented
    bearer with the secret's whole value; register.py copies it for Connect."""
    secret = f"a2a-{name}-key"
    arn = _secret_arn(sm, secret)
    if arn:
        return arn
    print(f"  creating secret {secret} with a generated key (the value is not shown)")
    return sm.create_secret(
        Name=secret, Description="API key for the audio bridge on ECS Express",
        SecretString=pysecrets.token_urlsafe(36),
        Tags=[{"Key": "project", "Value": "a2a-workshop"}])["ARN"]


def task_policy(region: str, account: str, nova_region: str, secret_arns: list[str],
                instance_id: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        # The Nova Sonic bidirectional stream is authorized by bedrock:InvokeModel.
        {"Sid": "NovaSonic", "Effect": "Allow", "Action": "bedrock:InvokeModel",
         "Resource": f"arn:aws:bedrock:{nova_region}::foundation-model/{NOVA_MODEL_ID}"},
        {"Sid": "ReadOwnSecrets", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": secret_arns},
        # Escalation notes for the human agent, written onto the contact on ESCALATE. The
        # workshop permissions boundary allows this on contacts in the account and Region.
        {"Sid": "EscalationNotes", "Effect": "Allow", "Action": "connect:UpdateContactAttributes",
         "Resource": f"arn:aws:connect:{region}:{account}:instance/{instance_id}/contact/*"},
    ]}


def ensure_task_role(iam, name: str, boundary: str, policy: dict, account: str) -> str:
    role = f"a2a-{name}-task"
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    try:
        arn = iam.get_role(RoleName=role)["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        print(f"  creating IAM role {role} (with the workshop permissions boundary)")
        arn = iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(trust),
                              PermissionsBoundary=boundary,
                              Description="Audio bridge on ECS Express: the container's identity",
                              Tags=[{"Key": "project", "Value": "a2a-workshop"}])["Role"]["Arn"]
    iam.put_role_policy(RoleName=role, PolicyName=TASK_POLICY, PolicyDocument=json.dumps(policy))
    return arn


def builder() -> str:
    for tool in ("finch", "docker"):
        if shutil.which(tool):
            return tool
    sys.exit("Neither finch nor docker is on PATH; install one to build the image.")


def build_and_push(ecr, uri: str, tag: str, arch: str) -> str:
    image = f"{uri}:{tag}"
    repo = uri.split("/", 1)[1]
    try:
        ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
        print(f"  image {image} is already in ECR (tags are immutable; pick a new --tag to rebuild)")
        return image
    except ClientError as e:
        if e.response["Error"]["Code"] != "ImageNotFoundException":
            raise
    tool = builder()
    auth = ecr.get_authorization_token()["authorizationData"][0]
    password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)[1]
    subprocess.run([tool, "login", "--username", "AWS", "--password-stdin", uri.split("/")[0]],
                   input=password, text=True, check=True, capture_output=True)
    target = "linux/arm64" if arch == "ARM64" else "linux/amd64"
    subprocess.run([tool, "build", "--platform", target, "-f", str(DOCKERFILE), "-t", image,
                    str(CONTEXT_DIR)], check=True)
    subprocess.run([tool, "push", image], check=True)
    print(f"  pushed {image}")
    return image


def container(image: str, name: str, out: dict, region: str, nova_region: str,
              key_arn: str, insurance_arn: str) -> dict:
    env = {"AWS_REGION": region, "A2A_SECRET_ID": key_arn,
           "INSURANCE_API_KEY_SECRET_ID": insurance_arn, "INSURANCE_API_URL": out["InsuranceApiUrl"],
           "CONNECT_INSTANCE_ID": out["ConnectInstanceId"], "DEMO_CUSTOMER_ID": DEMO_CUSTOMER_ID,
           "NOVA_REGION": nova_region, "MODEL_ID": NOVA_MODEL_ID, "VOICE_ID": "tiffany",
           "DEMO_TIMEZONE": os.environ.get("A2A_DEMO_TIMEZONE", "America/Chicago"),
           "PYTHONUNBUFFERED": "1"}
    return {"image": image, "containerPort": 8080,
            "awsLogsConfiguration": {"logGroup": out["EcsLogGroupName"], "logStreamPrefix": f"a2a-{name}"},
            "environment": [{"name": k, "value": v} for k, v in env.items()]}


def service_arn(region: str, account: str, name: str) -> str:
    return f"arn:aws:ecs:{region}:{account}:service/default/a2a-{name}"


def describe(ecs, arn: str) -> dict | None:
    # A new account has no default cluster until its first Express service creates one,
    # and describing a service in a missing cluster raises ClusterNotFoundException.
    try:
        svc = ecs.describe_express_gateway_service(serviceArn=arn)["service"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ServiceNotFoundException", "ResourceNotFoundException",
                                           "ClusterNotFoundException", "ClientException",
                                           "InvalidParameterException"):
            return None
        raise
    return None if svc.get("status", {}).get("statusCode") == "INACTIVE" else svc


SLR_TRIES = 6             # tries while ECS's new service-linked role settles
SLR_RETRY_SECONDS = 10


def create_service(ecs, **kw) -> None:
    """Create the Express service. In an account where ECS has never run a service, the first
    call makes ECS create its service-linked role, then fails because the new role cannot be
    assumed yet. The same call works a few seconds later, so that one error is retried."""
    for attempt in range(1, SLR_TRIES + 1):
        try:
            ecs.create_express_gateway_service(**kw)
            return
        except ClientError as e:
            err = e.response.get("Error", {})
            new_role = (err.get("Code") == "InvalidParameterException"
                        and "service linked role" in err.get("Message", "").lower())
            if not new_role or attempt == SLR_TRIES:
                raise
            print("  waiting for the new ECS service-linked role to be ready")
            time.sleep(SLR_RETRY_SECONDS)


def not_answering(host: str, timeout: float = 5.0) -> str | None:
    """Why https://<host>/health does not answer yet, or None once it answers 200 with a valid
    certificate. The service shows its URL at once; its host name and certificate take a few
    more minutes."""
    try:
        with urllib.request.urlopen(f"https://{host}/health", timeout=timeout) as r:
            return None if r.status == 200 else f"/health returned HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"/health returned HTTP {e.code}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            return "its certificate is not in place yet"
        if isinstance(e.reason, socket.gaierror):
            return "its host name does not resolve yet"
        return f"it does not answer yet ({e.reason})"
    except OSError as e:
        return f"it does not answer yet ({e})"


def deploy(ecs, arn: str, name: str, primary: dict, out: dict, task_role: str, arch: str) -> None:
    if describe(ecs, arn):
        ecs.update_express_gateway_service(serviceArn=arn, primaryContainer=primary, taskRoleArn=task_role)
        print(f"  updated service a2a-{name} -> {primary['image']}")
        return
    create_service(
        ecs, serviceName=f"a2a-{name}", executionRoleArn=out["EcsTaskExecutionRoleArn"],
        infrastructureRoleArn=out["EcsExpressInfrastructureRoleArn"], taskRoleArn=task_role,
        healthCheckPath="/health", primaryContainer=primary, cpu="1024", memory="2048",
        cpuArchitecture=arch, scalingTarget={"minTaskCount": 1, "maxTaskCount": 1},
        tags=[{"key": "project", "value": "a2a-workshop"}])
    print(f"  created service a2a-{name}")


def endpoint(svc: dict) -> str | None:
    for cfg in svc.get("activeConfigurations", []) or []:
        for path in cfg.get("ingressPaths", []) or []:
            if path.get("endpoint"):
                return path["endpoint"].replace("https://", "").rstrip("/")
    return None


def register_command(name: str, host: str) -> str:
    return (f"python3 register.py --name {name} --mode handoff-audio "
            f"--url wss://{host}/a2a --key-from secret:a2a-{name}-key")


def _exists(call, missing_code: str) -> bool:
    try:
        call()
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != missing_code:
            raise
        return False


def remove(ecs, ecr, sm, iam, arn: str, name: str, apply: bool) -> int:
    """Remove what this script created for a2a-<name>: the service first (it is what
    bills), then the image repository with its images, both secrets and the task role.
    Without apply, only list what exists. Returns how many of the five exist."""
    verb = "deleted" if apply else "found"
    repo, role = f"a2a-{name}", f"a2a-{name}-task"
    found = 0
    if describe(ecs, arn):
        found += 1
        if apply:
            ecs.delete_express_gateway_service(serviceArn=arn)
        print(f"  ECS Express service a2a-{name} {verb}")
    if _exists(lambda: ecr.describe_repositories(repositoryNames=[repo]), "RepositoryNotFoundException"):
        found += 1
        if apply:
            ecr.delete_repository(repositoryName=repo, force=True)
        print(f"  ECR repository {repo} {verb}")
    for secret in (f"a2a-{name}-key", f"a2a-{name}-insurance-key"):
        if _exists(lambda s=secret: sm.describe_secret(SecretId=s), "ResourceNotFoundException"):
            found += 1
            if apply:
                sm.delete_secret(SecretId=secret, ForceDeleteWithoutRecovery=True)
            print(f"  secret {secret} {verb}")
    if _exists(lambda: iam.get_role(RoleName=role), "NoSuchEntity"):
        found += 1
        if apply:
            try:
                iam.delete_role_policy(RoleName=role, PolicyName=TASK_POLICY)
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchEntity":
                    raise
            iam.delete_role(RoleName=role)
        print(f"  IAM role {role} {verb}")
    return found


def _discard_pending_input() -> None:
    """Drop anything typed or pasted before the question, so the next line of a pasted
    block is not read as the answer."""
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        pass


def confirm(summary: str) -> None:
    """Same question as the setup scripts' teardown: A2A_YES=1 answers it."""
    print(summary)
    if os.environ.get("A2A_YES") == "1":
        return
    _discard_pending_input()
    answer = input("Proceed? [y/N] ").strip()
    if answer.lower() not in ("y", "yes"):
        sys.exit(f"Aborted, nothing changed (the answer was {answer[:60]!r}; type y to proceed).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="audio", help="suffix for every resource name (default: audio)")
    ap.add_argument("--tag", help="image tag to build and deploy, for example v1")
    ap.add_argument("--status", action="store_true", help="show the service state, URL and register command")
    ap.add_argument("--host", action="store_true",
                    help="print only the service's host name, for HOST=$(...) in a shell")
    ap.add_argument("--delete", action="store_true",
                    help="remove the service, image repository, both secrets and the task role "
                         "(lists them unless --apply is also given)")
    ap.add_argument("--apply", action="store_true", help="with --delete: delete, after asking")
    ap.add_argument("--arch", choices=["ARM64", "X86_64"], default=None,
                    help="CPU architecture to build for and run on (default: this machine's)")
    ap.add_argument("--stack", default=os.environ.get("A2A_EVENT_STACK", "event-stack"),
                    help="Event_Stack name (default: event-stack, the name Workshop Studio uses)")
    ap.add_argument("--region", default=None)
    a = ap.parse_args(argv)
    if a.apply and not a.delete:
        ap.error("--apply goes with --delete")
    if not (a.tag or a.status or a.host or a.delete):
        ap.error("pass --tag vN to deploy, --status, --host, or --delete")

    session = boto3.session.Session(region_name=a.region)
    region = session.region_name
    account = session.client("sts").get_caller_identity()["Account"]
    ecs = session.client("ecs")
    arn = service_arn(region, account, a.name)

    if a.delete:
        if a.apply:
            confirm(f"Delete the service, image repository, secrets and task role named "
                    f"a2a-{a.name} in {account} ({region})?")
        found = remove(ecs, session.client("ecr"), session.client("secretsmanager"),
                       session.client("iam"), arn, a.name, a.apply)
        if not found:
            print(f"Nothing named a2a-{a.name} found.")
        elif not a.apply:
            print("Dry run. Rerun with --delete --apply to delete.")
        else:
            print("ECS removes the service and its load balancer over the next few minutes; "
                  "--status shows it until it is gone.")
        return 0

    if a.host:
        # Only the host goes to stdout, so HOST=$(... --host) is empty when there is none.
        svc = describe(ecs, arn)
        host = endpoint(svc) if svc else None
        if not host:
            print(f"a2a-{a.name} has no URL yet. Run --status to see its state.", file=sys.stderr)
            return 1
        print(host)
        return 0

    if a.status:
        svc = describe(ecs, arn)
        if not svc:
            print(f"a2a-{a.name}: not found")
            return 1
        host = endpoint(svc)
        state = svc["status"].get("statusCode")
        if not host:
            print(f"a2a-{a.name}: {state}, no URL yet. Run --status again in a minute.")
            return 1
        why = not_answering(host)
        print(f"a2a-{a.name}: {state}, " +
              ("ready" if why is None else f"not ready: {why}. Run --status again in a minute."))
        print(f"  health:   https://{host}/health")
        print(f"  register: {register_command(a.name, host)}")
        return 0 if why is None else 1

    arch = a.arch or host_arch()
    nova_region = os.environ.get("A2A_NOVA_REGION") or region
    out = event_stack_outputs(session.client("cloudformation"), a.stack)
    sm = session.client("secretsmanager")
    print("Prerequisites")
    uri = ensure_repo(session.client("ecr"), a.name)
    key_arn = ensure_key_secret(sm, a.name)
    insurance_arn = out["InsuranceApiKeySecretArn"]
    policy = task_policy(region, account, nova_region, [key_arn, insurance_arn], out["ConnectInstanceId"])
    task_role = ensure_task_role(session.client("iam"), a.name, out["PermissionsBoundaryArn"], policy, account)
    print(f"Image ({arch})")
    image = build_and_push(session.client("ecr"), uri, a.tag, arch)
    print("Service")
    primary = container(image, a.name, out, region, nova_region, key_arn, insurance_arn)
    deploy(ecs, arn, a.name, primary, out, task_role, arch)
    print("Run --status until it says ready: about 6 minutes for the first service in an "
          "account, a minute or two after that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
