"""Benchmark scorecard: per-GM agentic behaviour and outcomes, computed only from the ledger."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from gmbench.config import LeagueConfig
from gmbench.export.site import SCHEMA_VERSION, _write
from gmbench.ledger import Ledger, utc_now
from gmbench.state import replay


def build_scorecard(ledger: Ledger, cfg: LeagueConfig) -> dict[str, Any]:
    state = replay(ledger.events())
    sessions: dict[str, list[dict]] = defaultdict(list)
    for e in ledger.events({"SESSION_COMPLETED"}):
        sessions[e["payload"]["team_id"]].append(e["payload"])
    violations: dict[str, int] = defaultdict(int)
    for v in state.violations:
        violations[v["team"]] += 1
    points = state.points()
    rows = []
    for tid in state.order or list(state.teams):
        spec = cfg.team(tid)
        ss = sessions.get(tid, [])
        picks = [p for p in state.picks if p["team"] == tid]
        draft_ss = [s for s in ss if s.get("kind") == "draft_pick"]
        calls = sum(s["calls"] for s in ss)
        tools = sum(s["tool_calls"] for s in ss)
        invalid = sum(s["invalid_tool_calls"] for s in ss)
        pick_lat = [s["model_latency_s"] for s in draft_ss if s["outcome"] == "ok"]
        rows.append({
            "team": tid, "display": spec.display, "lab": spec.lab, "model": spec.model,
            "franchise": (state.teams[tid].persona or {}).get("franchise_name"),
            "points": points.get(tid, 0),
            "picks": len(picks),
            "autopicks": sum(1 for p in picks if p.get("auto") and p["auto"]["reason"] != "control_bot"),
            "sessions": len(ss),
            "sessions_ok": sum(1 for s in ss if s["outcome"] == "ok"),
            "llm_calls": calls,
            "tool_calls": tools,
            "tool_calls_per_pick": round(sum(s["tool_calls"] for s in draft_ss) / len(draft_ss), 2) if draft_ss else None,
            "invalid_tool_call_rate": round(invalid / tools, 4) if tools else None,
            "budget_refusals": sum(s.get("budget_refusals", 0) for s in ss),
            "nudges": sum(s["nudges"] for s in ss),
            "median_pick_latency_s": round(median(pick_lat), 1) if pick_lat else None,
            "reasoning_tokens": sum(s["tokens"]["reasoning"] for s in ss),
            "prompt_tokens": sum(s["tokens"]["prompt"] for s in ss),
            "cached_tokens": sum(s["tokens"]["cached"] for s in ss),
            "completion_tokens": sum(s["tokens"]["completion"] for s in ss),
            "cost_usd": round(sum(s["cost_usd"] for s in ss), 4),
            "violations": violations.get(tid, 0),
            "tools_by_name": _sum_tools(ss),
            "served_models": sorted({m for s in ss for m in s.get("served_models", [])}),
            "providers": sorted({p for s in ss for p in s.get("providers", [])}),
            "is_bot": state.teams[tid].is_bot,
        })
    return {"schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "ledger_head_seq": state.head_seq, "gms": rows}


def write_scorecard(ledger: Ledger, cfg: LeagueConfig, out_dir: Path) -> Path:
    path = out_dir / "scorecard.json"
    _write(path, build_scorecard(ledger, cfg))
    return path


def _sum_tools(sessions: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in sessions:
        for name, n in (s.get("tools_by_name") or {}).items():
            out[name] = out.get(name, 0) + n
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
