"""Browser web-call test page (D5), served by the agent itself.

GET  /test?k=TOKEN          -> page with Start / Hang up buttons
POST /test/start?k=TOKEN    -> connect:StartWebRTCContact on A2A_TEST_FLOW_ID, returns ConnectionData
POST /test/stop?k=TOKEN     -> connect:StopContact

Disabled unless A2A_TEST_PAGE_TOKEN and A2A_TEST_FLOW_ID are set. The browser joins the
call with the Amazon Chime SDK (loaded as ESM from jsDelivr). Use a headset.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os

from aiohttp import web

log = logging.getLogger("bridge.testpage")

TOKEN = os.environ.get("A2A_TEST_PAGE_TOKEN", "")
FLOW_ID = os.environ.get("A2A_TEST_FLOW_ID", "")
INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")
REGION = os.environ.get("AWS_REGION", "")

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>a2a-voice web call test</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#222}
button{font-size:18px;padding:10px 22px;margin-right:8px;border-radius:8px;border:1px solid #888;cursor:pointer}
#start{background:#0a7d3a;color:#fff;border-color:#0a7d3a}#stop{background:#b42318;color:#fff;border-color:#b42318}
#log{background:#f5f5f5;border-radius:8px;padding:12px;font:13px/1.5 ui-monospace,monospace;white-space:pre-wrap;min-height:120px}
.muted{color:#666;font-size:14px}
#script{margin-top:16px;border:1px solid #ddd;border-radius:8px;padding:12px;max-height:50vh;overflow-y:auto}
#script p{margin:6px 0}#script b{display:inline-block;min-width:4.5em}.USER b{color:#0a58ca}.ASSISTANT b{color:#0a7d3a}</style></head><body>
<h2>a2a-voice 웹 통화 테스트</h2>
<p class="muted">Region: __REGION__ / Flow: __FLOW__<br>헤드셋을 쓰세요. 스피커로 들으면 에이전트가 자기 목소리를 끼어들기로 인식합니다.</p>
<p><button id="start">통화 시작</button><button id="stop" disabled>끊기</button></p>
<audio id="audio" autoplay></audio>
<div id="log"></div>
<h3>대화 스크립트</h3>
<div id="script" aria-live="polite"><p class="muted">통화를 시작하면 여기에 대화가 표시됩니다.</p></div>
<script type="module">
import * as Chime from 'https://cdn.jsdelivr.net/npm/amazon-chime-sdk-js@3/+esm';
const k = new URLSearchParams(location.search).get('k') || '';
const $ = (id) => document.getElementById(id);
const log = (m) => { $('log').textContent += new Date().toLocaleTimeString() + '  ' + m + '\\n'; };
let session = null, contactId = null, poll = null;
const ROLE = {USER: '고객', ASSISTANT: '에이전트'};
async function refreshScript() {
  if (!contactId) return;
  const r = await fetch('/test/transcript?k=' + encodeURIComponent(k) + '&c=' + contactId);
  if (!r.ok) return;
  const box = $('script');
  box.replaceChildren(...(await r.json()).map(([role, text]) => {
    const p = document.createElement('p'), b = document.createElement('b');
    p.className = role; b.textContent = (ROLE[role] || role) + ':';
    p.append(b, ' ' + text);
    return p;
  }));
  box.scrollTop = box.scrollHeight;
}
const stopPoll = () => { clearInterval(poll); poll = null; setTimeout(refreshScript, 1500); };
$('start').onclick = async () => {
  $('start').disabled = true;
  try {
    log('마이크 권한 요청...');
    await navigator.mediaDevices.getUserMedia({audio: true});
    log('StartWebRTCContact 호출...');
    const r = await fetch('/test/start?k=' + encodeURIComponent(k), {method: 'POST'});
    if (!r.ok) throw new Error('start failed: ' + r.status + ' ' + await r.text());
    const d = await r.json();
    contactId = d.ContactId;
    log('contactId=' + contactId);
    $('script').replaceChildren();
    poll = setInterval(refreshScript, 1000);
    const logger = new Chime.ConsoleLogger('a2a', Chime.LogLevel.WARN);
    const dc = new Chime.DefaultDeviceController(logger);
    const cfg = new Chime.MeetingSessionConfiguration(d.ConnectionData.Meeting, d.ConnectionData.Attendee);
    session = new Chime.DefaultMeetingSession(cfg, logger, dc);
    const mics = await session.audioVideo.listAudioInputDevices();
    await session.audioVideo.startAudioInput(mics[0].deviceId);
    await session.audioVideo.bindAudioElement($('audio'));
    session.audioVideo.addObserver({
      audioVideoDidStart: () => log('통화 연결됨 (Voice call connected)'),
      audioVideoDidStop: (s) => { log('통화 종료: ' + s.statusCode()); stopPoll(); $('start').disabled = false; $('stop').disabled = true; },
    });
    session.audioVideo.start();
    $('stop').disabled = false;
  } catch (e) { log('ERROR ' + e.message); $('start').disabled = false; }
};
$('stop').onclick = async () => {
  try { session && session.audioVideo.stop(); } catch (e) {}
  if (contactId) await fetch('/test/stop?k=' + encodeURIComponent(k) + '&c=' + contactId, {method: 'POST'});
  log('끊기 요청');
};
</script></body></html>"""


# shortcut: kept in this task's memory, so it only works with one ECS task; use a shared store if the service scales out.
TRANSCRIPTS: dict[str, list] = {}
_MAX_CONTACTS = 50


def add_line(contact_arn: str | None, role: str, text: str) -> None:
    """Record one caller/agent utterance for the test page; consecutive same-role chunks merge."""
    text = (text or "").strip()
    if not (TOKEN and contact_arn and text):
        return
    cid = contact_arn.rsplit("/", 1)[-1]
    if cid not in TRANSCRIPTS and len(TRANSCRIPTS) >= _MAX_CONTACTS:
        TRANSCRIPTS.pop(next(iter(TRANSCRIPTS)))
    lines = TRANSCRIPTS.setdefault(cid, [])
    if lines and lines[-1][0] == role:
        lines[-1][1] += " " + text
    else:
        lines.append([role, text])


async def transcript(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=404)
    return web.json_response(TRANSCRIPTS.get(request.query.get("c", ""), []))


def _authorized(request: web.Request) -> bool:
    return bool(TOKEN) and hmac.compare_digest(request.query.get("k", ""), TOKEN)


async def page(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=404)
    html = PAGE.replace("__REGION__", REGION).replace("__FLOW__", FLOW_ID)
    return web.Response(text=html, content_type="text/html")


def _client():
    import boto3
    return boto3.client("connect", region_name=REGION)


async def start(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=404)

    def call():
        return _client().start_web_rtc_contact(
            InstanceId=INSTANCE_ID, ContactFlowId=FLOW_ID,
            ParticipantDetails={"DisplayName": "a2a-voice-test"},
            Attributes={"source": "a2a-voice-testpage"})

    try:
        r = await asyncio.get_running_loop().run_in_executor(None, call)
    except Exception as e:  # noqa: BLE001
        log.exception("StartWebRTCContact failed")
        return web.Response(status=502, text=str(e)[:300])
    r.pop("ResponseMetadata", None)
    log.info("test call started contact=%s", r.get("ContactId"))
    return web.Response(text=json.dumps(r, default=str), content_type="application/json")


async def stop(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=404)
    cid = request.query.get("c", "")
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _client().stop_contact(ContactId=cid, InstanceId=INSTANCE_ID))
    except Exception as e:  # noqa: BLE001
        log.info("StopContact: %s", e)
    return web.json_response({"ok": True})


def add_routes(app: web.Application) -> None:
    if TOKEN and FLOW_ID and INSTANCE_ID:
        app.router.add_get("/test", page)
        app.router.add_post("/test/start", start)
        app.router.add_post("/test/stop", stop)
        app.router.add_get("/test/transcript", transcript)
        log.info("test page enabled for flow %s", FLOW_ID)
