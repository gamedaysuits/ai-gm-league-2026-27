"""Auction pools: rosters drafted by people, scored from the league's committed NHL game lines.

A pool file (``pools/<slug>.yaml``) lists each manager's players with the price paid.
Players carry an NHL ``id`` when it is known; the rest are resolved by name against the
NHL's season stats, with club and position breaking ties, so stats are never joined on
names. Points are goals plus assists from every skater line in ``runs/<run>/data/games``
on or after the pool's ``count_from`` date: the same files the daily scorer commits.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from gmbench.export.site import SCHEMA_VERSION, _write
from gmbench.ledger import utc_now
from gmbench.season.scoring import PlayerTotal

GROUP_POSITIONS = {"F": {"C", "L", "R"}, "D": {"D"}, "G": {"G"}}


# --- name -> NHL player ID ---------------------------------------------------------


def norm(name: str) -> str:
    """Lower-case ASCII name without punctuation: 'Tim Stützle' -> 'tim stutzle', 'J.T. Miller' -> 'jt miller'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z ]", " ", ascii_name.replace(".", "").replace("'", "")).split())


class Directory:
    """Everyone with NHL stats in the given season totals, for resolving drafted names to IDs."""

    def __init__(self, *totals: Mapping[int, PlayerTotal], overrides: Mapping[tuple[str, str], int] | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.people: dict[int, dict[str, Any]] = {}
        for source in totals:
            for pid, t in source.items():
                person = self.people.setdefault(pid, {"name": t.name, "position": t.position, "clubs": set()})
                person["clubs"].update(filter(None, t.teams.split(",")))

    def name(self, pid: int) -> str:
        return self.people[pid]["name"]

    def resolve(self, name: str, club: str, group: str | None = None) -> tuple[int, str]:
        """(player ID, how it matched: exact | fuzzy | override). Raises LookupError unless exactly one fits.

        An empty ``club`` lets the fuzzy match consider every club.
        """
        key = norm(name)
        if (key, club) in self.overrides:
            return self.overrides[(key, club)], "override"

        def fits(p: dict[str, Any]) -> bool:
            return group is None or p["position"] in GROUP_POSITIONS[group]

        found = [pid for pid, p in self.people.items() if norm(p["name"]) == key and fits(p)]
        if len(found) > 1:
            found = [pid for pid in found if club in self.people[pid]["clubs"]]
        if len(found) == 1:
            return found[0], "exact"
        # Official names can differ from everyday ones ("Mats Zuccarello Aasen", "John-Jason Peterka"):
        # same club and position group, the drafted surname among the official name's words, same first initial.
        surname = key.split()[-1]
        found = [
            pid for pid, p in self.people.items()
            if fits(p) and (not club or club in p["clubs"])
            and surname in norm(p["name"]).split() and norm(p["name"])[:1] == key[:1]
        ]
        if len(found) == 1:
            return found[0], "fuzzy"
        raise LookupError(f"cannot resolve {name!r} ({club}, {group}): candidates {found}")


# --- scoring -----------------------------------------------------------------------


def load_pool(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _skater_lines(games_dir: Path, since: str) -> dict[str, dict[int, dict[str, Any]]]:
    """Game date -> player ID -> that day's skater line, for every committed date on or after ``since``."""
    days: dict[str, dict[int, dict[str, Any]]] = {}
    for path in sorted(games_dir.glob("*.json")) if games_dir.is_dir() else []:
        if path.stem >= since:
            days[path.stem] = {int(ln["player_id"]): ln for ln in json.loads(path.read_text()) if ln["group"] == "skater"}
    return days


def score_pool(pool: Mapping[str, Any], games_dir: Path, directory: Directory | None = None) -> dict[str, Any]:
    days = _skater_lines(games_dir, pool["count_from"])
    dates = sorted(days)
    teams, unresolved = [], []
    for team in pool["teams"]:
        players, running = [], [0] * len(dates)
        for p in team["players"]:
            pid, matched = p.get("id"), "listed"
            if pid is None and directory is not None:
                try:
                    pid, matched = directory.resolve(p["name"], p.get("club") or "", p["slot"][0])
                except LookupError:
                    pid = None
            if pid is None:
                matched = None
                unresolved.append({"manager": team["manager"], "slot": p["slot"], "name": p["name"]})
            lines = [days[d].get(pid) for d in dates] if pid is not None else []
            played = [ln for ln in lines if ln]
            for i, ln in enumerate(lines):
                if ln:
                    running[i] += ln["goals"] + ln["assists"]
            goals, assists = sum(ln["goals"] for ln in played), sum(ln["assists"] for ln in played)
            players.append({
                "slot": p["slot"], "listed": p.get("listed", p["name"]),
                "name": directory.people.get(pid, p)["name"] if directory and matched != "listed" else p["name"],
                "club": played[-1]["team"] if played else p.get("club"), "price": p["price"], "pid": pid,
                "matched": matched, "gp": len(played), "goals": goals, "assists": assists, "points": goals + assists,
            })
        series, total = [], 0
        for pts in running:
            total += pts
            series.append(total)
        spent = sum(p["price"] for p in team["players"])
        teams.append({
            "id": re.sub(r"[^a-z0-9]+", "-", team["manager"].lower()).strip("-"), "manager": team["manager"],
            "spent": spent, "left": pool["budget"] - spent, "points": total,
            "last_7": sum(running[-7:]), "series": series, "players": players,
        })
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "name": pool["name"], "season": pool["season"],
        "count_from": pool["count_from"], "budget": pool["budget"], "roster": pool["roster"],
        "scoring": pool["scoring"], "prizes": pool.get("prizes", []),
        "days_scored": len(dates), "last_game_date": dates[-1] if dates else None, "dates": dates,
        "teams": teams, "unresolved": unresolved,
    }


def write_pool_export(pool: Mapping[str, Any], games_dir: Path, out_dir: Path, *,
                      directory: Directory | None = None) -> Path:
    path = out_dir / f"{pool['slug']}.json"
    _write(path, score_pool(pool, games_dir, directory), compact=True)
    return path


# --- roster check against the NHL's player directory -------------------------------

SEARCH_URL = "https://search.d3.nhle.com/api/v1/search/player?culture=en-us&limit=20&q={q}"


def check_pool(pool: Mapping[str, Any], get_json: Any) -> list[dict[str, Any]]:
    """One row per pool player: the NHL player behind its ``id`` (player page), or the name search's candidates.

    ``get_json(url) -> (payload, sha)``. Rows carry ``flags`` for anything a person should look at:
    a position outside the slot's group, a surname that differs from the sheet's, or no single match.
    """
    from urllib.parse import quote

    from gmbench.data.nhl import PLAYER_URL

    def text(v: Any) -> str:
        return (v.get("default") if isinstance(v, dict) else v) or ""

    rows = []
    for team in pool["teams"]:
        for p in team["players"]:
            group, flags, row = p["slot"][0], [], {"manager": team["manager"], "slot": p["slot"], "listed": p.get("listed", p["name"])}
            if p.get("id"):
                page, _ = get_json(PLAYER_URL.format(player_id=p["id"]))
                row.update(id=int(p["id"]), nhl_name=f"{text(page.get('firstName'))} {text(page.get('lastName'))}".strip(),
                           position=page.get("position"), team=page.get("currentTeamAbbrev"), active=page.get("isActive"),
                           how="listed id")
            else:
                key = norm(p["name"])

                def search(q: str, match: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
                    found, _ = get_json(SEARCH_URL.format(q=quote(q)))
                    found = found if isinstance(found, list) else found.get("results", [])
                    same = [c for c in found if match(norm(str(c.get("name", ""))))
                            and c.get("positionCode") in GROUP_POSITIONS[group]]
                    return (([c for c in same if c.get("active")] or same) if len(same) > 1 else same), found

                (same, found), how = search(p["name"], lambda n: n == key), "name search"
                if len(same) != 1:  # "Matthew" in the sheet, "Matt" in the NHL's directory: same surname and initial
                    (same, found), how = search(key.split()[-1], lambda n: n.split()[-1] == key.split()[-1] and n[:1] == key[:1]), "surname search"
                if len(same) == 1:
                    c = same[0]
                    row.update(id=int(c["playerId"]), nhl_name=c.get("name"), position=c.get("positionCode"),
                               team=c.get("teamAbbrev"), active=c.get("active"), how=how)
                else:
                    row.update(id=None, nhl_name=None, position=None, team=None, active=None, how="name search")
                    flags.append("no single match: " + "; ".join(
                        f"{c.get('name')} {c.get('positionCode')} {c.get('teamAbbrev')} {c.get('playerId')}" for c in found[:6]))
            if row.get("position") and row["position"] not in GROUP_POSITIONS[group]:
                flags.append(f"position {row['position']} in a {group} slot")
            if row.get("nhl_name") and norm(row["listed"]).split()[-1] != norm(row["nhl_name"]).split()[-1]:
                flags.append("sheet spelling differs")
            if row.get("id") and row.get("active") is False:
                flags.append("NHL lists him as inactive")
            if row.get("team") and p.get("club") and row["team"] != p["club"]:
                flags.append(f"club in file {p['club']}, NHL says {row['team']}")
            row["flags"] = flags
            rows.append(row)
    return rows


def check_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["| Manager | Slot | Sheet | NHL player | ID | Pos | Club | How | Check |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['manager']} | {r['slot']} | {r['listed']} | {r['nhl_name'] or '?'} | {r['id'] or '?'} | "
                     f"{r['position'] or ''} | {r['team'] or ''} | {r['how']} | {'; '.join(r['flags']) or 'ok'} |")
    return "\n".join(lines)
