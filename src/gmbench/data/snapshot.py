"""Build, save and load frozen league snapshots (the only data GM tools may read).

    uv run python -m gmbench.data.snapshot --kind test [--as-of 2026-09-28]

A snapshot is all 32 club rosters (current club = roster club), plus NHL-contracted
players those lists omit (injured / minors / unsigned; `on_roster=False`, club from
the NHL player API), each with up to three newest-first regular-season lines, ESPN
injuries, headline-only news, every club's schedule, and the house projection.
`sources` records the sha256 of every raw payload used. Files are canonical JSON
named `{kind}-{as_of}-{sha12}.json`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from gmbench.config import ROOT, LeagueConfig, load_config
from gmbench.data import espn, nhl
from gmbench.data.models import POSITION_GROUP, Player, SeasonLine, Snapshot
from gmbench.data.projections import (
    compute_baselines,
    fantasy_points,
    is_goalie_line,
    project_player,
    season_length,
    shift_season,
)

log = logging.getLogger(__name__)

SNAPSHOT_DIR = ROOT / "data" / "snapshots"
MAX_SEASONS = 3


def build_snapshot(
    kind: str,
    as_of_date: str = "2026-09-28",
    *,
    cfg: LeagueConfig | None = None,
    news_limit: int = 50,
    report: dict[str, Any] | None = None,
) -> Snapshot:
    """Fetch every source and assemble a Snapshot. `report` (optional) receives build diagnostics."""
    date.fromisoformat(as_of_date)  # validate YYYY-MM-DD
    cfg = cfg or load_config()
    season = str(cfg.raw["league"]["season"])
    scoring = cfg.rules["scoring"]
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sources: dict[str, str] = {}

    rosters = nhl.fetch_rosters(season, sources=sources)
    # The window includes the target season so in-season snapshots carry current stats;
    # projections only ever use the three completed seasons before it.
    raw_lines = nhl.fetch_season_lines(shift_season(season, -MAX_SEASONS), season, sources=sources)
    first_puck: dict[str, str] = {}
    team_games = nhl.fetch_team_games(season, sources=sources, first_puck=first_puck)
    injuries_payload = espn.fetch_injuries_payload(sources=sources)
    _check_clubs(rosters, team_games)

    # Roster lists omit injured, minor-league and unsigned players. Add everyone who played in
    # the NHL last season (or is on ESPN's injury list) and whose player page shows an active
    # NHL club, flagged on_roster=False.
    entries: dict[int, tuple[dict[str, Any], bool]] = {r["id"]: (r, True) for r in rosters}
    candidates = _off_roster_candidates(entries, raw_lines, injuries_payload, shift_season(season, -1))
    landing_failures: list[int] = []
    for r in nhl.fetch_contracted_players(candidates, sources=sources, failures=landing_failures):
        entries.setdefault(r["id"], (r, False))

    lines = {pid: [_season_line(d, scoring) for d in ds] for pid, ds in raw_lines.items()}
    groups = {pid: POSITION_GROUP.get(r["position"]) for pid, (r, _) in entries.items()}
    baselines = compute_baselines(
        ((groups.get(pid) or POSITION_GROUP.get(raw_lines[pid][0].get("position"), "F"), lines[pid])
         for pid in sorted(lines)),
        season,
    )  # fmt: skip
    game_counts = Counter(len(dates) for dates in team_games.values() if dates)
    season_games = game_counts.most_common(1)[0][0] if game_counts else season_length(season)
    age_on = date(int(season[:4]), 10, 1)

    players: dict[int, Player] = {}
    for pid, (r, on_roster) in sorted(entries.items()):
        group = groups[pid]
        if group is None:
            log.warning("skipping %s %s (%s): unknown position %r", r["first_name"], r["last_name"], r["team"], r["position"])
            continue
        own = [line for line in lines.get(pid, []) if is_goalie_line(line) == (group == "G")]
        player = _player(r, group, age_on, on_roster=on_roster, seasons=own[:MAX_SEASONS])
        player.projection = project_player(
            group, own, age=player.age, target_season=season, baselines=baselines, season_games=season_games
        )
        players[pid] = player

    injury_stats: dict[str, Any] = {}
    injuries, unmatched = espn.parse_injuries(injuries_payload, players.values(), stats=injury_stats)
    for pid, injury in injuries.items():
        players[pid].injury = injury
    news_stats: dict[str, Any] = {}
    news = espn.fetch_news(players.values(), news_limit, sources=sources, stats=news_stats)

    if report is not None:
        report.update(
            season_games=season_games,
            baselines=asdict(baselines),
            off_roster={"candidates": len(candidates), "added": sum(1 for _, on in entries.values() if not on),
                        "landing_failures": landing_failures},
            injuries={**injury_stats, "unmatched": unmatched},
            news=news_stats,
        )  # fmt: skip
    return Snapshot(
        kind=kind,
        taken_at=taken_at,
        season=season,
        as_of_date=as_of_date,
        players=players,
        team_games=team_games,
        first_puck_utc=dict(sorted(first_puck.items())),
        news=news,
        sources=dict(sorted(sources.items())),
    )


def _off_roster_candidates(
    entries: dict[int, tuple[dict[str, Any], bool]],
    raw_lines: dict[int, list[dict[str, Any]]],
    injuries_payload: dict[str, Any],
    last_season: str,
) -> set[int]:
    """Player IDs worth checking against the NHL player API: not on a roster, and either
    played in `last_season` or share an exact name with an ESPN-injured player no roster matched."""
    cands = {
        pid for pid, ds in raw_lines.items()
        if pid not in entries and any(d["season"] == last_season and d["gp"] > 0 for d in ds)
    }  # fmt: skip
    rostered = [_player(r, POSITION_GROUP.get(r["position"], "F"), None) for r, _ in entries.values()]
    _, unmatched = espn.parse_injuries(injuries_payload, rostered)
    by_name: dict[str, set[int]] = defaultdict(set)
    for pid, ds in raw_lines.items():
        if pid not in entries and ds[0].get("name"):
            by_name[espn.normalize_name(ds[0]["name"])].add(pid)
    for u in unmatched:
        cands |= by_name.get(espn.normalize_name(u["name"]), set())
    return cands


def _player(r: dict[str, Any], group: str, age_on: date | None, *, on_roster: bool = True, seasons=()) -> Player:
    return Player(
        id=r["id"],
        name=f"{r['first_name']} {r['last_name']}".strip(),
        first_name=r["first_name"],
        last_name=r["last_name"],
        team=r["team"],
        on_roster=on_roster,
        position=r["position"],
        group=group,
        birth_date=r["birth_date"],
        age=_age(r["birth_date"], age_on) if age_on else None,
        shoots=r["shoots"],
        sweater=r["sweater"],
        seasons=list(seasons),
    )


def _season_line(d: dict[str, Any], scoring: dict[str, Any]) -> SeasonLine:
    line = SeasonLine.model_validate(d)  # extra "name"/"position" keys are ignored
    line.fantasy_points = fantasy_points(line, scoring, goalie=is_goalie_line(line))
    return line


def _age(birth_date: str | None, on: date) -> float | None:
    try:
        born = date.fromisoformat(birth_date or "")
    except ValueError:
        return None
    return round((on - born).days / 365.25, 2)


def _check_clubs(rosters: list[dict[str, Any]], team_games: dict[str, list[str]]) -> None:
    rostered = Counter(r["team"] for r in rosters)
    missing = [t for t in nhl.TEAMS if not rostered[t] or not team_games.get(t)]
    if missing:
        raise RuntimeError(f"empty roster or schedule for {missing}; refusing to build a partial snapshot")
    goalies = Counter(r["team"] for r in rosters if r["position"] == "G")
    thin = [t for t in nhl.TEAMS if goalies[t] < 2]
    if thin:
        log.warning("clubs with fewer than 2 rostered goalies: %s", thin)


# --------------------------------------------------------------------------- persistence


def snapshot_bytes(snap: Snapshot) -> bytes:
    """Canonical serialization: sorted keys, no whitespace, ASCII."""
    return json.dumps(snap.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("ascii")


def snapshot_sha256(snap: Snapshot) -> str:
    return hashlib.sha256(snapshot_bytes(snap)).hexdigest()


def save_snapshot(snap: Snapshot, out_dir: Path | None = None) -> Path:
    """Write `{kind}-{as_of_date}-{sha12}.json` atomically; returns the path."""
    data = snapshot_bytes(snap)
    out = Path(out_dir) if out_dir is not None else SNAPSHOT_DIR
    out.mkdir(parents=True, exist_ok=True)
    kind = re.sub(r"[^A-Za-z0-9._-]+", "-", snap.kind).strip("-") or "snapshot"
    path = out / f"{kind}-{snap.as_of_date}-{hashlib.sha256(data).hexdigest()[:12]}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def load_snapshot(path: str | Path, *, verify: bool = True) -> Snapshot:
    """Load a saved snapshot; with `verify`, the file's sha256 must match the name's sha12."""
    path = Path(path)
    data = path.read_bytes()
    match = re.search(r"-([0-9a-f]{12})\.json$", path.name)
    if verify and match and hashlib.sha256(data).hexdigest()[:12] != match.group(1):
        raise ValueError(f"{path.name}: content does not match the hash in its filename")
    return Snapshot.model_validate_json(data)


# --------------------------------------------------------------------------- CLI


def summarize(snap: Snapshot, report: dict[str, Any] | None = None, top: int = 25) -> str:
    players = list(snap.players.values())
    groups = Counter(p.group for p in players)
    by_team: dict[str, Counter] = defaultdict(Counter)
    for p in players:
        by_team[p.team][p.group] += 1
    last = shift_season(snap.season, -1)
    played_last = sum(1 for p in players if any(s.season == last and s.gp > 0 for s in p.seasons))
    off = [p for p in players if not p.on_roster]
    roster_goalies = Counter(p.team for p in players if p.on_roster and p.group == "G")
    out = [
        f"snapshot {snap.kind} as of {snap.as_of_date} (season {snap.season}, taken {snap.taken_at})",
        f"players: {len(players)} ({len(players) - len(off)} on roster lists + {len(off)} off-roster)  "
        f"F={groups['F']} D={groups['D']} G={groups['G']}  (>=1 GP in {last}: {played_last})",
        "per club F/D/G: " + "  ".join(f"{t} {c['F']}/{c['D']}/{c['G']}" for t, c in sorted(by_team.items())),
        f"clubs with <2 rostered G: {[t for t in sorted(by_team) if roster_goalies[t] < 2] or 'none'}",
        f"schedules: {len(snap.team_games)} clubs, games per club {dict(Counter(map(len, snap.team_games.values())))}",
    ]
    if off:
        best = sorted(off, key=lambda p: (-(p.projection.fantasy_points if p.projection else 0.0), p.id))[:12]
        out.append(
            "top off-roster: "
            + ", ".join(f"{p.name} {p.team} {p.position} {p.projection.fantasy_points:.0f}"
                        + (f" [{p.injury.status}]" if p.injury else "") for p in best)
        )  # fmt: skip
    if report:
        if "off_roster" in report:
            out.append(f"off-roster check: {report['off_roster']}")
        inj, news = report.get("injuries", {}), report.get("news", {})
        entries = inj.get("entries") or 0
        out.append(
            f"injuries: {inj.get('matched')}/{entries} ESPN entries matched "
            f"({100 * (inj.get('matched') or 0) / max(entries, 1):.1f}%) -> {inj.get('players')} players"
        )
        for u in inj.get("unmatched", []):
            out.append(f"  unmatched: {u['name']} ({u['team']}, {u['status']})")
        out.append(
            f"news: {len(snap.news)} items; athlete tags matched {news.get('athlete_tags_matched')}/{news.get('athlete_tags')}"
            f"; unmatched: {news.get('unmatched_athletes')}"
        )
        out.append(f"season games: {report.get('season_games')}  baselines: {report.get('baselines')}")
    ranked = sorted(players, key=lambda p: (-(p.projection.fantasy_points if p.projection else 0.0), p.id))
    out.append(f"top {top} by projection:")
    for i, p in enumerate(ranked[:top], 1):
        proj = p.projection
        inj = f"  [{p.injury.status}]" if p.injury else ""
        out.append(f"{i:>3}. {p.name:<24} {p.team} {p.position}  {proj.fantasy_points:6.1f} pts  {proj.games:4.1f} g{inj}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m gmbench.data.snapshot", description=__doc__.splitlines()[0])
    parser.add_argument("--kind", required=True, help='snapshot kind, e.g. "draft", "week-2026-10-05", "test"')
    parser.add_argument("--as-of", default="2026-09-28", help="league date the snapshot represents (YYYY-MM-DD)")
    parser.add_argument("--news-limit", type=int, default=50)
    parser.add_argument("--out-dir", type=Path, default=None, help=f"default: {SNAPSHOT_DIR}")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    started = time.monotonic()
    report: dict[str, Any] = {}
    snap = build_snapshot(args.kind, args.as_of, news_limit=args.news_limit, report=report)
    path = save_snapshot(snap, args.out_dir)
    print(summarize(snap, report))
    print(f"saved {path} ({path.stat().st_size / 1e6:.2f} MB) in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
