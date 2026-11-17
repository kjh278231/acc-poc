"""Results dashboard API against a fake results tree."""
import json

import httpx
import pytest

from app.adapters import results_api


@pytest.fixture
def fake_results(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "20260101-000000"
    raw.mkdir(parents=True)
    (raw / "meta.json").write_text(json.dumps({"run_id": "20260101-000000", "mode": "live", "reps": 1}))
    (raw / "decisions.jsonl").write_text(json.dumps({"case": "d01", "engine": "jev_haiku", "calls": [{"model": "m"}]}) + "\n")
    out = tmp_path / "20260101-000000"
    (out / "charts").mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps({"meta": {"run_id": "20260101-000000"}, "conversations": {}}))
    (out / "report.md").write_text("# r")
    (out / "charts" / "cost.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(results_api, "RESULTS", tmp_path)
    monkeypatch.setattr(results_api, "RAW", tmp_path / "raw")
    monkeypatch.setattr(results_api, "TESTS", tmp_path / "tests")
    return tmp_path


@pytest.fixture
def client(fake_results):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(results_api.build_router())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_runs_and_drilldown(client):
    async with client as c:
        runs = (await c.get("/api/results/runs")).json()
        assert runs[0]["run_id"] == "20260101-000000"
        assert (await c.get("/api/results/runs/20260101-000000/summary")).json()["meta"]["run_id"] == "20260101-000000"
        d = (await c.get("/api/results/runs/20260101-000000/decisions")).json()
        assert d[0]["models"] == ["m"] and "calls" not in d[0]
        assert (await c.get("/api/results/runs/20260101-000000/doc/report")).text == "# r"
        assert (await c.get("/api/results/runs/20260101-000000/charts/cost.png")).status_code == 200


async def test_path_traversal_rejected(client):
    async with client as c:
        assert (await c.get("/api/results/runs/..%2F..%2Fetc/summary")).status_code == 404
        assert (await c.get("/api/results/runs/20260101-000000/charts/..%2Fsummary.json")).status_code == 404
        assert (await c.get("/api/results/runs/20260101-000000/doc/secret")).status_code == 404


def test_parse_junit(tmp_path):
    x = tmp_path / "20260101-000000.xml"
    x.write_text('<testsuites><testsuite name="pytest" time="1.5" timestamp="t">'
                 '<testcase classname="tests.test_a" name="ok" time="0.1"/>'
                 '<testcase classname="tests.test_a" name="bad" time="0.2"><failure message="boom">trace</failure></testcase>'
                 '</testsuite></testsuites>')
    t = results_api.parse_junit(x)
    assert (t["total"], t["passed"], t["failed"]) == (2, 1, 1)
    assert t["cases"][1]["file"] == "tests/test_a.py" and "boom" in t["cases"][1]["message"]
