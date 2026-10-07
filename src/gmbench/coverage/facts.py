"""The facts of one front-office week, read straight from the ledger (plus player names from the exports/snapshots).

Week numbering: the week of Monday 2026-09-28 (draft day, opening night on the 29th) is week 1, so the front office
that runs on Monday 2026-10-05 opens week 2 and reports on week 1's games.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from gmbench.config import LeagueConfig, call_name
from gmbench.state import LeagueState, apply

SEASON_WEEK1 = date(2026, 9, 28)


def clean(text: str) -> str:
    """A GM's words as written: drop voice-direction tags ("[confident]", "[pause]") and tidy the spacing."""
    return " ".join(re.sub(r"\[[a-z][a-z \-]*\]", " ", text, flags=re.I).split())


def week_number(week_start: date) -> int:
    return (week_start - SEASON_WEEK1).days // 7 + 1


@dataclass
class Team:
    id: str
    display: str  # model name, e.g. "Gemini 3.1 Pro"
    lab: str
    call: str  # short on-air name, e.g. "Gemini"
    bot: bool
    franchise: str | None
    gm: str | None
    primary: str
    secondary: str
    cup_pick: str | None = None

    @property
    def credit(self) -> str:
        """'Gemini 3.1 Pro (Google)': the model first, always."""
        return f"{self.display} ({self.lab})" if not self.bot else "Autodraft (control bot)"


@dataclass
class Line:
    """Something a GM said this week, in its own words."""
    id: str
    team: str
    kind: str  # chat | presser | pitch | reply
    text: str
    context: str = ""  # for trade talk: who it was said to and about what


@dataclass
class TradeTalk:
    trade_id: str
    proposer: str
    recipient: str
    give: list[int]
    get: list[int]
    message: str
    status: str  # executed | rejected | voided | open | countered | accepted
    counter_of: str | None
    replies: list[dict[str, Any]] = field(default_factory=list)
    void_reason: str | None = None


@dataclass
class WeekFacts:
    week_start: date
    number: int
    trades_open: bool
    lock_at: str | None
    locked: bool
    teams: dict[str, Team]
    standings: list[dict[str, Any]]  # rank, team, points, week_points, prev_rank
    results_dates: list[str]  # the game dates the "last week" numbers cover
    top_players: list[dict[str, Any]]  # best counted performances last week
    trades: list[TradeTalk]
    waivers: list[dict[str, Any]]  # won claims
    waiver_claims: int
    lines: list[Line]
    names: dict[int, dict[str, Any]]  # player id -> {name, pos, nhl}
    rosters: dict[str, list[int]]
    season: dict[int, dict[str, int]] = field(default_factory=dict)  # moved players: gp/goals/assists/wins to Sunday

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)

    @property
    def executed(self) -> list[TradeTalk]:
        return [t for t in self.trades if t.status == "executed"]

    def name(self, pid: int) -> str:
        return (self.names.get(int(pid)) or {}).get("name") or f"Player {pid}"

    def label(self, pid: int) -> str:
        p = self.names.get(int(pid)) or {}
        bits = [b for b in (p.get("pos"), p.get("nhl")) if b]
        return f"{self.name(pid)} ({', '.join(bits)})" if bits else self.name(pid)


def _names(root: Path, run_dir: Path, state: LeagueState) -> dict[int, dict[str, Any]]:
    names: dict[int, dict[str, Any]] = {}
    for pk in state.picks:
        names[int(pk["player_id"])] = {"name": pk["player_name"], "pos": pk["position"], "nhl": pk["nhl_team"]}
    try:
        tracker = json.loads((run_dir / "exports" / "tracker.json").read_text())
        for pid, p in tracker.get("players", {}).items():
            if p.get("name"):
                names.setdefault(int(pid), {"name": p["name"], "pos": p.get("pos"), "nhl": p.get("nhl")})
    except (OSError, ValueError):
        pass
    ref = (state.snapshot or {}).get("ref")
    if ref and (root / ref).exists():
        try:
            snap = json.loads((root / ref).read_text())
            for pid, p in (snap.get("players") or {}).items():
                names.setdefault(int(pid), {"name": p.get("name"), "pos": p.get("position"), "nhl": p.get("team")})
        except (OSError, ValueError):
            pass
    return names


def _team_meta(state: LeagueState, cfg: LeagueConfig) -> dict[str, Team]:
    out = {}
    for tid, t in state.teams.items():
        spec = cfg.team(tid)
        p = t.persona or {}
        out[tid] = Team(id=tid, display=spec.display or tid, lab=spec.lab, call=call_name(tid), bot=t.is_bot,
                        franchise=p.get("franchise_name"), gm=p.get("gm_name"),
                        primary=p.get("primary_color") or ("#4b5475" if t.is_bot else "#2a3566"),
                        secondary=p.get("secondary_color") or "#8fa8ea", cup_pick=p.get("cup_pick"))
    return out


def week_facts(events: list[dict[str, Any]], cfg: LeagueConfig, *, week_start: date, root: Path, run_dir: Path) -> WeekFacts:
    """Facts for the front office of ``week_start``: standings and results through the Sunday before, plus everything
    the GMs did and said in that week's front office."""
    wk = week_start.isoformat()
    cutoff = (week_start - timedelta(days=1)).isoformat()  # last game date in the results
    prev_cutoff = (week_start - timedelta(days=8)).isoformat()

    state = LeagueState()
    opened: dict[str, Any] | None = None
    locked = False
    claims = 0
    for e in events:
        apply(state, e)
        if e["type"] == "WEEK_OPENED" and e["payload"]["week"] == wk:
            opened = e["payload"]
        elif e["type"] == "WEEK_LOCKED" and e["payload"]["week"] == wk:
            locked = True
        elif e["type"] == "WAIVER_CLAIMS_SUBMITTED" and e["payload"]["week"] == wk:
            claims += len(e["payload"]["claims"])

    def totals(through: str) -> dict[str, int]:
        out = {t: 0 for t in state.teams}
        for day, body in state.scores.items():
            if day <= through:
                for t, pts in body["team_points"].items():
                    out[t] = out.get(t, 0) + int(pts)
        return out

    def ranks(pts: dict[str, int]) -> dict[str, int]:
        return {t: 1 + sum(1 for o in pts.values() if o > p) for t, p in pts.items()}

    now_pts, prev_pts = totals(cutoff), totals(prev_cutoff)
    now_rank, prev_rank = ranks(now_pts), ranks(prev_pts)
    results_dates = sorted(d for d in state.scores if prev_cutoff < d <= cutoff)
    standings = [{"rank": now_rank[t], "team": t, "points": now_pts[t], "week_points": now_pts[t] - prev_pts.get(t, 0),
                  "prev_rank": prev_rank.get(t) if any(prev_pts.values()) else None}
                 for t in sorted(state.teams, key=lambda t: (-now_pts[t], -(now_pts[t] - prev_pts.get(t, 0)), t))]

    perf: dict[tuple[str, int], dict[str, Any]] = {}
    for day in results_dates:
        for ln in state.scores[day]["lines"]:
            if not ln.get("active") or not int(ln["counted"]):
                continue
            k = (ln["team"], int(ln["player_id"]))
            row = perf.setdefault(k, {"team": ln["team"], "pid": int(ln["player_id"]), "pts": 0, "goals": 0, "assists": 0,
                                      "wins": 0, "shutouts": 0, "gp": 0})
            row["pts"] += int(ln["counted"])
            row["gp"] += 1
            for src, dst in (("goals", "goals"), ("assists", "assists"), ("win", "wins"), ("shutout", "shutouts")):
                row[dst] += int(ln.get(src) or 0)
    top_players = sorted(perf.values(), key=lambda r: (-r["pts"], -r["goals"], r["pid"]))[:5]

    trades = []
    for t in sorted((t for t in state.trades.values() if t["week"] == wk), key=lambda t: t["trade_id"]):
        trades.append(TradeTalk(trade_id=t["trade_id"], proposer=t["proposer"], recipient=t["recipient"],
                                give=[int(x) for x in t["give"]], get=[int(x) for x in t["get"]],
                                message=t.get("message") or "", status=t["status"], counter_of=t.get("counter_of"),
                                replies=list(t.get("responses") or [])))
    for e in events:
        if e["type"] == "TRADE_VOIDED":
            for t in trades:
                if t.trade_id == e["payload"]["trade_id"]:
                    t.void_reason = e["payload"].get("reason")
    waivers = [x for x in state.transactions if x["kind"] == "waiver" and x["week"] == wk]

    names = _names(root, run_dir, state)
    lines: list[Line] = []
    for c in state.chat:
        if c["week"] == wk:
            lines.append(Line(id=f"chat-{c['seq']}", team=c["team"], kind="chat", text=clean(c["message"])))
    for r in state.reports:
        if r.get("kind") == "presser" and r.get("week") == wk:
            lines.append(Line(id=f"press-{r['seq']}", team=r["team"], kind="presser", text=clean(r["line"])))
    meta = _team_meta(state, cfg)

    def deal(t: TradeTalk) -> str:
        give = ", ".join(names.get(p, {}).get("name") or str(p) for p in t.give)
        get = ", ".join(names.get(p, {}).get("name") or str(p) for p in t.get)
        return f"{meta[t.proposer].call} offers {give} to {meta[t.recipient].call} for {get}"
    for t in trades:
        if t.message.strip():
            lines.append(Line(id=f"pitch-{t.trade_id}", team=t.proposer, kind="pitch", text=clean(t.message),
                              context=f"trade pitch to {meta[t.recipient].call}: {deal(t)}"))
        for i, r in enumerate(t.replies):
            if (r.get("message") or "").strip():
                lines.append(Line(id=f"reply-{t.trade_id}-{i}", team=r["team"], kind="reply", text=clean(r["message"]),
                                  context=f"{r['action']}s {meta[t.proposer].call}'s offer: {deal(t)}"))

    moved = {p for t in trades for p in t.give + t.get} | {p for w in waivers for p in (w["add"], w["drop"])}
    return WeekFacts(week_start=week_start, number=week_number(week_start),
                     trades_open=bool((opened or {}).get("trades_open")), lock_at=(opened or {}).get("lock_at"),
                     locked=locked, teams=meta, standings=standings, results_dates=results_dates,
                     top_players=top_players, trades=trades, waivers=waivers, waiver_claims=claims, lines=lines,
                     names=names, rosters={t: list(v.roster) for t, v in state.teams.items()},
                     season=_season(run_dir / "data" / "games", moved, cfg.raw["league"]["first_puck_drop_utc"][:10], cutoff))


def _season(games_dir: Path, pids: set[int], since: str, through: str) -> dict[int, dict[str, int]]:
    """Season-to-date lines for the players who moved this week, from the committed per-game files."""
    out = {p: {"gp": 0, "goals": 0, "assists": 0, "wins": 0} for p in pids}
    for path in sorted(games_dir.glob("*.json")) if games_dir.is_dir() else []:
        if since <= path.stem <= through:
            for ln in json.loads(path.read_text()):
                row = out.get(int(ln["player_id"]))
                if row is not None:
                    row["gp"] += 1
                    row["goals"] += int(ln.get("goals") or 0)
                    row["assists"] += int(ln.get("assists") or 0)
                    row["wins"] += int(ln.get("win") or 0)
    return out


def weeks_with_front_office(events: list[dict[str, Any]]) -> list[date]:
    return sorted({date.fromisoformat(e["payload"]["week"]) for e in events if e["type"] == "WEEK_LOCKED"})
