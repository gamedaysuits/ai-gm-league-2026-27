"""Draft tracker feed: the whole league in one compact file for the tracker page on gamedaysuits.ca.

``tracker.json`` joins every draft pick, each model's standing (points, last 7
scored dates, running total per date), current rosters with the points each
player has counted for his team, and season-to-date fantasy points per player.
``front_office`` is every weekly front office, newest first: trades with their (judged) talk, waiver wins, the
week's featured lines and the rest of the chat, and the week's article once it's published. It is rebuilt from the ledger plus the committed per-game lines in
``runs/<run>/data/games``, so the daily scorer can refresh it without a data
snapshot. Player names come from the draft picks, then the current snapshot
when it is on disk (waiver adds, on the weekly runner), then the previous
``tracker.json``.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from gmbench.config import LeagueConfig
from gmbench.coverage.run import feed_player_ids, front_office_feed
from gmbench.data.models import Snapshot
from gmbench.export.site import SCHEMA_VERSION, _write, standings_series
from gmbench.ledger import utc_now
from gmbench.state import LeagueState

PERSONA_FIELDS = {"franchise_name": "franchise", "franchise_abbrev": "abbrev", "franchise_city": "city",
                  "gm_name": "gm", "primary_color": "primary", "secondary_color": "secondary"}


def season_points(state: LeagueState, games_dir: Path, *, since: str) -> dict[int, dict[str, Any]]:
    """Games played, fantasy points and latest club per player, from every committed game line since ``since``.

    A scored date with no games file falls back to the ledger's lines (rostered players only).
    """
    days: dict[str, list[tuple[int, int, str]]] = {}
    for path in sorted(games_dir.glob("*.json")) if games_dir.is_dir() else []:
        if path.stem >= since:
            days[path.stem] = [(ln["player_id"], ln["fantasy_points"], ln["team"]) for ln in json.loads(path.read_text())]
    for day, body in state.scores.items():
        if day not in days:
            days[day] = [(ln["player_id"], ln["fantasy_points"], ln["nhl_team"]) for ln in body["lines"]]
    totals: dict[int, dict[str, Any]] = {}
    for day in sorted(days):
        for pid, pts, club in days[day]:
            t = totals.setdefault(int(pid), {"gp": 0, "pts": 0})
            t["gp"] += 1
            t["pts"] += int(pts)
            t["nhl"] = club
    return totals


def _previous_players(path: Path) -> dict[int, dict[str, Any]]:
    try:
        return {int(pid): p for pid, p in json.loads(path.read_text()).get("players", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def write_tracker_export(state: LeagueState, cfg: LeagueConfig, out_dir: Path, *, games_dir: Path, today: str,
                         snapshot: Snapshot | None = None, events: list[dict[str, Any]] | None = None) -> Path:
    path = out_dir / "tracker.json"
    since = cfg.raw["league"]["first_puck_drop_utc"][:10]
    days, recent, series = standings_series(state)
    totals = state.points()

    counted: dict[tuple[str, int], int] = defaultdict(int)
    for body in state.scores.values():
        for ln in body["lines"]:
            counted[(ln["team"], int(ln["player_id"]))] += int(ln["counted"])

    players: dict[int, dict[str, Any]] = {
        int(pk["player_id"]): {"name": pk["player_name"], "pos": pk["position"], "group": pk["group"], "nhl": pk["nhl_team"]}
        for pk in state.picks
    }
    feed = front_office_feed(events, cfg, out_dir.parent) if events is not None else []
    wanted = {pid for t in state.teams.values() for pid in t.roster} | {pid for _, pid in counted} | feed_player_ids(feed)
    previous = _previous_players(path)
    for pid in sorted(wanted - players.keys()):
        if snapshot and pid in snapshot.players:
            p = snapshot.players[pid]
            players[pid] = {"name": p.name, "pos": p.position, "group": p.group, "nhl": p.team}
        elif pid in previous:
            players[pid] = {k: previous[pid].get(k) for k in ("name", "pos", "group", "nhl")}
        else:
            players[pid] = {"name": None, "pos": None, "group": state.player_group.get(pid), "nhl": None}
    played = season_points(state, games_dir, since=since)
    for pid, p in players.items():
        p.update(played.get(pid, {"gp": 0, "pts": 0}))

    teams = []
    for tid in state.order or list(state.teams):
        t = state.teams[tid]
        spec = cfg.team(tid)
        persona = t.persona or {}
        active = {int(pid) for pid in (t.lineup or {}).get("active", [])}
        teams.append({
            "id": tid, "display": spec.display, "lab": spec.lab, "bot": t.is_bot,
            **{short: persona.get(key) for key, short in PERSONA_FIELDS.items()},
            "points": totals.get(tid, 0), "last_7": recent.get(tid, 0), "series": series.get(tid, []),
            "roster": [{"pid": pid, "counted": counted.get((tid, pid), 0), "active": pid in active} for pid in t.roster],
            "former": [{"pid": pid, "counted": n} for (team, pid), n in sorted(counted.items())
                       if team == tid and n and pid not in t.roster],
        })

    doc = {
        "schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "as_of": today, "ledger_head_seq": state.head_seq,
        "league": cfg.raw["league"]["name"],
        "season": {"start": since, "end": cfg.raw["league"]["regular_season_end"]},
        "scoring": cfg.rules["scoring"], "rounds": cfg.rules["rounds"], "order": state.order,
        "days_scored": len(days), "last_game_date": days[-1] if days else None, "dates": days,
        "teams": teams,
        "picks": [{"no": pk["pick_no"], "round": pk["round"], "team": pk["team"], "pid": int(pk["player_id"]),
                   "proj": pk.get("projected_points"), "auto": bool(pk.get("auto")),
                   "owner": state.owner.get(int(pk["player_id"]))} for pk in state.picks],
        "players": {str(pid): players[pid] for pid in sorted(players)},
        "front_office": feed,
    }
    _write(path, doc, compact=True)
    return path
