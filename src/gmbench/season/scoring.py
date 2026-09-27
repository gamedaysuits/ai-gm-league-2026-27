"""Fantasy scoring engine: NHL per-game stat lines -> league fantasy points.

Source: the NHL stats REST "summary" reports in per-game mode
(``isAggregate=false&isGame=true``), one bulk call each for skaters and goalies
over a date window. Goalie wins, losses, OT losses and shutouts are the NHL's
own per-game rulings (a 1-0 overtime win is W + SO), so nothing here
re-derives decisions from boxscores.

API behaviour this module relies on (verified September 2026):

* ``limit=-1`` returns every row, up to a hard 10,000-row result window:
  ``start + rows <= 10,000``, and ``total`` is capped at 10,000 too. Explicit
  limits above 100 are silently clamped to 100, and without ``limit`` a page
  holds 50 rows.
* Beyond the window, offsets return nothing, so a report that fills the window
  is continued by keyset pagination: rows are sorted by (gameId, playerId) and
  the next request adds ``and gameId>=<last gameId>``. The boundary game is
  fetched again and de-duplicated.
* Skater reports contain no goalies. In the playoffs, an overtime loss is
  recorded as a loss (``otLosses`` is always 0).
* The NHL blocks Python-urllib's User-Agent. httpx's default User-Agent works.

For daily use, callers rescore a trailing window (for example the last 7 days)
on every run to pick up stat corrections. A 7-day window is two requests and
at most about 2,300 rows (2025-26 maximum). Output is sorted and depends only on the NHL
data, so the same data always gives the same lines. :func:`reconcile` checks
summed lines against the NHL's own season totals, which catches any game a
window missed.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gmbench.config import load_config

STATS_BASE = "https://api.nhle.com/stats/rest/en"
RESULT_WINDOW = 10_000  # hard row window of the stats REST API (see module docstring)
PRESEASON, REGULAR, PLAYOFFS = 1, 2, 3

HTTP_TIMEOUT_S = 60.0
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 1.0  # 1s, 2s, 4s between attempts
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

Group = Literal["skater", "goalie"]

# Scoring-rule key (league.yaml rules.scoring.<group>) -> GameLine field.
# Goalies score only decisions and shutouts; their goals and assists never count.
SCORED_STATS: dict[str, dict[str, str]] = {
    "skater": {"goal": "goals", "assist": "assists"},
    "goalie": {"win": "win", "loss": "loss", "otl": "otl", "shutout": "shutout"},
}
TOTAL_FIELDS = ("gp", "goals", "assists", "win", "loss", "otl", "shutout")

_SORT = json.dumps([
    {"property": "gameId", "direction": "ASC"},
    {"property": "playerId", "direction": "ASC"},
])


class GameLine(BaseModel):
    """One player's stat line in one NHL game."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    game_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")  # NHL gameDate
    game_id: int
    player_id: int
    team: str  # club he played for in this game
    group: Group
    goals: int = Field(default=0, ge=0)
    assists: int = Field(default=0, ge=0)
    win: int = Field(default=0, ge=0, le=1)
    loss: int = Field(default=0, ge=0, le=1)
    otl: int = Field(default=0, ge=0, le=1)
    shutout: int = Field(default=0, ge=0, le=1)
    fantasy_points: int = 0

    @model_validator(mode="after")
    def _one_decision(self) -> GameLine:
        decisions = self.win + self.loss + self.otl
        if self.group == "skater" and (decisions or self.shutout):
            raise ValueError("skater lines carry no goalie decisions or shutouts")
        if decisions > 1:
            raise ValueError("a goalie gets at most one decision per game")
        return self


class PlayerTotal(BaseModel):
    """The NHL's own season aggregate for one player (all clubs combined)."""

    model_config = ConfigDict(frozen=True)

    player_id: int
    name: str
    group: Group
    position: str  # C, L, R, D, G
    teams: str  # NHL teamAbbrevs, e.g. "NYR,LAK" after a trade
    gp: int = 0
    goals: int = 0
    assists: int = 0
    win: int = 0
    loss: int = 0
    otl: int = 0
    shutout: int = 0


def league_scoring() -> dict:
    """This league's scoring rules (league.yaml ``rules.scoring``)."""
    return load_config().rules["scoring"]


def fantasy_points(line: GameLine, scoring: Mapping[str, Mapping[str, int]]) -> int:
    """Fantasy points for one line under ``scoring``. Pure.

    ``scoring`` has the shape of league.yaml ``rules.scoring``:
    ``{"skater": {"goal": 1, "assist": 1}, "goalie": {"win": 2, "otl": 1, "shutout": 2}}``.
    A stat missing from the rules scores 0. An unknown stat key raises rather
    than silently scoring 0.
    """
    fields = SCORED_STATS[line.group]
    points = 0
    for stat, weight in scoring[line.group].items():
        if stat not in fields:
            raise ValueError(f"unknown {line.group} scoring stat {stat!r} (known: {', '.join(fields)})")
        if not isinstance(weight, int):
            raise TypeError(f"scoring weight for {line.group}.{stat} must be an int, got {weight!r}")
        points += weight * getattr(line, fields[stat])
    return points


def fetch_game_lines(
    start_date: str | date,
    end_date: str | date,
    game_type: int = REGULAR,
    *,
    scoring: Mapping[str, Mapping[str, int]] | None = None,
    client: httpx.Client | None = None,
) -> list[GameLine]:
    """Every skater and goalie line for NHL games dated start_date..end_date (inclusive).

    Uses two bulk requests, one per report. More are needed only if a report
    fills the API's 10,000-row window. ``fantasy_points`` uses ``scoring``
    (default: this league's rules). Lines are sorted by (game_date, game_id,
    group, player_id).
    """
    start, end = _iso_date(start_date), _iso_date(end_date)
    if start > end:
        raise ValueError(f"start_date {start} is after end_date {end}")
    rules = league_scoring() if scoring is None else scoring
    where = f'gameDate>="{start}" and gameDate<="{end}" and gameTypeId={int(game_type)}'
    with _http(client) as http:
        skaters = _fetch_per_game(http, "skater", where)
        goalies = _fetch_per_game(http, "goalie", where)
    lines = [_skater_line(r) for r in skaters] + [_goalie_line(r) for r in goalies]
    scored = [ln.model_copy(update={"fantasy_points": fantasy_points(ln, rules)}) for ln in lines]
    scored.sort(key=lambda ln: (ln.game_date, ln.game_id, ln.group, ln.player_id))
    return scored


def fetch_season_totals(
    season: str | int, game_type: int = REGULAR, *, client: httpx.Client | None = None
) -> dict[int, PlayerTotal]:
    """The NHL's season aggregates per player (``isGame=false``), keyed by player ID."""
    where = f"seasonId={int(season)} and gameTypeId={int(game_type)}"
    params = {"isAggregate": "false", "isGame": "false", "start": 0, "limit": -1,
              "sort": json.dumps([{"property": "playerId", "direction": "ASC"}]), "cayenneExp": where}
    totals: dict[int, PlayerTotal] = {}
    with _http(client) as http:
        for report in ("skater", "goalie"):
            payload = _get_json(http, f"{STATS_BASE}/{report}/summary", params)
            rows = payload["data"]
            if len(rows) >= RESULT_WINDOW or len(rows) < int(payload.get("total") or 0):
                raise RuntimeError(f"{report} season totals exceed the {RESULT_WINDOW}-row window")
            for r in rows:
                stat = {k: r.get(k) or 0 for k in ("gamesPlayed", "goals", "assists")}
                if report == "goalie":
                    decisions = {"win": r.get("wins") or 0, "loss": r.get("losses") or 0,
                                 "otl": r.get("otLosses") or 0, "shutout": r.get("shutouts") or 0}
                    name, position = r["goalieFullName"], "G"
                else:
                    decisions, name, position = {}, r["skaterFullName"], r["positionCode"]
                totals[r["playerId"]] = PlayerTotal(
                    player_id=r["playerId"], name=name, group=report, position=position,
                    teams=r.get("teamAbbrevs") or "", gp=stat["gamesPlayed"],
                    goals=stat["goals"], assists=stat["assists"], **decisions,
                )
    return totals


def sum_lines(lines: Iterable[GameLine]) -> dict[int, dict[str, int]]:
    """Per-player sums of game lines: games (gp), stats, and fantasy points."""
    out: dict[int, dict[str, int]] = {}
    for ln in lines:
        t = out.setdefault(ln.player_id, dict.fromkeys((*TOTAL_FIELDS, "fantasy_points"), 0))
        t["gp"] += 1
        for f in TOTAL_FIELDS[1:]:
            t[f] += getattr(ln, f)
        t["fantasy_points"] += ln.fantasy_points
    return out


def reconcile(lines: Iterable[GameLine], totals: Mapping[int, PlayerTotal]) -> list[str]:
    """Differences between summed game lines and the NHL's season totals. Empty means they agree.

    ``lines`` must cover every game of the season/game type that ``totals`` describes.
    """
    lines = list(lines)
    sums = sum_lines(lines)
    groups = {ln.player_id: ln.group for ln in lines}
    problems: list[str] = []
    for pid in sorted(set(sums) | set(totals)):
        total, summed = totals.get(pid), sums.get(pid)
        if total is None:
            problems.append(f"{pid}: {summed['gp']} game lines but no NHL season total")
            continue
        if summed is None:
            if total.gp:
                problems.append(f"{pid} {total.name}: NHL total has {total.gp} GP but no game lines")
            continue
        if groups[pid] != total.group:
            problems.append(f"{pid} {total.name}: group {groups[pid]} in lines vs {total.group} in totals")
        for f in TOTAL_FIELDS:
            if summed[f] != getattr(total, f):
                problems.append(f"{pid} {total.name}: {f} lines={summed[f]} nhl={getattr(total, f)}")
    return problems


# --- internals ---------------------------------------------------------------


def _iso_date(value: str | date) -> str:
    return (value if isinstance(value, date) else date.fromisoformat(str(value))).isoformat()


@contextmanager
def _http(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=HTTP_TIMEOUT_S, headers={"Accept": "application/json"}) as own:
        yield own


def _get_json(http: httpx.Client, url: str, params: Mapping[str, Any]) -> Any:
    """GET JSON, retrying network errors, 429 and 5xx with exponential backoff."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        delay = RETRY_BACKOFF_S * 2 ** (attempt - 1)
        try:
            resp = http.get(url, params=params)
        except httpx.TransportError:
            if attempt == RETRY_ATTEMPTS:
                raise
        else:
            if resp.status_code not in RETRY_STATUS or attempt == RETRY_ATTEMPTS:
                resp.raise_for_status()
                return resp.json()
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = min(float(retry_after), 60.0)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _fetch_per_game(http: httpx.Client, report: str, where: str) -> list[dict[str, Any]]:
    """All per-game rows of one summary report, sorted by (gameId, playerId)."""
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    cursor: int | None = None
    while True:
        cayenne = where if cursor is None else f"{where} and gameId>={cursor}"
        payload = _get_json(http, f"{STATS_BASE}/{report}/summary", {
            "isAggregate": "false", "isGame": "true", "start": 0, "limit": -1,
            "sort": _SORT, "cayenneExp": cayenne,
        })
        page = payload["data"]
        keys = [(r["gameId"], r["playerId"]) for r in page]
        rows.update(zip(keys, page))
        truncated = bool(page) and (len(page) >= RESULT_WINDOW or len(page) < int(payload.get("total") or 0))
        if not truncated:
            return [rows[k] for k in sorted(rows)]
        if keys != sorted(keys):
            raise RuntimeError(f"{report} rows are not sorted by (gameId, playerId); cannot page past the window")
        if cursor is not None and keys[-1][0] <= cursor:
            raise RuntimeError(f"{report} paging made no progress at gameId {cursor}")
        cursor = keys[-1][0]


def _skater_line(r: Mapping[str, Any]) -> GameLine:
    return GameLine(
        game_date=str(r["gameDate"])[:10], game_id=r["gameId"], player_id=r["playerId"],
        team=r["teamAbbrev"], group="skater",
        goals=r.get("goals") or 0, assists=r.get("assists") or 0,
    )


def _goalie_line(r: Mapping[str, Any]) -> GameLine:
    return GameLine(
        game_date=str(r["gameDate"])[:10], game_id=r["gameId"], player_id=r["playerId"],
        team=r["teamAbbrev"], group="goalie",
        goals=r.get("goals") or 0, assists=r.get("assists") or 0,
        win=r.get("wins") or 0, loss=r.get("losses") or 0,
        otl=r.get("otLosses") or 0, shutout=r.get("shutouts") or 0,
    )
