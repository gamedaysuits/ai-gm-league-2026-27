"""Daily scoring: official NHL game lines -> SCORE_DAY events, rescoring a trailing window.

Each game date is scored against the lineup each team locked for that week (the
LINEUP_SET in force on that date), so later trades and waiver moves never rewrite
past days. Re-running is idempotent; if the NHL revises a stat line, the day is
re-emitted as a SCORE_CORRECTION with the new totals.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from gmbench.ledger import Ledger, canonical
from gmbench.season.scoring import REGULAR, GameLine, fetch_game_lines
from gmbench.state import LeagueState, TeamState, replay

EASTERN = ZoneInfo("America/New_York")


def yesterday_eastern(now: datetime | None = None) -> date:
    now = now or datetime.now(tz=EASTERN)
    return now.astimezone(EASTERN).date() - timedelta(days=1)


def lineup_on(team: TeamState, day: str) -> dict[str, Any] | None:
    """The lineup in force on a game date: the latest LINEUP_SET whose week starts on or before it."""
    current = None
    for entry in team.lineup_history:
        if entry.get("week") and entry["week"] <= day:
            current = entry
    return current


def score_day(state: LeagueState, day: str, lines: list[GameLine]) -> dict[str, Any]:
    active_owner: dict[int, str] = {}
    bench_owner: dict[int, str] = {}
    for team in state.teams.values():
        lineup = lineup_on(team, day)
        if lineup:
            active_owner.update({int(p): team.id for p in lineup["active"]})
            bench_owner.update({int(p): team.id for p in lineup["bench"]})
    team_points: dict[str, int] = {tid: 0 for tid in state.teams}
    rostered = []
    for ln in lines:
        owner = active_owner.get(ln.player_id) or bench_owner.get(ln.player_id)
        if owner is None:
            continue
        active = ln.player_id in active_owner
        pts = ln.fantasy_points if active else 0
        team_points[owner] += pts
        rostered.append({"player_id": ln.player_id, "team": owner, "active": active, "game_id": ln.game_id,
                         "nhl_team": ln.team, "group": ln.group, "goals": ln.goals, "assists": ln.assists,
                         "win": ln.win, "otl": ln.otl, "shutout": ln.shutout, "fantasy_points": ln.fantasy_points,
                         "counted": pts})
    rostered.sort(key=lambda r: (r["team"], r["player_id"], r["game_id"]))
    body = {"date": day, "team_points": dict(sorted(team_points.items())), "lines": rostered,
            "games": sorted({ln.game_id for ln in lines})}
    body["digest"] = hashlib.sha256(canonical(body).encode()).hexdigest()
    return body


def run_scoring(ledger: Ledger, *, start: date, end: date, games_dir: Path,
                fetch: Callable[..., list[GameLine]] = fetch_game_lines, log: Callable[[str], None] = print) -> list[dict]:
    state = replay(ledger.events())
    lines = fetch(start, end, REGULAR)
    by_day: dict[str, list[GameLine]] = defaultdict(list)
    for ln in lines:
        by_day[ln.game_date].append(ln)
    games_dir.mkdir(parents=True, exist_ok=True)
    emitted = []
    for day in sorted(by_day):
        (games_dir / f"{day}.json").write_text(json.dumps([ln.model_dump() for ln in by_day[day]], separators=(",", ":")))
        body = score_day(state, day, by_day[day])
        prior = state.scores.get(day)
        if prior is None:
            kind = "SCORE_DAY"
        elif prior.get("digest") != body["digest"]:
            kind = "SCORE_CORRECTION"
            body["previous_digest"] = prior.get("digest")
            body["previous_team_points"] = prior.get("team_points")
        else:
            continue
        event = ledger.append(kind, "league:scorer", body)
        state.scores[day] = body
        emitted.append(event)
        top = sorted(body["team_points"].items(), key=lambda kv: -kv[1])[:3]
        log(f"{kind:<16} {day}: {len(body['games'])} games, top {top}")
    return emitted
