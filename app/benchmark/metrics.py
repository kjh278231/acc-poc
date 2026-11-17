"""Aggregate raw benchmark JSONL into summary.json, charts (PNG) and report.md.

  uv run python -m app.benchmark.metrics results/raw/<run_id>
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from app.config import ROOT  # noqa: E402

ENGINES = ["haiku_only", "jev_haiku"]
LABEL = {"haiku_only": "A. Haiku Only", "jev_haiku": "B. Jev + Haiku"}
COLOR = {"haiku_only": "#2a78d6", "jev_haiku": "#eb6834"}  # dataviz reference palette slots 1-2 (light)
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def lat(xs: list[float]) -> dict[str, float | None]:
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "mean": statistics.fmean(xs) if xs else None}


def rate(n: int, d: int) -> float | None:
    return n / d if d else None


# ---------------------------------------------------------------------- summaries
def summarize_decisions(rows: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for e in ENGINES:
        r = [x for x in rows if x["engine"] == e]
        ok = [x for x in r if not x.get("error")]
        calls = Counter(c["model"] for x in ok for c in x["calls"])
        cats: dict[str, list[bool]] = defaultdict(list)
        for x in ok:
            cats[x["category"]].append(x["correct"])
        nofb = [x for x in ok if not x.get("fallback")]
        fb = [x for x in ok if x.get("fallback")]
        out[e] = {
            "n": len(r), "errors": len(r) - len(ok),
            "accuracy": rate(sum(x["correct"] for x in ok), len(r)),
            "intent_acc": rate(sum(x["intent_ok"] for x in ok), len(r)),
            "action_acc": rate(sum(x["action_ok"] for x in ok), len(r)),
            "slot_acc": rate(sum(x["slot_ok"] for x in ok), len(r)),
            "decision_ms": lat([x["decision_ms"] for x in ok]),
            "decision_ms_no_fallback": lat([x["decision_ms"] for x in nofb]),
            "decision_ms_fallback": lat([x["decision_ms"] for x in fb]),
            "accuracy_no_fallback": rate(sum(x["correct"] for x in nofb), len(nofb)),
            "accuracy_fallback": rate(sum(x["correct"] for x in fb), len(fb)),
            "ttft_ms": lat([x.get("ttft_ms") for x in ok]),
            "fallback_rate": rate(len(fb), len(ok)) if e == "jev_haiku" else None,
            "fallback_reasons": dict(Counter(x["fallback_reason"] for x in fb)),
            "in_tokens_mean": statistics.fmean([x["input_tokens"] for x in ok]) if ok else None,
            "out_tokens_mean": statistics.fmean([x["output_tokens"] for x in ok]) if ok else None,
            "cost_per_decision": statistics.fmean([x["cost_usd"] for x in ok]) if ok else None,
            "cost_actual_share": rate(sum(x["cost_actual"] for x in ok), len(ok)),
            "calls_by_model": dict(calls),
            "by_category": {k: rate(sum(v), len(v)) for k, v in sorted(cats.items())},
            "failures": sorted({(x["case"], x["text"], x["intent"], x["action"], json.dumps(x["slots"], ensure_ascii=False))
                                for x in ok if not x["correct"]}),
        }
    return out


def summarize_conversations(rows: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for e in ENGINES:
        r = [x for x in rows if x["engine"] == e]
        turns = [t for x in r for t in x["turns"]]
        tool_turns = [t for t in turns if t["tool_ms"]]
        succ = [x for x in r if x["success"]]
        cats: dict[str, list[bool]] = defaultdict(list)
        scen: dict[str, list[bool]] = defaultdict(list)
        for x in r:
            cats[x["category"]].append(x["success"])
            scen[x["scenario"]].append(x["success"])
        calls = Counter()
        for t in turns:
            calls.update(t["calls_by_model"])
        cost_total = sum(x["cost_usd"] for x in r)
        cost_per_call = cost_total / len(r) if r else None
        cost_per_success = cost_total / len(succ) if succ else None
        tf = [x for x in r if x["tool_failure_handled"] is not None]
        out[e] = {
            "n_calls": len(r), "n_turns": len(turns),
            "success_rate": rate(len(succ), len(r)),
            "outcome_acc": rate(sum(x["outcome_ok"] for x in r), len(r)),
            "slots_acc": rate(sum(x["slots_ok"] for x in r), len(r)),
            "wrong_termination": sum(x["wrong_termination"] for x in r),
            "stuck": sum(x["stuck"] for x in r),
            "reprompts_per_call": statistics.fmean([x["reprompts"] for x in r]) if r else None,
            "turns_per_call": statistics.fmean([x["n_turns"] for x in r]) if r else None,
            "intent_acc_turn": rate(sum(x["intent_ok"] for x in r), sum(x["intent_n"] for x in r)),
            "tool_failure_handled": rate(sum(bool(x["tool_failure_handled"]) for x in tf), len(tf)),
            "scenario_ms": lat([x["scenario_ms"] for x in r]),
            "first_response_ready_ms": lat([t["first_response_ready_ms"] for t in turns]),
            "first_response_ready_ms_tool_turns": lat([t["first_response_ready_ms"] for t in tool_turns]),
            "turn_total_ms": lat([t["turn_total_ms"] for t in turns]),
            "decision_ms": lat([t["decision_ms"] for t in turns]),
            "ttft_ms": lat([t["ttft_ms"] for t in turns]),
            "tool_ms": lat([t["tool_ms"] for t in tool_turns]),
            "fallback_rate_turn": rate(sum(t["fallback"] for t in turns), len(turns)) if e == "jev_haiku" else None,
            "fallback_reasons": dict(Counter(t["fallback_reason"] for t in turns if t["fallback"])),
            "model_errors": sum(len(t["model_errors"]) for t in turns),
            "calls_by_model": dict(calls),
            "calls_per_call": {m: c / len(r) for m, c in calls.items()} if r else {},
            "in_tokens_per_call": statistics.fmean([x["input_tokens"] for x in r]) if r else None,
            "out_tokens_per_call": statistics.fmean([x["output_tokens"] for x in r]) if r else None,
            "cost_per_turn": cost_total / len(turns) if turns else None,
            "cost_per_call": cost_per_call,
            "cost_per_completed_call": cost_per_success,
            "cost_1k_calls": cost_per_call * 1000 if cost_per_call is not None else None,
            "cost_10k_calls": cost_per_call * 10000 if cost_per_call is not None else None,
            "cost_actual_share": rate(sum(t["cost_actual"] for t in turns), len(turns)),
            "by_category": {k: rate(sum(v), len(v)) for k, v in sorted(cats.items())},
            "by_scenario": {k: rate(sum(v), len(v)) for k, v in sorted(scen.items())},
            "failed_examples": [{"scenario": x["scenario"], "outcome": x["outcome"], "slots": x["slots"],
                                 "turns": [(t["input"], t["action"], t["reply"]) for t in x["turns"]]}
                                for x in r if not x["success"]][:6],
        }
    return out


def summarize_a2a(rows: list[dict], conv: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for e in ENGINES:
        r = [x for x in rows if x["engine"] == e]
        turns = [t for x in r for t in x["turns"]]
        ids = {x["scenario"] for x in r}
        direct = [t for x in conv if x["engine"] == e and x["scenario"] in ids for t in x["turns"]]
        out[e] = {
            "n_calls": len(r), "n_turns": len(turns),
            "violations": sum(len(x["violations"]) for x in r),
            "violation_examples": [v for x in r for v in x["violations"]][:5],
            "outcome_acc": rate(sum(x["outcome_ok"] for x in r), len(r)),
            "traced_share": rate(sum(t["traced"] for t in turns), len(turns)),
            "connect_ms": lat([x["connect_ms"] for x in r]),
            "init_ms": lat([x["init_ms"] for x in r]),
            "greeting_ms": lat([x["greeting_ms"] for x in r]),
            "first_chunk_ms": lat([t["first_chunk_ms"] for t in turns]),
            "turn_ms": lat([t["turn_ms"] for t in turns]),
            "server_ms": lat([t["server_ms"] for t in turns]),
            "overhead_ms": lat([t["overhead_ms"] for t in turns]),
            "direct_first_response_ready_ms": lat([t["first_response_ready_ms"] for t in direct]),
            "direct_turn_total_ms": lat([t["turn_total_ms"] for t in direct]),
        }
    return out


# ---------------------------------------------------------------------- charts
def _style(ax, title: str, ylabel: str | None = None) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=28)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK2, fontsize=9)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _grouped(ax, groups: list[str], values: dict[str, list[float | None]], fmt: str) -> None:
    w = 0.36
    for i, e in enumerate(ENGINES):
        xs = [g + (i - 0.5) * (w + 0.02) for g in range(len(groups))]
        vals = [v if v is not None else 0 for v in values[e]]
        bars = ax.bar(xs, vals, width=w, color=COLOR[e], label=LABEL[e])
        for b, v in zip(bars, values[e]):
            if v is not None:
                ax.annotate(fmt.format(v), (b.get_x() + b.get_width() / 2, b.get_height()), ha="center", va="bottom",
                            fontsize=8, color=INK2, xytext=(0, 2), textcoords="offset points")
    ax.set_xticks(range(len(groups)), groups)
    ax.legend(frameon=False, fontsize=9, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2)
    ax.margins(y=0.12)


def charts(s: dict[str, Any], out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    files = []
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    d, c, a = s.get("decisions"), s.get("conversations"), s.get("a2a")

    if c:
        fig, ax = plt.subplots(figsize=(8, 4.2))
        groups = ["Decision P50", "Decision P95", "First response P50", "First response P95", "Turn total P50", "Turn total P95"]
        vals = {e: [c[e]["decision_ms"]["p50"], c[e]["decision_ms"]["p95"], c[e]["first_response_ready_ms"]["p50"],
                    c[e]["first_response_ready_ms"]["p95"], c[e]["turn_total_ms"]["p50"], c[e]["turn_total_ms"]["p95"]] for e in ENGINES}
        _grouped(ax, groups, vals, "{:.0f}")
        _style(ax, "Per-turn latency in full calls (ms, lower is better)", "ms")
        ax.tick_params(axis="x", labelsize=8)
        fig.tight_layout()
        fig.savefig(out / "latency_turn.png", dpi=150)
        plt.close(fig)
        files.append("latency_turn.png")

        fig, ax = plt.subplots(figsize=(8, 4))
        data = [[t for t in s["_raw_frr"][e] if t is not None] for e in ENGINES]
        bp = ax.boxplot(data, orientation="horizontal", widths=0.45, patch_artist=True, showfliers=True,
                        medianprops={"color": INK, "linewidth": 1.5}, flierprops={"markersize": 3, "markeredgecolor": INK2})
        for patch, e in zip(bp["boxes"], ENGINES):
            patch.set_facecolor(COLOR[e])
            patch.set_edgecolor(SURFACE)
        ax.set_yticks([1, 2], [LABEL[e] for e in ENGINES])
        _style(ax, "Time to first response ready, all turns (ms)")
        ax.xaxis.grid(True, color=GRID)
        ax.yaxis.grid(False)
        fig.tight_layout()
        fig.savefig(out / "latency_distribution.png", dpi=150)
        plt.close(fig)
        files.append("latency_distribution.png")

        fig, ax = plt.subplots(figsize=(8, 4))
        groups = ["Cost / turn (x1000)", "Cost / call (x1000)", "Cost / completed call (x1000)"]
        vals = {e: [x * 1000 if x is not None else None for x in
                    (c[e]["cost_per_turn"], c[e]["cost_per_call"], c[e]["cost_per_completed_call"])] for e in ENGINES}
        _grouped(ax, groups, vals, "{:.3f}")
        _style(ax, "Model API cost (USD x 1000; i.e. USD per 1,000 units)", "USD x 1000")
        fig.tight_layout()
        fig.savefig(out / "cost.png", dpi=150)
        plt.close(fig)
        files.append("cost.png")

        cats = sorted(set(c["haiku_only"]["by_category"]) | set(c["jev_haiku"]["by_category"]))
        fig, ax = plt.subplots(figsize=(9, 4))
        vals = {e: [100 * (c[e]["by_category"].get(k) or 0) for k in cats] for e in ENGINES}
        _grouped(ax, cats, vals, "{:.0f}")
        ax.set_ylim(0, 115)
        _style(ax, "Call success by scenario category (%)", "%")
        ax.tick_params(axis="x", labelsize=8, rotation=20)
        fig.tight_layout()
        fig.savefig(out / "success_by_category.png", dpi=150)
        plt.close(fig)
        files.append("success_by_category.png")

    if d or c:
        fig, ax = plt.subplots(figsize=(8, 4))
        groups, vals = [], {e: [] for e in ENGINES}
        if d:
            groups += ["Decision: intent", "Decision: action", "Decision: slots", "Decision: all"]
            for e in ENGINES:
                vals[e] += [100 * (d[e][k] or 0) for k in ("intent_acc", "action_acc", "slot_acc", "accuracy")]
        if c:
            groups += ["Call success"]
            for e in ENGINES:
                vals[e].append(100 * (c[e]["success_rate"] or 0))
        _grouped(ax, groups, vals, "{:.0f}")
        ax.set_ylim(0, 115)
        _style(ax, "Accuracy (%)", "%")
        ax.tick_params(axis="x", labelsize=8)
        fig.tight_layout()
        fig.savefig(out / "accuracy.png", dpi=150)
        plt.close(fig)
        files.append("accuracy.png")

    if a:
        fig, ax = plt.subplots(figsize=(8, 4))
        groups = ["Init P50", "Server turn P50", "Client turn P50", "A2A overhead P50", "A2A overhead P95"]
        vals = {e: [a[e]["init_ms"]["p50"], a[e]["server_ms"]["p50"], a[e]["turn_ms"]["p50"], a[e]["overhead_ms"]["p50"],
                    a[e]["overhead_ms"]["p95"]] for e in ENGINES}
        _grouped(ax, groups, vals, "{:.1f}")
        _style(ax, "Local A2A (WebSocket) timings (ms)", "ms")
        ax.tick_params(axis="x", labelsize=8)
        fig.tight_layout()
        fig.savefig(out / "a2a.png", dpi=150)
        plt.close(fig)
        files.append("a2a.png")
    return files


# ---------------------------------------------------------------------- report
def f_ms(x) -> str:
    return "–" if x is None else f"{x:,.0f}"


def f_pct(x) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def f_usd(x, digits=6) -> str:
    return "–" if x is None else f"${x:.{digits}f}"


def build_report(raw: Path) -> str:
    raw = Path(raw)
    meta = json.loads((raw / "meta.json").read_text())
    dec, conv, a2a = load(raw / "decisions.jsonl"), load(raw / "conversations.jsonl"), load(raw / "a2a.jsonl")
    from app.benchmark.runner import tool_failure_reported
    for x in conv:  # re-score with the current grader so older raw runs stay comparable
        if x.get("tool_failure_handled") is not None:
            x["tool_failure_handled"] = tool_failure_reported(x["turns"][-1]["reply"] if x["turns"] else "")
            x["success"] = x["outcome_ok"] and x["slots_ok"] and x["tms_ok"] and x["tool_failure_handled"]
    s: dict[str, Any] = {"meta": meta}
    if dec:
        s["decisions"] = summarize_decisions(dec)
    if conv:
        s["conversations"] = summarize_conversations(conv)
        s["_raw_frr"] = {e: [t["first_response_ready_ms"] for x in conv if x["engine"] == e for t in x["turns"]] for e in ENGINES}
    if a2a:
        s["a2a"] = summarize_a2a(a2a, conv)
    out = ROOT / "results" / meta["run_id"]
    files = charts(s, out / "charts")
    s.pop("_raw_frr", None)
    (out / "summary.json").write_text(json.dumps(s, indent=2, ensure_ascii=False, default=str))
    md = render_md(s, files)
    (out / "report.md").write_text(md, encoding="utf-8")
    return f"report: {out / 'report.md'}"


def render_md(s: dict[str, Any], files: list[str]) -> str:
    m = s["meta"]
    L = [f"# Benchmark report — {m['run_id']}", ""]
    if m["mode"] == "mock":
        L += ["> **MOCK RUN** — offline stand-in models. Numbers below are NOT model performance.", ""]
    L += [f"- Mode: **{m['mode']}**, reps: {m['reps']}, seed: {m['seed']}, concurrency: {m['concurrency']}",
          f"- Haiku: `{m['haiku_model']}` (reasoning={m['haiku_reasoning']}); Jev: `{m['jev_model']}`, threshold {m['jev_threshold']}",
          f"- TMS mock latency: {m['tms_latency_ms']} ms; spent: ${m.get('spent_usd', 0):.4f}; {m['started']} → {m.get('finished')}", ""]
    d, c, a = s.get("decisions"), s.get("conversations"), s.get("a2a")
    if d:
        L += ["## 1. Pure decision benchmark (engine + rule engine, no tools, no adapter)", "",
              "| Metric | A. Haiku Only | B. Jev + Haiku |", "|---|---:|---:|"]
        rows = [("Decisions (n)", lambda x: str(x["n"])), ("Errors", lambda x: str(x["errors"])),
                ("Accuracy (intent+action+slots)", lambda x: f_pct(x["accuracy"])),
                ("Intent accuracy", lambda x: f_pct(x["intent_acc"])), ("Action accuracy", lambda x: f_pct(x["action_acc"])),
                ("Slot accuracy", lambda x: f_pct(x["slot_acc"])),
                ("Decision time P50 / P95 (ms)", lambda x: f"{f_ms(x['decision_ms']['p50'])} / {f_ms(x['decision_ms']['p95'])}"),
                ("Decision time P50, no fallback (ms)", lambda x: f_ms(x["decision_ms_no_fallback"]["p50"])),
                ("Decision time P50, with fallback (ms)", lambda x: f_ms(x["decision_ms_fallback"]["p50"])),
                ("Time to first token P50 (ms)", lambda x: f_ms(x["ttft_ms"]["p50"])),
                ("Fallback rate", lambda x: f_pct(x["fallback_rate"])),
                ("Accuracy without / with fallback", lambda x: f"{f_pct(x['accuracy_no_fallback'])} / {f_pct(x['accuracy_fallback'])}"),
                ("Input / output tokens per decision", lambda x: f"{x['in_tokens_mean'] or 0:.0f} / {x['out_tokens_mean'] or 0:.0f}"),
                ("Cost per decision", lambda x: f_usd(x["cost_per_decision"], 7)),
                ("API calls by model", lambda x: ", ".join(f"{k}: {v}" for k, v in x["calls_by_model"].items()))]
        for name, fn in rows:
            L.append(f"| {name} | {fn(d['haiku_only'])} | {fn(d['jev_haiku'])} |")
        L += ["", f"Fallback reasons (B): {d['jev_haiku']['fallback_reasons'] or 'none'}", "",
              "Accuracy by category:", "", "| Category | A | B |", "|---|---:|---:|"]
        for k in sorted(set(d["haiku_only"]["by_category"]) | set(d["jev_haiku"]["by_category"])):
            L.append(f"| {k} | {f_pct(d['haiku_only']['by_category'].get(k))} | {f_pct(d['jev_haiku']['by_category'].get(k))} |")
        for e in ENGINES:
            if d[e]["failures"]:
                L += ["", f"Incorrect decisions — {LABEL[e]} (case, text, intent, action, slots):", ""]
                L += [f"- {f[0]} `{f[1]}` → {f[2]}, {f[3]}, {f[4]}" for f in d[e]["failures"][:12]]
        L.append("")
    if c:
        L += ["## 2. Full conversation benchmark (direct orchestrator calls)", "",
              "| Metric | A. Haiku Only | B. Jev + Haiku |", "|---|---:|---:|"]
        rows = [("Calls / turns", lambda x: f"{x['n_calls']} / {x['n_turns']}"),
                ("Call success (outcome + slots + TMS)", lambda x: f_pct(x["success_rate"])),
                ("Outcome correct", lambda x: f_pct(x["outcome_acc"])), ("Slots correct", lambda x: f_pct(x["slots_acc"])),
                ("Wrong terminations", lambda x: str(x["wrong_termination"])), ("Stuck (max turns)", lambda x: str(x["stuck"])),
                ("Re-asks per call", lambda x: f"{x['reprompts_per_call']:.2f}"), ("Turns per call", lambda x: f"{x['turns_per_call']:.2f}"),
                ("Intent accuracy (per turn)", lambda x: f_pct(x["intent_acc_turn"])),
                ("TMS failure reported correctly", lambda x: f_pct(x["tool_failure_handled"])),
                ("Model decision P50 / P95 (ms)", lambda x: f"{f_ms(x['decision_ms']['p50'])} / {f_ms(x['decision_ms']['p95'])}"),
                ("Time to first token P50 (ms)", lambda x: f_ms(x["ttft_ms"]["p50"])),
                ("**First response ready P50 / P95 (ms)**", lambda x: f"**{f_ms(x['first_response_ready_ms']['p50'])} / {f_ms(x['first_response_ready_ms']['p95'])}**"),
                ("First response ready on tool turns P50 (ms)", lambda x: f_ms(x["first_response_ready_ms_tool_turns"]["p50"])),
                ("Turn total P50 / P95 (ms)", lambda x: f"{f_ms(x['turn_total_ms']['p50'])} / {f_ms(x['turn_total_ms']['p95'])}"),
                ("Tool (TMS) time P50 (ms)", lambda x: f_ms(x["tool_ms"]["p50"])),
                ("Scenario completion P50 / P95 (ms, agent time only)", lambda x: f"{f_ms(x['scenario_ms']['p50'])} / {f_ms(x['scenario_ms']['p95'])}"),
                ("Fallback rate (turns)", lambda x: f_pct(x["fallback_rate_turn"])),
                ("Model errors", lambda x: str(x["model_errors"])),
                ("API calls per call, by model", lambda x: ", ".join(f"{k}: {v:.2f}" for k, v in x["calls_per_call"].items())),
                ("Input / output tokens per call", lambda x: f"{x['in_tokens_per_call']:.0f} / {x['out_tokens_per_call']:.0f}"),
                ("Cost per turn", lambda x: f_usd(x["cost_per_turn"], 7)), ("Cost per call", lambda x: f_usd(x["cost_per_call"], 6)),
                ("Cost per completed call", lambda x: f_usd(x["cost_per_completed_call"], 6)),
                ("Cost for 1,000 calls", lambda x: f_usd(x["cost_1k_calls"], 3)), ("Cost for 10,000 calls", lambda x: f_usd(x["cost_10k_calls"], 2)),
                ("Share of cost from provider-reported usage", lambda x: f_pct(x["cost_actual_share"]))]
        for name, fn in rows:
            L.append(f"| {name} | {fn(c['haiku_only'])} | {fn(c['jev_haiku'])} |")
        L += ["", f"Fallback reasons (B): {c['jev_haiku']['fallback_reasons'] or 'none'}", "",
              "Success by scenario:", "", "| Scenario | A | B |", "|---|---:|---:|"]
        for k in sorted(c["haiku_only"]["by_scenario"]):
            L.append(f"| {k} | {f_pct(c['haiku_only']['by_scenario'].get(k))} | {f_pct(c['jev_haiku']['by_scenario'].get(k))} |")
        for e in ENGINES:
            if c[e]["failed_examples"]:
                L += ["", f"Failed calls — {LABEL[e]} (first {len(c[e]['failed_examples'])}):", ""]
                for f in c[e]["failed_examples"]:
                    L.append(f"- **{f['scenario']}** outcome={f['outcome']} slots={json.dumps(f['slots'], ensure_ascii=False)}")
                    L += [f"  - `{t[0]}` → {t[1]}: {t[2]}" for t in f["turns"]]
        L += ["", "Costs are model API only (OpenRouter). Amazon Connect, STT/TTS, telephony and hosting are excluded — see docs/cost-model.md.", ""]
    if a:
        L += ["## 3. A2A adapter vs direct (local WebSocket, mock Connect client)", "",
              "| Metric | A. Haiku Only | B. Jev + Haiku |", "|---|---:|---:|"]
        rows = [("Calls / turns", lambda x: f"{x['n_calls']} / {x['n_turns']}"),
                ("Protocol violations", lambda x: str(x["violations"])), ("Turns with TRACING_SPAN", lambda x: f_pct(x["traced_share"])),
                ("FINISH type correct", lambda x: f_pct(x["outcome_acc"])),
                ("WS connect P50 (ms)", lambda x: f_ms(x["connect_ms"]["p50"])),
                ("INIT_SESSION → response P50 (ms)", lambda x: f"{x['init_ms']['p50']:.2f}" if x["init_ms"]["p50"] is not None else "–"),
                ("First chunk at client P50 (ms)", lambda x: f_ms(x["first_chunk_ms"]["p50"])),
                ("Turn at client P50 / P95 (ms)", lambda x: f"{f_ms(x['turn_ms']['p50'])} / {f_ms(x['turn_ms']['p95'])}"),
                ("Turn inside server P50 (ms)", lambda x: f_ms(x["server_ms"]["p50"])),
                ("A2A overhead P50 / P95 (ms)", lambda x: f"{x['overhead_ms']['p50']:.1f} / {x['overhead_ms']['p95']:.1f}" if x["overhead_ms"]["p50"] is not None else "–"),
                ("Direct (same scenarios) turn total P50 (ms)", lambda x: f_ms(x["direct_turn_total_ms"]["p50"]))]
        for name, fn in rows:
            L.append(f"| {name} | {fn(a['haiku_only'])} | {fn(a['jev_haiku'])} |")
        L += ["", "Overhead = client-observed turn time − server root span (`invoke_agent`) duration: JSON-RPC framing, "
              "trace serialization and loopback transport. Real Connect adds WAN latency and Connect-side processing that "
              "only a Phase 2 test in AWS can measure.", ""]
    if files:
        L += ["## Charts", ""] + [f"![{f}](charts/{f})" for f in files] + [""]
    return "\n".join(L)


if __name__ == "__main__":
    print(build_report(Path(sys.argv[1])))
