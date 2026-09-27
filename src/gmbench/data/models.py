"""Data contracts for frozen league snapshots.

A snapshot is the only information source GM tools may read during a draft or
weekly session, so every GM sees identical data and sessions can be replayed.
Players are always keyed by NHL player ID (names collide: two Elias Petterssons).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Group = Literal["F", "D", "G"]
POSITION_GROUP: dict[str, Group] = {"C": "F", "L": "F", "R": "F", "D": "D", "G": "G"}


class SeasonLine(BaseModel):
    """One NHL regular season for one player (all teams combined)."""

    season: str  # "20252026"
    teams: str | None = None  # NHL "teamAbbrevs", e.g. "TOR,VAN" after a trade
    gp: int = 0
    # skaters
    goals: int = 0
    assists: int = 0
    points: int = 0
    plus_minus: int | None = None
    pp_points: int | None = None
    shots: int | None = None
    toi_per_game_s: float | None = None
    pp_toi_per_game_s: float | None = None
    # goalies
    gs: int | None = None
    wins: int | None = None
    losses: int | None = None
    ot_losses: int | None = None
    shutouts: int | None = None
    save_pct: float | None = None
    gaa: float | None = None
    fantasy_points: int = 0  # under this league's scoring rules


class Injury(BaseModel):
    status: str  # e.g. "Out", "Day-To-Day", "Injured Reserve", "Suspension"
    updated: str | None = None
    note: str | None = Field(default=None, description="Third-party text: GM-visible only, never exported publicly")


class Projection(BaseModel):
    """House baseline projection. Hidden from GMs; used by the autodraft bot and for Pick Value Added."""

    games: float
    fantasy_points: float
    method: str


class Player(BaseModel):
    id: int
    name: str
    first_name: str
    last_name: str
    team: str  # current NHL club abbreviation, e.g. "EDM"
    # False: under contract with `team` per the NHL player API but not on its current roster
    # list (injured reserve, assigned to the minors, or unsigned).
    on_roster: bool = True
    position: str  # C, L, R, D, G
    group: Group
    birth_date: str | None = None
    age: float | None = None  # years, as of the season's opening day
    shoots: str | None = None
    sweater: int | None = None
    seasons: list[SeasonLine] = Field(default_factory=list)  # newest first, up to 3
    injury: Injury | None = None
    projection: Projection | None = None


class NewsItem(BaseModel):
    published: str
    headline: str
    teams: list[str] = Field(default_factory=list)
    player_ids: list[int] = Field(default_factory=list)


class Snapshot(BaseModel):
    kind: str  # "draft", "week-2026-10-05", "test", ...
    taken_at: str  # UTC ISO timestamp
    season: str  # "20262027"
    as_of_date: str  # league date the snapshot represents (YYYY-MM-DD)
    players: dict[int, Player]
    team_games: dict[str, list[str]] = Field(default_factory=dict)  # club -> game dates (YYYY-MM-DD, ET)
    first_puck_utc: dict[str, str] = Field(default_factory=dict)  # game date (ET) -> earliest start, UTC ISO
    news: list[NewsItem] = Field(default_factory=list)
    sources: dict[str, str] = Field(default_factory=dict)  # source label -> sha256 of the raw payload
