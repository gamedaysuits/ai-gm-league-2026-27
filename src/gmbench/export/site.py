"""Public JSON exports for the league site and the media pipeline (no private notes or third-party text)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from gmbench.config import LeagueConfig
from gmbench.data.models import Snapshot
from gmbench.ledger import utc_now
from gmbench.state import LeagueState

SCHEMA_VERSION = 1


def _write(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def write_draft_export(state: LeagueState, snapshot: Snapshot, cfg: LeagueConfig, out_dir: Path) -> Path:
    teams = []
    for tid in state.order or list(state.teams):
        spec = cfg.team(tid)
        t = state.teams[tid]
        teams.append({
            "id": tid, "model": spec.model, "display": spec.display, "lab": spec.lab,
            "persona": _public_persona(t.persona),
            "roster": [{"player_id": pid, "name": snapshot.players[pid].name, "nhl_team": snapshot.players[pid].team,
                        "position": snapshot.players[pid].position} for pid in t.roster if pid in snapshot.players],
        })
    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "ledger_head_seq": state.head_seq,
        "league": cfg.raw["league"]["name"],
        "order": state.order,
        "rounds": cfg.rules["rounds"],
        "teams": teams,
        "picks": [{k: v for k, v in pk.items() if k != "session"} for pk in state.picks],
        "says": [{k: v for k, v in s.items() if k in ("seq", "team", "kind", "round", "pick_no", "line", "addressed_to")}
                 for s in state.says],
    }
    path = out_dir / "draft.json"
    _write(path, doc)
    return path


def _public_persona(persona: dict[str, Any] | None) -> dict[str, Any] | None:
    if not persona:
        return None
    return {k: v for k, v in persona.items() if not k.startswith("private_")}


def write_standings_export(state: LeagueState, cfg: LeagueConfig, out_dir: Path, *, today: str) -> Path:
    """Standings with totals, last-7-days, and a daily cumulative series per team."""
    days = sorted(state.scores)
    totals = state.points()
    cutoff = max(days[-7:]) if days else None
    recent = {tid: 0 for tid in state.teams}
    if days:
        for d in days[-7:]:
            for tid, pts in state.scores[d]["team_points"].items():
                recent[tid] = recent.get(tid, 0) + int(pts)
    series: dict[str, list[int]] = {tid: [] for tid in state.teams}
    running = {tid: 0 for tid in state.teams}
    for d in days:
        for tid in state.teams:
            running[tid] += int(state.scores[d]["team_points"].get(tid, 0))
            series[tid].append(running[tid])
    rows = sorted(state.teams, key=lambda t: (-totals.get(t, 0), t))
    doc = {
        "schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "as_of": today, "ledger_head_seq": state.head_seq,
        "days_scored": len(days), "last_game_date": cutoff,
        "standings": [{"rank": i + 1, "team": t, "display": cfg.team(t).display, "lab": cfg.team(t).lab,
                       "franchise": (state.teams[t].persona or {}).get("franchise_name"),
                       "gm_name": (state.teams[t].persona or {}).get("gm_name"),
                       "points": totals.get(t, 0), "last_7_days": recent.get(t, 0)} for i, t in enumerate(rows)],
        "series": {"dates": days, "cumulative": series},
    }
    path = out_dir / "standings.json"
    _write(path, doc)
    return path


GRADE_POINTS = {"A+": 4.3, "A": 4.0, "A-": 3.7, "B+": 3.3, "B": 3.0, "B-": 2.7, "C+": 2.3, "C": 2.0, "C-": 1.7, "D": 1.0, "F": 0.0}


def write_grades_export(state: LeagueState, cfg: LeagueConfig, out_dir: Path) -> Path:
    """Consensus draft grades (every GM grades every other GM), spreads, and predicted finishes."""
    reports = [r for r in state.reports if r.get("kind") == "post_draft"]
    received: dict[str, list[dict[str, Any]]] = {t: [] for t in state.teams}
    predicted_rank: dict[str, list[int]] = {t: [] for t in state.teams}
    predicted_by_others: dict[str, list[int]] = {t: [] for t in state.teams}
    for r in reports:
        for g in r["grades"]:
            received.setdefault(g["team"], []).append({"from": r["team"], "grade": g["grade"], "comment": g["comment"]})
        for i, tid in enumerate(r["predicted_standings"], 1):
            predicted_rank.setdefault(tid, []).append(i)
            if tid != r["team"]:
                predicted_by_others.setdefault(tid, []).append(i)
    rows = []
    for tid, grades in received.items():
        pts = [GRADE_POINTS[g["grade"]] for g in grades]
        avg = sum(pts) / len(pts) if pts else None
        rows.append({"team": tid, "display": cfg.team(tid).display,
                     "franchise": (state.teams[tid].persona or {}).get("franchise_name"),
                     "gpa": round(avg, 2) if avg is not None else None,
                     "spread": round(max(pts) - min(pts), 1) if pts else None,
                     "avg_predicted_finish": round(sum(predicted_rank[tid]) / len(predicted_rank[tid]), 1) if predicted_rank[tid] else None,
                     "league_predicted_finish": (round(sum(predicted_by_others[tid]) / len(predicted_by_others[tid]), 2)
                                                 if predicted_by_others[tid] else None),
                     "self_prediction": next((r["predicted_standings"].index(tid) + 1 for r in reports if r["team"] == tid), None),
                     "grades": grades})
    rows.sort(key=lambda r: (-(r["gpa"] or 0), r["team"]))
    doc = {"schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "ledger_head_seq": state.head_seq, "teams": rows,
           "projections": [{"team": r["team"], "projected_points": r["projected_points"], "range_80": r["range_80"]} for r in reports]}
    path = out_dir / "grades.json"
    _write(path, doc)
    return path
