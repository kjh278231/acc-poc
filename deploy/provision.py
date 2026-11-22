"""Provision (idempotently) the AWS resources for a public demo of the voice agent on EC2.

  uv run python deploy/provision.py [--ssh-cidr 1.2.3.4/32] [--instance-type t3.micro]

Creates / reuses (all tagged Project=acc-poc):
  * key pair `acc-poc`           -> private key saved to ~/.ssh/acc-poc.pem (0600)
  * security group `acc-poc-sg`  -> 22 from --ssh-cidr (default: this machine's public IP), 80/443 from anywhere
  * IAM role/profile `acc-poc-ec2` -> only polly:SynthesizeSpeech/DescribeVoices + transcribe streaming
                                    (no AWS keys are copied to the server)
  * EC2 instance `acc-poc` (Ubuntu 24.04, gp3 20 GB) in the first subnet of the VPC with an internet gateway
  * Elastic IP                    -> stable address; the HTTPS hostname is <ip-with-dashes>.sslip.io

Writes deploy/.state.json (gitignored) for deploy/deploy.sh.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

import boto3

REGION = "ap-northeast-2"
NAME = "acc-poc"
TAGS = [{"Key": "Name", "Value": NAME}, {"Key": "Project", "Value": NAME}]
STATE = Path(__file__).parent / ".state.json"
KEY_PATH = Path.home() / ".ssh" / f"{NAME}.pem"

ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Resource": "*", "Action": [
        "polly:SynthesizeSpeech", "polly:DescribeVoices",
        "transcribe:StartStreamTranscription", "transcribe:StartStreamTranscriptionWebSocket"]}],
}
TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def tagspec(kind: str) -> list[dict]:
    return [{"ResourceType": kind, "Tags": TAGS}]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-cidr", default=None)
    ap.add_argument("--instance-type", default="t3.micro")  # test use: smallest that fits (1 GB + swap)
    a = ap.parse_args()
    ssh_cidr = a.ssh_cidr or urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip() + "/32"

    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam")
    ssm = boto3.client("ssm", region_name=REGION)

    # ---- network: VPC with an internet gateway, first subnet routed to it ----
    vpc = subnet = None
    for v in ec2.describe_vpcs()["Vpcs"]:
        igw = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [v["VpcId"]]}])["InternetGateways"]
        if igw:
            subs = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [v["VpcId"]]}])["Subnets"]
            if subs:
                vpc, subnet = v["VpcId"], sorted(subs, key=lambda s: s["SubnetId"])[0]["SubnetId"]
                break
    if not vpc:
        raise SystemExit("no VPC with an internet gateway and a subnet found")
    print("vpc", vpc, "subnet", subnet)

    # ---- key pair ----
    keys = ec2.describe_key_pairs(Filters=[{"Name": "key-name", "Values": [NAME]}])["KeyPairs"]
    if not keys:
        k = ec2.create_key_pair(KeyName=NAME, KeyType="ed25519", TagSpecifications=tagspec("key-pair"))
        KEY_PATH.parent.mkdir(mode=0o700, exist_ok=True)
        KEY_PATH.write_text(k["KeyMaterial"])
        os.chmod(KEY_PATH, 0o600)
        print("key pair created ->", KEY_PATH)
    elif not KEY_PATH.exists():
        raise SystemExit(f"key pair {NAME} exists in AWS but {KEY_PATH} is missing; delete the key pair or restore the file")

    # ---- security group ----
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{NAME}-sg"]}, {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if sgs:
        sg = sgs[0]["GroupId"]
    else:
        sg = ec2.create_security_group(GroupName=f"{NAME}-sg", Description="acc-poc public demo: https + ssh from admin",
                                       VpcId=vpc, TagSpecifications=tagspec("security-group"))["GroupId"]
    want = [(22, ssh_cidr, "ssh admin"), (80, "0.0.0.0/0", "http (ACME + redirect)"), (443, "0.0.0.0/0", "https")]
    have = {(p["FromPort"], r["CidrIp"]) for p in ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"]
            for r in p.get("IpRanges", [])}
    for port, cidr, desc in want:
        if (port, cidr) not in have:
            ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                                                             "IpRanges": [{"CidrIp": cidr, "Description": desc}]}])
    print("security group", sg, "ssh from", ssh_cidr)

    # ---- IAM role + instance profile ----
    role = f"{NAME}-ec2"
    try:
        iam.get_role(RoleName=role)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(TRUST), Tags=TAGS,
                        Description="acc-poc voice demo: Polly + Transcribe streaming only")
    iam.put_role_policy(RoleName=role, PolicyName="voice", PolicyDocument=json.dumps(ROLE_POLICY))
    try:
        iam.get_instance_profile(InstanceProfileName=role)
    except iam.exceptions.NoSuchEntityException:
        iam.create_instance_profile(InstanceProfileName=role, Tags=TAGS)
        iam.add_role_to_instance_profile(InstanceProfileName=role, RoleName=role)
        time.sleep(10)  # instance profile propagation
    print("iam role", role)

    # ---- instance ----
    found = [i for r in ec2.describe_instances(Filters=[{"Name": "tag:Project", "Values": [NAME]},
                                                       {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
             for i in r["Instances"]]
    if found:
        iid = found[0]["InstanceId"]
        if found[0]["State"]["Name"] in ("stopped", "stopping"):
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
            ec2.start_instances(InstanceIds=[iid])
    else:
        ami = ssm.get_parameter(Name="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id")["Parameter"]["Value"]
        for attempt in range(5):
            try:
                iid = ec2.run_instances(
                    ImageId=ami, InstanceType=a.instance_type, MinCount=1, MaxCount=1, KeyName=NAME,
                    IamInstanceProfile={"Name": role},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet, "Groups": [sg], "AssociatePublicIpAddress": True}],
                    BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 20, "VolumeType": "gp3", "DeleteOnTermination": True}}],
                    MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
                    TagSpecifications=tagspec("instance") + tagspec("volume"))["Instances"][0]["InstanceId"]
                break
            except ec2.exceptions.ClientError as e:  # profile not yet visible to EC2
                if "Invalid IAM Instance Profile" not in str(e) or attempt == 4:
                    raise
                time.sleep(8)
        print("instance launched", iid, ami)
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])

    # ---- elastic IP ----
    addrs = ec2.describe_addresses(Filters=[{"Name": "tag:Project", "Values": [NAME]}])["Addresses"]
    if addrs:
        alloc, ip = addrs[0]["AllocationId"], addrs[0]["PublicIp"]
    else:
        r = ec2.allocate_address(Domain="vpc", TagSpecifications=tagspec("elastic-ip"))
        alloc, ip = r["AllocationId"], r["PublicIp"]
    if addrs and addrs[0].get("InstanceId") == iid:
        pass
    else:
        ec2.associate_address(AllocationId=alloc, InstanceId=iid, AllowReassociation=True)
    host = ip.replace(".", "-") + ".sslip.io"
    state = {"region": REGION, "instance_id": iid, "ip": ip, "host": host, "allocation_id": alloc, "security_group": sg,
             "role": role, "key_path": str(KEY_PATH), "ssh_cidr": ssh_cidr}
    STATE.write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
