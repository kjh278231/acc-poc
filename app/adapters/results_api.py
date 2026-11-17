"""Results dashboard API: benchmark runs (summary, raw drill-down, charts, reports) and on-demand pytest runs.

Local tool: binds with the main app (run uvicorn on 127.0.0.1). The tests endpoint runs pytest in a subprocess
with LLM_MODE=mock, so it never spends API money.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import ROOT

RESULTS = ROOT / "results"
RAW = RESULTS / "raw"
TESTS = RESULTS / "tests"
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-mock)?$")
CHART = re.compile(r"^[a-z0-9_]+\.png$")

_test_lock = asyncio.Lock()


def _run_dir(run_id: str) -> Path:
    if not RUN_ID.match(run_id) or not (RAW / run_id / "meta.json").exists():
        raise HTTPException(404, "no such run")
    return RAW / run_id


def _jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _summary(run_id: str) -> dict[str, Any]:
    _run_dir(run_id)
    f = RESULTS / run_id / "summary.json"
    if not f.exists():
        from app.benchmark.metrics import build_report
        build_report(RAW / run_id)
    return json.loads(f.read_text(encoding="utf-8"))


def parse_junit(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    cases = []
    for tc in suite.iter("testcase"):
        status, message = "passed", None
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = ((el.get("message") or "") + "\n" + (el.text or "")).strip()[:4000]
        parts = tc.get("classname", "").split(".")
        if len(parts) > 1 and parts[-1][:1].isupper():  # tests.test_a.TestX -> tests/test_a.py
            parts = parts[:-1]
        cases.append({"file": "/".join(parts) + ".py" if parts[0] else "",
                      "name": re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), tc.get("name") or ""), "time_s": float(tc.get("time") or 0), "status": status, "message": message})
    counts = {s: sum(1 for c in cases if c["status"] == s) for s in ("passed", "failed", "error", "skipped")}
    return {"id": path.stem, "timestamp": suite.get("timestamp"), "duration_s": float(suite.get("time") or 0),
            "total": len(cases), **counts, "cases": cases}


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/results")

    @r.get("/runs")
    async def runs() -> list[dict[str, Any]]:
        out = []
        for d in sorted(RAW.glob("*/meta.json"), reverse=True):
            m = json.loads(d.read_text())
            s = RESULTS / m["run_id"] / "summary.json"
            row = {k: m.get(k) for k in ("run_id", "mode", "reps", "started", "finished", "spent_usd", "haiku_model",
                                         "jev_model", "jev_threshold", "parts")}
            row["has_findings"] = (RESULTS / m["run_id"] / "findings.md").exists()
            if s.exists():
                sm = json.loads(s.read_text())
                c = sm.get("conversations") or {}
                row["headline"] = {e: {"frr_p50": (c.get(e) or {}).get("first_response_ready_ms", {}).get("p50"),
                                       "success": (c.get(e) or {}).get("success_rate"),
                                       "cost_per_call": (c.get(e) or {}).get("cost_per_call")} for e in c}
            out.append(row)
        return out

    @r.get("/runs/{run_id}/summary")
    async def summary(run_id: str) -> dict[str, Any]:
        return _summary(run_id)

    @r.post("/runs/{run_id}/rebuild")
    async def rebuild(run_id: str) -> dict[str, Any]:
        from app.benchmark.metrics import build_report
        build_report(_run_dir(run_id))
        return _summary(run_id)

    @r.get("/runs/{run_id}/decisions")
    async def decisions(run_id: str) -> list[dict[str, Any]]:
        rows = _jsonl(_run_dir(run_id) / "decisions.jsonl")
        for x in rows:
            x["models"] = [c["model"] for c in x.pop("calls", [])]
        return rows

    @r.get("/runs/{run_id}/conversations")
    async def conversations(run_id: str) -> list[dict[str, Any]]:
        rows = _jsonl(_run_dir(run_id) / "conversations.jsonl")
        if rows:  # keep the same grader as the report
            from app.benchmark.runner import tool_failure_reported
            for x in rows:
                if x.get("tool_failure_handled") is not None:
                    x["tool_failure_handled"] = tool_failure_reported(x["turns"][-1]["reply"] if x["turns"] else "")
                    x["success"] = x["outcome_ok"] and x["slots_ok"] and x["tms_ok"] and x["tool_failure_handled"]
        return rows

    @r.get("/runs/{run_id}/a2a")
    async def a2a(run_id: str) -> list[dict[str, Any]]:
        return _jsonl(_run_dir(run_id) / "a2a.jsonl")

    @r.get("/runs/{run_id}/doc/{name}")
    async def doc(run_id: str, name: str) -> PlainTextResponse:
        _run_dir(run_id)
        if name not in ("report", "findings"):
            raise HTTPException(404)
        f = RESULTS / run_id / f"{name}.md"
        if not f.exists():
            raise HTTPException(404, f"{name}.md not found")
        return PlainTextResponse(f.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    @r.get("/runs/{run_id}/charts/{file}")
    async def chart(run_id: str, file: str) -> FileResponse:
        _run_dir(run_id)
        f = RESULTS / run_id / "charts" / file
        if not CHART.match(file) or not f.exists():
            raise HTTPException(404)
        return FileResponse(f)

    # ------------------------------------------------------------------ tests
    @r.get("/tests")
    async def test_runs() -> list[dict[str, Any]]:
        out = []
        for f in sorted(TESTS.glob("*.xml"), reverse=True)[:20]:
            t = parse_junit(f)
            t.pop("cases")
            out.append(t)
        return out

    @r.get("/tests/{test_id}")
    async def test_run(test_id: str) -> dict[str, Any]:
        f = TESTS / f"{test_id}.xml"
        if not re.match(r"^[0-9]{8}-[0-9]{6}$", test_id) or not f.exists():
            raise HTTPException(404)
        t = parse_junit(f)
        log = TESTS / f"{test_id}.log"
        t["log"] = log.read_text(encoding="utf-8")[-20000:] if log.exists() else ""
        return t

    @r.post("/tests/run")
    async def run_tests() -> dict[str, Any]:
        if _test_lock.locked():
            raise HTTPException(409, "a test run is already in progress")
        async with _test_lock:
            TESTS.mkdir(parents=True, exist_ok=True)
            tid = time.strftime("%Y%m%d-%H%M%S")
            xml, log = TESTS / f"{tid}.xml", TESTS / f"{tid}.log"
            env = {**os.environ, "LLM_MODE": "mock"}
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", cwd=ROOT, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            except TimeoutError as e:
                proc.kill()
                raise HTTPException(504, "pytest timed out") from e
            log.write_text(out.decode(errors="replace"), encoding="utf-8")
            if not xml.exists():
                raise HTTPException(500, out.decode(errors="replace")[-2000:])
            t = parse_junit(xml)
            t["exit_code"] = proc.returncode
            t["log"] = out.decode(errors="replace")[-20000:]
            return t

    return r
