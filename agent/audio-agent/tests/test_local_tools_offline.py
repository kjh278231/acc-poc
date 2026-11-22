"""Offline check of end_call handling (no AWS): python tests/test_local_tools_offline.py

ESCALATE must reach Connect as COMPLETE, post a callback request to the TMS, retry once,
and log CALLBACK_NOT_FILED when the TMS is down or unset.
"""
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge.local_tools import LocalTools  # noqa: E402

received, fail_first = [], [True]


class Tms(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if fail_first[0]:
            fail_first[0] = False
            self.send_response(500)
        else:
            received.append(body)
            self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class Errors(logging.Handler):
    def __init__(self, level=logging.ERROR):
        super().__init__(level)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


errors = Errors()
logging.getLogger("bridge.local_tools").addHandler(errors)
srv = HTTPServer(("127.0.0.1", 0), Tms)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/callback"
arn = "arn:aws:connect:ap-northeast-2:1:instance/i/contact/c-123"
esc = {"outcome": "ESCALATE", "summary": "요금 문의", "reason": "상담원 요청"}

# 1. TMS fails once, then accepts: one request filed, Connect sees COMPLETE.
t = LocalTools(region="ap-northeast-2", contact_arn=arn, tms_url=url)
assert t.run("end_call", esc) == {"acknowledged": True, "outcome": "ESCALATE"}
assert t.pending.outcome == "COMPLETE"
assert received == [{"contactId": "c-123", "phone": None, "reason": "상담원 요청", "summary": "요금 문의"}], received
assert not errors.lines

# 2. COMPLETE never calls the TMS.
LocalTools(region="ap-northeast-2", contact_arn=arn, tms_url=url).run(
    "end_call", {"outcome": "COMPLETE", "summary": "끝"})
assert len(received) == 1

# 3. TMS down: CALLBACK_NOT_FILED with the request body.
srv.shutdown()
srv.server_close()
LocalTools(region="ap-northeast-2", contact_arn=arn, tms_url=url).run("end_call", esc)
assert len(errors.lines) == 1 and errors.lines[0].startswith("CALLBACK_NOT_FILED") and "c-123" in errors.lines[0]

# 4. No TMS configured: no HTTP attempt, still logged.
LocalTools(region="ap-northeast-2", contact_arn=arn).run("end_call", esc)
assert len(errors.lines) == 2

# 5. Mock TMS: ~0.5 s, accepted, reply logged, nothing at ERROR.
infos = Errors(logging.INFO)
lt_log = logging.getLogger("bridge.local_tools")
lt_log.setLevel(logging.INFO)
lt_log.addHandler(infos)
t0 = time.monotonic()
LocalTools(region="ap-northeast-2", contact_arn=arn, tms_url="mock").run("end_call", esc)
assert 0.5 <= time.monotonic() - t0 < 1.0
assert len(errors.lines) == 2
assert any('"status": "received"' in m and "c-123" in m for m in infos.lines), infos.lines

print("local_tools offline: OK")
