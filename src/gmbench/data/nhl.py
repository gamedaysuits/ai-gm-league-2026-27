"""NHL sources: club rosters, player pages, bulk season stats, and club schedules.

- Rosters:   api-web.nhle.com/v1/roster/{TEAM}/{season}  (omits injured, minor-league and unsigned players)
- Players:   api-web.nhle.com/v1/player/{id}/landing     (isActive + currentTeamAbbrev, used to fill that gap)
- Stats:     api.nhle.com/stats/rest/en/{skater/summary, skater/timeonice, goalie/summary}
- Schedules: api-web.nhle.com/v1/club-schedule-season/{TEAM}/{season}

Fetchers take an optional `sources` dict and record `label -> sha256` of every
raw payload they used. Parsers are pure so they can be tested on fixtures.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlencode

from gmbench.data.http import FetchError, combined_sha256, get_json

log = logging.getLogger(__name__)

TEAM_NAMES: dict[str, str] = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals",
    "WPG": "Winnipeg Jets",
}
TEAMS: tuple[str, ...] = tuple(TEAM_NAMES)

ROSTER_URL = "https://api-web.nhle.com/v1/roster/{team}/{season}"
PLAYER_URL = "https://api-web.nhle.com/v1/player/{player_id}/landing"
SCHEDULE_URL = "https://api-web.nhle.com/v1/club-schedule-season/{team}/{season}"
STATS_URL = "https://api.nhle.com/stats/rest/en/{report}"
MAX_WORKERS = 6  # polite parallelism for per-club / per-player calls
ROSTER_GROUPS = ("forwards", "defensemen", "goalies")


# --------------------------------------------------------------------------- rosters


def parse_roster(payload: dict[str, Any], team: str) -> list[dict[str, Any]]:
    """Flatten one club roster payload into player dicts with the club attached."""
    out = []
    for group in ROSTER_GROUPS:
        for p in payload.get(group) or []:
            out.append(
                {
                    "id": int(p["id"]),
                    "first_name": _text(p.get("firstName")),
                    "last_name": _text(p.get("lastName")),
                    "team": team,
                    "position": p.get("positionCode"),
                    "sweater": p.get("sweaterNumber"),
                    "shoots": p.get("shootsCatches"),
                    "birth_date": p.get("birthDate"),
                }
            )
    return out


def fetch_rosters(
    season: str, *, teams: tuple[str, ...] = TEAMS, sources: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Every rostered player for `season` (camp invitees included), in club order.

    These lists omit injured, minor-league and unsigned players (see fetch_contracted_players).
    A player listed by two clubs is kept on the first one only (logged).
    """
    results = _parallel_map(lambda t: get_json(ROSTER_URL.format(team=t, season=season)), teams)
    players: list[dict[str, Any]] = []
    seen: dict[int, str] = {}
    for team in teams:
        payload, sha = results[team]
        if sources is not None:
            sources[f"nhl:roster:{team}:{season}"] = sha
        for p in parse_roster(payload, team):
            if p["id"] in seen:
                log.warning("player %s listed by %s and %s; keeping %s", p["id"], seen[p["id"]], team, seen[p["id"]])
                continue
            seen[p["id"]] = team
            players.append(p)
    return players


def parse_player_landing(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Roster-shaped dict if the player is active and belongs to an NHL club, else None."""
    team = payload.get("currentTeamAbbrev")
    if not payload.get("isActive") or team not in TEAM_NAMES:
        return None
    return {
        "id": int(payload["playerId"]),
        "first_name": _text(payload.get("firstName")),
        "last_name": _text(payload.get("lastName")),
        "team": team,
        "position": payload.get("position"),
        "sweater": payload.get("sweaterNumber"),
        "shoots": payload.get("shootsCatches"),
        "birth_date": payload.get("birthDate"),
    }


def fetch_contracted_players(
    player_ids: set[int] | list[int],
    *,
    sources: dict[str, str] | None = None,
    failures: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Roster-shaped dicts for those `player_ids` whose player page shows an active NHL club.

    Used for players the roster endpoint omits. A page that still fails after retries is
    skipped (logged, appended to `failures`) rather than failing the whole snapshot.
    """
    ids = sorted(set(player_ids))

    def fetch(pid: int) -> tuple[Any, str] | None:
        try:
            return get_json(PLAYER_URL.format(player_id=pid))
        except FetchError as exc:
            log.warning("player %s: %s", pid, exc)
            return None

    results = _parallel_map(fetch, ids)
    out, hashes = [], []
    for pid in ids:
        if results[pid] is None:
            if failures is not None:
                failures.append(pid)
            continue
        payload, sha = results[pid]
        hashes.append(sha)  # ordered by player id
        if (player := parse_player_landing(payload)) is not None:
            out.append(player)
    if sources is not None and hashes:
        sources["nhl:player-landing"] = combined_sha256(hashes)
    return out


# --------------------------------------------------------------------------- season stats


def fetch_report(
    report: str, cayenne_exp: str, *, sources: dict[str, str] | None = None, label: str | None = None
) -> list[dict[str, Any]]:
    """All rows of one NHL stats report, paginating defensively in case `limit=-1` is capped."""
    rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    start = 0
    sort = json.dumps([{"property": "playerId", "direction": "ASC"}, {"property": "seasonId", "direction": "ASC"}])
    while True:
        query = urlencode(
            {
                "isAggregate": "false",
                "isGame": "false",
                "sort": sort,
                "start": start,
                "limit": -1,
                "cayenneExp": cayenne_exp,
            },
            quote_via=quote,
        )
        data, sha = get_json(f"{STATS_URL.format(report=report)}?{query}")
        page = data.get("data") or []
        total = int(data.get("total") or 0)
        rows.extend(page)
        hashes.append(sha)
        start += len(page)
        if not page or start >= total:
            break
        log.info("%s: paginating (%d/%d rows)", report, start, total)
    if sources is not None:
        sources[label or f"nhl:stats:{report}:{cayenne_exp}"] = combined_sha256(hashes)
    return rows


def merge_season_rows(
    skater_summary: list[dict[str, Any]],
    skater_toi: list[dict[str, Any]],
    goalie_summary: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Merge report rows into SeasonLine-shaped dicts per player, newest season first.

    Each dict also carries `name` and `position` (the report's positionCode, "G" for
    goalies); SeasonLine ignores both. Goalie lines always have `gs` set; skater lines never do.
    """
    lines: dict[tuple[int, int], dict[str, Any]] = {}
    for r in skater_summary:
        key = (int(r["playerId"]), int(r["seasonId"]))
        lines[key] = {
            "season": str(r["seasonId"]),
            "teams": r.get("teamAbbrevs"),
            "name": r.get("skaterFullName"),
            "position": r.get("positionCode"),
            "gp": _int(r.get("gamesPlayed")),
            "goals": _int(r.get("goals")),
            "assists": _int(r.get("assists")),
            "points": _int(r.get("points")),
            "plus_minus": _opt_int(r.get("plusMinus")),
            "pp_points": _opt_int(r.get("ppPoints")),
            "shots": _opt_int(r.get("shots")),
            "toi_per_game_s": _round(r.get("timeOnIcePerGame"), 1),
        }
    for r in skater_toi:
        line = lines.get((int(r["playerId"]), int(r["seasonId"])))
        if line is None:
            continue
        line["pp_toi_per_game_s"] = _round(r.get("ppTimeOnIcePerGame"), 1)
        if line["toi_per_game_s"] is None:
            line["toi_per_game_s"] = _round(r.get("timeOnIcePerGame"), 1)
    for r in goalie_summary:
        key = (int(r["playerId"]), int(r["seasonId"]))
        if key in lines:
            log.warning("player %s has skater and goalie rows for %s; keeping goalie row", *key)
        lines[key] = {
            "season": str(r["seasonId"]),
            "teams": r.get("teamAbbrevs"),
            "name": r.get("goalieFullName"),
            "position": "G",
            "gp": _int(r.get("gamesPlayed")),
            "goals": _int(r.get("goals")),
            "assists": _int(r.get("assists")),
            "points": _int(r.get("points")),
            "gs": _int(r.get("gamesStarted")),
            "wins": _int(r.get("wins")),
            "losses": _int(r.get("losses")),
            "ot_losses": _int(r.get("otLosses")),
            "shutouts": _int(r.get("shutouts")),
            "save_pct": _round(r.get("savePct"), 4),
            "gaa": _round(r.get("goalsAgainstAverage"), 2),
        }
    by_player: dict[int, list[dict[str, Any]]] = {}
    for (pid, _season), line in sorted(lines.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        by_player.setdefault(pid, []).append(line)
    return by_player


def fetch_season_lines(
    first_season: str, last_season: str, game_type: int = 2, *, sources: dict[str, str] | None = None
) -> dict[int, list[dict[str, Any]]]:
    """Per-player season lines (all clubs combined) for seasons first..last, newest first."""
    exp = f"gameTypeId={game_type} and seasonId>={first_season} and seasonId<={last_season}"
    reports = {}
    for report in ("skater/summary", "skater/timeonice", "goalie/summary"):
        label = f"nhl:stats:{report}:{first_season}-{last_season}:gameType{game_type}"
        reports[report] = fetch_report(report, exp, sources=sources, label=label)
    return merge_season_rows(reports["skater/summary"], reports["skater/timeonice"], reports["goalie/summary"])


# --------------------------------------------------------------------------- schedules


def parse_schedule(payload: dict[str, Any], game_type: int = 2) -> list[str]:
    """Sorted unique game dates (YYYY-MM-DD; the NHL's gameDate is the ET date)."""
    return sorted({g["gameDate"] for g in payload.get("games") or [] if g.get("gameType") == game_type})


def parse_start_times(payload: dict[str, Any], game_type: int = 2) -> dict[str, str]:
    """Game date (ET) -> earliest scheduled start (UTC ISO) among this club's games."""
    out: dict[str, str] = {}
    for g in payload.get("games") or []:
        if g.get("gameType") == game_type and g.get("startTimeUTC"):
            day, start = g["gameDate"], g["startTimeUTC"]
            if day not in out or start < out[day]:
                out[day] = start
    return out


def fetch_team_games(
    season: str, *, teams: tuple[str, ...] = TEAMS, sources: dict[str, str] | None = None,
    first_puck: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Club -> sorted regular-season game dates. If `first_puck` is given, it's filled with
    game date -> the earliest start time (UTC) across all clubs, for lineup-lock times."""
    results = _parallel_map(lambda t: get_json(SCHEDULE_URL.format(team=t, season=season)), teams)
    out: dict[str, list[str]] = {}
    for team in teams:
        payload, sha = results[team]
        if sources is not None:
            sources[f"nhl:schedule:{team}:{season}"] = sha
        out[team] = parse_schedule(payload)
        if first_puck is not None:
            for day, start in parse_start_times(payload).items():
                if day not in first_puck or start < first_puck[day]:
                    first_puck[day] = start
    return out


# --------------------------------------------------------------------------- helpers


def _parallel_map(fn, keys):
    keys = list(keys)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(zip(keys, pool.map(fn, keys)))


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return (value.get("default") or "").strip()
    return (value or "").strip()


def _int(value: Any) -> int:
    return int(value or 0)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _round(value: Any, ndigits: int) -> float | None:
    return None if value is None else round(float(value), ndigits)
