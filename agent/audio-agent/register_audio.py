#!/usr/bin/env python3
"""Register the a2a-voice agent with Amazon Connect for an AUDIO handoff (fork of register.py).

Reads ~/a2a-voice/<region>.outputs.env instead of the workshop event-stack, and builds its
own contact flow instead of cloning the workshop's starting flow. Uses the AWS CLI (like the
workshop scripts) so new qconnect/appintegrations shapes work even with an older boto3.

  python3 register_audio.py --url wss://HOST/a2a [--voice Seoyeon] [--dry-run]
  python3 register_audio.py --teardown [--apply]

Eight steps: KMS key (reuse) -> secret (Connect-readable) -> A2A_SERVER application ->
instance association -> security profile -> orchestrator AI agent -> publish -> contact flow.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "a2a-voice"
SECRET = f"{NAME}-connect-key"
APP = NAME
SP = NAME
AGENT = f"{NAME}-orchestrator"
FLOW = f"{NAME}-audio"

REASON = {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}
TOOLS = [
    {"toolName": "Complete", "toolType": "RETURN_TO_CONTROL",
     "description": "Close the conversation when the customer has no more questions",
     "instruction": {"instruction": "Mark the conversation complete when the customer is done."},
     "inputSchema": REASON, "userInteractionConfiguration": {"isUserConfirmationRequired": False}},
]
# No Escalate tool: the flow has no human queue. The ECS agent files callback requests with the TMS itself.
INSTRUCTION = ("Hand off immediately. This agent handles the whole call in its own voice and finishes "
               "with COMPLETE when the call is over.")


def load_env(path: str) -> dict:
    out = {}
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def aws(service: str, op: str, params: dict | None = None, *, quiet_secret=False, ok_codes=()) -> dict:
    """aws <service> <op> --cli-input-json (params never echoed when they hold a secret)."""
    cmd = ["aws", service, op, "--output", "json"]
    tmp = None
    if params:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(params, tmp)
        tmp.close()
        cmd += ["--cli-input-json", f"file://{tmp.name}"]
    shown = "(params hidden)" if quiet_secret else json.dumps(params or {})[:160]
    print(f"  $ aws {service} {op} {shown}")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if tmp:
        os.unlink(tmp.name)
    if p.returncode != 0:
        if any(c in p.stderr for c in ok_codes):
            return {"_error": p.stderr.strip()}
        raise SystemExit(f"FAILED {service} {op}: {p.stderr.strip()}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def cli_op(name: str) -> str:
    """CamelCase -> kebab-case for the CLI."""
    name = name.replace("AIAgent", "AiAgent")
    out = ""
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out += "-"
        out += ch.lower()
    return out


def call(service, op, params=None, **kw):
    return aws(service, cli_op(op), params, **kw)


def flow_content(o: dict, agent_arn_latest: str, voice: str, error_text: str,
                 lang_code: str = "ko-KR") -> str:
    """Start -> logging -> AI session -> Set voice -> language -> Lex (Nova Sonic bot, agent ARN)
    -> disconnect. The call is handled inside the Lex block by the ECS agent; errors play error_text."""
    actions = [
        {"Identifier": "log", "Type": "UpdateFlowLoggingBehavior",
         "Parameters": {"FlowLoggingBehavior": "Enabled"}, "Transitions": {"NextAction": "session"}},
        {"Identifier": "session", "Type": "CreateWisdomSession",
         "Parameters": {"WisdomAssistantArn": o["A2A_ASSISTANT_ARN"]},
         "Transitions": {"NextAction": "setsession", "Errors": [{"NextAction": "err", "ErrorType": "NoMatchingError"}]}},
        # The console's Connect assistant block = CreateWisdomSession + UpdateContactData. Without this
        # second action the contact has no WisdomInfo.SessionArn and the Lex QInConnect intent fails at once.
        {"Identifier": "setsession", "Type": "UpdateContactData",
         "Parameters": {"WisdomSessionArn": "$.Wisdom.SessionArn"},
         "Transitions": {"NextAction": "voice", "Errors": [{"NextAction": "err", "ErrorType": "NoMatchingError"}]}},
        # Only Connect's own prompt (error_text) uses this voice; the call itself uses the agent's TTS.
        {"Identifier": "voice", "Type": "UpdateContactTextToSpeechVoice",
         "Parameters": {"TextToSpeechVoice": voice, "TextToSpeechEngine": "Generative"},
         "Transitions": {"NextAction": "lang", "Errors": [{"NextAction": "lang", "ErrorType": "NoMatchingError"}]}},
        # Console "Set language attribute": without it the contact language defaults to en-US and the
        # Lex runtime answers 404 (ResourceNotFound) because the bot alias only has the A2A_LOCALE locale.
        {"Identifier": "lang", "Type": "UpdateContactData",
         "Parameters": {"LanguageCode": lang_code},
         "Transitions": {"NextAction": "lex", "Errors": [{"NextAction": "err", "ErrorType": "NoMatchingError"}]}},
        {"Identifier": "lex", "Type": "ConnectParticipantWithLexBot",
         # Connect requires a prompt here; 1 ms of silence so the callee hears the agent first (outbound).
         "Parameters": {"SSML": '<speak><break time="1ms"/></speak>', "LexV2Bot": {"AliasArn": o["A2A_SONIC_BOT_ALIAS_ARN"]},
                        "LexSessionAttributes": {"x-amz-lex:q-in-connect:ai-agent-arn": agent_arn_latest}},
         "Transitions": {"NextAction": "end", "Errors": [
             {"NextAction": "end", "ErrorType": "NoMatchingCondition"},
             {"NextAction": "err", "ErrorType": "NoMatchingError"}]}},
        {"Identifier": "err", "Type": "MessageParticipant", "Parameters": {"Text": error_text},
         "Transitions": {"NextAction": "end", "Errors": [{"NextAction": "end", "ErrorType": "NoMatchingError"}]}},
        {"Identifier": "end", "Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
    ]
    return json.dumps({"Version": "2019-10-30", "StartAction": "log", "Actions": actions})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--outputs")
    ap.add_argument("--locale", default=os.environ.get("A2A_LOCALE", "ko_KR"))
    ap.add_argument("--voice", default="Seoyeon")
    ap.add_argument("--error-text", default="연결이 원활하지 않습니다. 잠시 후 다시 걸어 주세요.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    o = load_env(a.outputs or f"~/a2a-voice/{region}.outputs.env")
    acct, inst, inst_arn, asst = o["A2A_ACCOUNT_ID"], o["A2A_INSTANCE_ID"], o["A2A_INSTANCE_ARN"], o["A2A_ASSISTANT_ID"]
    agent_base = f"arn:aws:wisdom:{region}:{acct}:ai-agent/{asst}"

    if a.teardown:
        return teardown(o, region, agent_base, a.apply)
    if not a.url or not a.url.startswith("wss://"):
        ap.error("--url wss://HOST/a2a is required (audio streaming needs wss://)")

    print("1/8 KMS key (reuse A2A_KMS_KEY_ARN)")
    key = o["A2A_KMS_KEY_ARN"]
    pol = json.loads(call("kms", "GetKeyPolicy", {"KeyId": key, "PolicyName": "default"})["Policy"])
    if not any(s.get("Sid") == "AllowConnectToDecryptSecret" for s in pol["Statement"]):
        raise SystemExit("KMS key has no Connect decrypt statement")
    if a.dry_run:
        print("[dry-run] stop after read-only checks")
        print("  url:", a.url, " locale:", a.locale, " flow voice:", a.voice)
        return 0

    print("2/8 Secret readable by Connect")
    agent_key = call("secretsmanager", "GetSecretValue", {"SecretId": o["A2A_AGENT_KEY_SECRET_ARN"]},
                     quiet_secret=True)["SecretString"]
    got = call("secretsmanager", "DescribeSecret", {"SecretId": SECRET}, ok_codes=("ResourceNotFoundException",))
    if "_error" in got:
        sec_arn = call("secretsmanager", "CreateSecret", {"Name": SECRET, "KmsKeyId": key, "SecretString": agent_key,
                                                          "Description": "API key Connect presents to the a2a-voice agent"},
                       quiet_secret=True)["ARN"]
    else:
        sec_arn = got["ARN"]
        call("secretsmanager", "PutSecretValue", {"SecretId": sec_arn, "SecretString": agent_key}, quiet_secret=True)
    call("secretsmanager", "PutResourcePolicy", {"SecretId": sec_arn, "ResourcePolicy": json.dumps({
        "Version": "2012-10-17", "Statement": [{"Sid": "AllowConnectToRetrieveSecret", "Effect": "Allow",
                                               "Principal": {"Service": "connect.amazonaws.com"},
                                               "Action": "secretsmanager:GetSecretValue", "Resource": "*",
                                               "Condition": {"StringEquals": {"aws:SourceAccount": acct},
                                                             "ArnEquals": {"aws:SourceArn": inst_arn}}}]})})

    print("3/8 A2A_SERVER application")
    auth = {"AuthType": "API_KEY", "CredentialProviderIdentifier": sec_arn}
    apps = call("appintegrations", "ListApplications", {}).get("Applications", [])
    hit = next((x for x in apps if x.get("Name") == APP), None)
    if hit:
        call("appintegrations", "UpdateApplication", {"Arn": hit["Arn"], "ApplicationSourceConfig": {
            "ExternalUrlConfig": {"AccessUrl": a.url}}, "AuthConfig": auth})
        app_id, app_arn = hit["Id"], hit["Arn"]
    else:
        r = call("appintegrations", "CreateApplication", {
            "Name": APP, "Namespace": APP, "Description": "a2a-voice external audio agent",
            "ApplicationType": "A2A_SERVER", "ApplicationSourceConfig": {"ExternalUrlConfig": {"AccessUrl": a.url}},
            "AuthConfig": auth, "Tags": {"AmazonConnectEnabled": "True"}})
        app_id, app_arn = r["Id"], r["Arn"]

    print("4/8 Associate application with the instance")
    assoc = call("connect", "ListIntegrationAssociations", {"InstanceId": inst, "IntegrationType": "APPLICATION"})
    if not any(x.get("IntegrationArn") == app_arn for x in assoc.get("IntegrationAssociationSummaryList", [])):
        call("connect", "CreateIntegrationAssociation", {"InstanceId": inst, "IntegrationType": "APPLICATION",
                                                         "IntegrationArn": app_arn})

    print("5/8 Security profile (THIRD_PARTY collaborator + contact context)")
    sps = call("connect", "ListSecurityProfiles", {"InstanceId": inst}).get("SecurityProfileSummaryList", [])
    sp = next((x for x in sps if x["Name"] == SP), None)
    allowed = [{"Arn": app_arn, "Type": "THIRD_PARTY"}]
    if sp:
        sp_id = sp["Id"]
        call("connect", "UpdateSecurityProfile", {"InstanceId": inst, "SecurityProfileId": sp_id,
                                                  "AllowedAIAgents": allowed, "Permissions": ["BasicAgentAccess"]})
    else:
        sp_id = call("connect", "CreateSecurityProfile", {
            "InstanceId": inst, "SecurityProfileName": SP, "Description": "a2a-voice audio collaborator",
            "AllowedAIAgents": allowed, "Permissions": ["BasicAgentAccess"]})["SecurityProfileId"]

    print("6/8 Orchestrator AI agent (immediate audio handoff, Complete only, no prompt)")
    conf = {"orchestrationAIAgentConfiguration": {
        "connectInstanceArn": inst_arn, "locale": a.locale, "toolConfigurations": TOOLS,
        "multiAgentConfigurations": [{"handoffAgentConfiguration": {
            "agentTarget": {"applicationId": app_id}, "instruction": {"instruction": INSTRUCTION},
            "audioStreamingEnabled": True, "immediateHandoff": True}}]}}
    agents = call("qconnect", "ListAIAgents", {"assistantId": asst}).get("aiAgentSummaries", [])
    ag = next((x for x in agents if x.get("name") == AGENT), None)
    if ag:
        agent_id = ag["aiAgentId"]
        call("qconnect", "UpdateAIAgent", {"assistantId": asst, "aiAgentId": agent_id, "visibilityStatus": "PUBLISHED",
                                           "configuration": conf})
    else:
        agent_id = call("qconnect", "CreateAIAgent", {"assistantId": asst, "name": AGENT, "type": "ORCHESTRATION",
                                                      "configuration": conf, "visibilityStatus": "PUBLISHED",
                                                      "description": "a2a-voice: hands voice calls to the ECS agent"})[
            "aiAgent"]["aiAgentId"]

    print("7/8 Security profile on the agent, publish")
    for q in (None, "$SAVED", "$LATEST"):
        call("connect", "AssociateSecurityProfiles", {"InstanceId": inst, "EntityType": "AI_AGENT",
                                                      "EntityArn": f"{agent_base}/{agent_id}" + (f":{q}" if q else ""),
                                                      "SecurityProfiles": [{"Id": sp_id}]},
             ok_codes=("ResourceNotFound", "InvalidParameter", "Duplicate"))
    ver = call("qconnect", "CreateAIAgentVersion", {"assistantId": asst, "aiAgentId": agent_id})
    number = ver.get("versionNumber") or ver["aiAgent"].get("versionNumber")
    call("connect", "AssociateSecurityProfiles", {"InstanceId": inst, "EntityType": "AI_AGENT",
                                                  "EntityArn": f"{agent_base}/{agent_id}:{number}",
                                                  "SecurityProfiles": [{"Id": sp_id}]}, ok_codes=("Duplicate",))

    print("8/8 Contact flow")
    content = flow_content(o, f"{agent_base}/{agent_id}:$LATEST", a.voice, a.error_text,
                           lang_code=a.locale.replace("_", "-"))
    flows = call("connect", "ListContactFlows", {"InstanceId": inst, "ContactFlowTypes": ["CONTACT_FLOW"]})
    fid = next((f["Id"] for f in flows.get("ContactFlowSummaryList", []) if f["Name"] == FLOW), None)
    if fid:
        call("connect", "UpdateContactFlowContent", {"InstanceId": inst, "ContactFlowId": fid, "Content": content})
    else:
        fid = call("connect", "CreateContactFlow", {"InstanceId": inst, "Name": FLOW, "Type": "CONTACT_FLOW",
                                                    "Content": content,
                                                    "Description": "a2a-voice: audio handoff to the ECS agent"})[
            "ContactFlowId"]
    summary = {"A2A_APP_ARN": app_arn, "A2A_APP_ID": app_id, "A2A_CONNECT_SECRET_ARN": sec_arn,
               "A2A_SECURITY_PROFILE_ID": sp_id, "A2A_AGENT_ID": agent_id, "A2A_AGENT_VERSION": str(number),
               "A2A_FLOW_ID": fid, "A2A_AGENT_URL": a.url}
    with open(Path(a.outputs or f"~/a2a-voice/{region}.outputs.env").expanduser(), "a", encoding="utf-8") as f:
        for k, v in summary.items():
            f.write(f"{k}={v}\n")
    print(json.dumps(summary, indent=1))
    return 0


def teardown(o, region, agent_base, apply) -> int:
    inst, asst = o["A2A_INSTANCE_ID"], o["A2A_ASSISTANT_ID"]
    print("would delete" if not apply else "deleting", "flow / agent / security profile / association / app / secret")
    if not apply:
        return 0
    flows = call("connect", "ListContactFlows", {"InstanceId": inst, "ContactFlowTypes": ["CONTACT_FLOW"]})
    for f in flows.get("ContactFlowSummaryList", []):
        if f["Name"] == FLOW:
            call("connect", "DeleteContactFlow", {"InstanceId": inst, "ContactFlowId": f["Id"]})
    for x in call("qconnect", "ListAIAgents", {"assistantId": asst}).get("aiAgentSummaries", []):
        if x.get("name") == AGENT:
            call("qconnect", "DeleteAIAgent", {"assistantId": asst, "aiAgentId": x["aiAgentId"]})
    for x in call("connect", "ListSecurityProfiles", {"InstanceId": inst}).get("SecurityProfileSummaryList", []):
        if x["Name"] == SP:
            call("connect", "DeleteSecurityProfile", {"InstanceId": inst, "SecurityProfileId": x["Id"]})
    apps = call("appintegrations", "ListApplications", {}).get("Applications", [])
    app = next((x for x in apps if x.get("Name") == APP), None)
    if app:
        for x in call("connect", "ListIntegrationAssociations", {"InstanceId": inst, "IntegrationType": "APPLICATION"}
                      ).get("IntegrationAssociationSummaryList", []):
            if x.get("IntegrationArn") == app["Arn"]:
                call("connect", "DeleteIntegrationAssociation", {"InstanceId": inst,
                                                                 "IntegrationAssociationId": x["IntegrationAssociationId"]})
        call("appintegrations", "DeleteApplication", {"Arn": app["Arn"]})
    call("secretsmanager", "DeleteSecret", {"SecretId": SECRET, "ForceDeleteWithoutRecovery": True},
         ok_codes=("ResourceNotFoundException",))
    return 0


if __name__ == "__main__":
    sys.exit(main())
