"""Offline tests for the fantasy scoring engine (no network: HTTP is mocked)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError

from gmbench.season import scoring as sc
from gmbench.season.scoring import GameLine, PlayerTotal, fantasy_points, fetch_game_lines, reconcile

LEAGUE = {"skater": {"goal": 1, "assist": 1}, "goalie": {"win": 2, "otl": 1, "shutout": 2}}
PLAYOFF_POOL_2026 = {"skater": {"goal": 1, "assist": 1}, "goalie": {"win": 1, "shutout": 2}}


def skater(**stats) -> GameLine:
    return GameLine(game_date="2026-10-08", game_id=2026020001, player_id=8478402, team="EDM", group="skater", **stats)


def goalie(**stats) -> GameLine:
    return GameLine(game_date="2026-10-08", game_id=2026020001, player_id=8476883, team="TBL", group="goalie", **stats)


def test_league_yaml_scoring_is_what_these_tests_assume():
    assert sc.league_scoring() == LEAGUE


# --- fantasy_points ------------------------------------------------------------


@pytest.mark.parametrize(("goals", "assists", "expected"), [(1, 0, 1), (0, 1, 1), (2, 1, 3), (3, 2, 5)])
def test_skater_goals_and_assists(goals, assists, expected):
    assert fantasy_points(skater(goals=goals, assists=assists), LEAGUE) == expected


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ({"win": 1}, 2),
        ({"win": 1, "shutout": 1}, 4),  # e.g. Vasilevskiy's 1-0 OT win on 2026-05-01
        ({"otl": 1}, 1),
        ({"loss": 1}, 0),
    ],
)
def test_goalie_wins_otl_and_shutouts(decision, expected):
    assert fantasy_points(goalie(**decision), LEAGUE) == expected


def test_goalie_goals_and_assists_do_not_count():
    assert fantasy_points(goalie(assists=1), LEAGUE) == 0
    assert fantasy_points(goalie(goals=1, assists=2, win=1), LEAGUE) == 2


def test_zero_lines_score_zero():
    assert fantasy_points(skater(), LEAGUE) == 0
    assert fantasy_points(goalie(), LEAGUE) == 0  # relief appearance, no decision


def test_other_rule_sets_apply_as_given():
    # 2026 AI Playoff Pool: win 1 + shutout bonus 2, no OTL points.
    assert fantasy_points(goalie(win=1, shutout=1), PLAYOFF_POOL_2026) == 3
    assert fantasy_points(goalie(otl=1), PLAYOFF_POOL_2026) == 0
    assert fantasy_points(skater(goals=1, assists=1), PLAYOFF_POOL_2026) == 2


def test_unknown_stats_and_bad_weights_raise():
    with pytest.raises(ValueError, match="unknown skater scoring stat 'ppp'"):
        fantasy_points(skater(goals=1), {"skater": {"ppp": 1}, "goalie": {}})
    with pytest.raises(ValueError, match="unknown goalie scoring stat 'assist'"):
        fantasy_points(goalie(assists=1), {"skater": {}, "goalie": {"assist": 1}})
    with pytest.raises(TypeError):
        fantasy_points(skater(goals=1), {"skater": {"goal": 0.5}, "goalie": {}})


def test_fantasy_points_ignores_the_stored_value():
    line = skater(goals=2, fantasy_points=99)
    assert fantasy_points(line, LEAGUE) == 2
    assert line.fantasy_points == 99  # pure: the line is not modified


# --- GameLine invariants -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"group": "skater", "win": 1},
        {"group": "skater", "shutout": 1},
        {"group": "goalie", "win": 1, "loss": 1},
        {"group": "goalie", "win": 2},
        {"group": "skater", "goals": -1},
        {"group": "skater", "game_date": "2026-10-8"},
        {"group": "defense"},
        {"group": "skater", "points": 3},  # unknown field
    ],
)
def test_gameline_rejects_impossible_lines(bad):
    base = {"game_date": "2026-10-08", "game_id": 1, "player_id": 2, "team": "EDM"}
    with pytest.raises(ValidationError):
        GameLine(**{**base, **bad})


# --- fetch_game_lines against a fake NHL stats API ----------------------------


def _skater_row(game_id, player_id, goals=0, assists=0, team="EDM", date="2026-10-08"):
    return {"gameDate": date, "gameId": game_id, "playerId": player_id, "teamAbbrev": team,
            "positionCode": "C", "goals": goals, "assists": assists, "gamesPlayed": 1}


def _goalie_row(game_id, player_id, team="TBL", date="2026-10-08", **stats):
    row = {"gameDate": date, "gameId": game_id, "playerId": player_id, "teamAbbrev": team,
           "goals": 0, "assists": 0, "wins": 0, "losses": 0, "otLosses": 0, "shutouts": 0, "ties": None}
    return {**row, **stats}


class FakeStatsAPI:
    """Serves per-game summary rows the way the real API does: sorted, capped at a row window."""

    def __init__(self, skaters, goalies, window=sc.RESULT_WINDOW, fail_first=0):
        self.rows = {"skater": skaters, "goalie": goalies}
        self.window = window
        self.fail_first = fail_first
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_first:
            self.fail_first -= 1
            return httpx.Response(503)
        report = urlparse(str(request.url)).path.split("/")[-2]
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.requests.append({"report": report, **params})
        rows = sorted(self.rows[report], key=lambda r: (r["gameId"], r["playerId"]))
        cursor = re.search(r"gameId>=(\d+)", params["cayenneExp"])
        if cursor:
            rows = [r for r in rows if r["gameId"] >= int(cursor.group(1))]
        page = rows[: self.window]
        return httpx.Response(200, json={"data": page, "total": min(len(rows), self.window)})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_fetch_game_lines_parses_scores_and_sorts():
    api = FakeStatsAPI(
        skaters=[
            _skater_row(2026020002, 8478402, goals=1, assists=2, date="2026-10-09"),
            _skater_row(2026020001, 8479318, goals=0, assists=1),
            _skater_row(2026020001, 8478402, goals=2),
        ],
        goalies=[
            _goalie_row(2026020001, 8476883, wins=1, shutouts=1, assists=1),
            _goalie_row(2026020001, 8480045, team="EDM", otLosses=1),
        ],
    )
    lines = fetch_game_lines("2026-10-08", "2026-10-09", sc.PLAYOFFS, scoring=LEAGUE, client=api.client())

    assert [(ln.game_id, ln.group, ln.player_id, ln.fantasy_points) for ln in lines] == [
        (2026020001, "goalie", 8476883, 4),  # W + SO; his assist does not count
        (2026020001, "goalie", 8480045, 1),  # OTL
        (2026020001, "skater", 8478402, 2),
        (2026020001, "skater", 8479318, 1),
        (2026020002, "skater", 8478402, 3),
    ]
    assert lines[0].assists == 1  # the raw stat is kept, only the scoring ignores it
    assert [r["report"] for r in api.requests] == ["skater", "goalie"]  # two bulk calls
    for req in api.requests:
        assert req["isAggregate"] == "false" and req["isGame"] == "true"
        assert req["start"] == "0" and req["limit"] == "-1"
        assert req["cayenneExp"] == 'gameDate>="2026-10-08" and gameDate<="2026-10-09" and gameTypeId=3'


def test_fetch_game_lines_pages_past_the_row_window(monkeypatch):
    monkeypatch.setattr(sc, "RESULT_WINDOW", 4)
    skaters = [_skater_row(2026020000 + g, 8470000 + p, goals=1) for g in (1, 2, 3) for p in (1, 2, 3)]
    api = FakeStatsAPI(skaters=skaters, goalies=[], window=4)
    lines = fetch_game_lines("2026-10-08", "2026-10-08", scoring=LEAGUE, client=api.client())

    assert sorted((ln.game_id, ln.player_id) for ln in lines) == sorted((r["gameId"], r["playerId"]) for r in skaters)
    cursors = [re.search(r"gameId>=(\d+)", r["cayenneExp"]) for r in api.requests if r["report"] == "skater"]
    assert [c.group(1) if c else None for c in cursors] == [None, "2026020002", "2026020003"]


def test_fetch_game_lines_is_deterministic():
    rows = [_skater_row(2026020001, p, goals=p % 2) for p in (5, 3, 9, 1)]
    first = fetch_game_lines("2026-10-08", "2026-10-08", scoring=LEAGUE, client=FakeStatsAPI(rows, []).client())
    second = fetch_game_lines("2026-10-08", "2026-10-08", scoring=LEAGUE,
                              client=FakeStatsAPI(list(reversed(rows)), []).client())
    assert first == second
    assert [ln.player_id for ln in first] == [1, 3, 5, 9]


def test_fetch_game_lines_uses_league_scoring_by_default():
    api = FakeStatsAPI(skaters=[], goalies=[_goalie_row(2026020001, 8476883, wins=1)])
    [line] = fetch_game_lines("2026-10-08", "2026-10-08", client=api.client())
    assert line.fantasy_points == LEAGUE["goalie"]["win"]


def test_transient_errors_are_retried(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    api = FakeStatsAPI(skaters=[_skater_row(2026020001, 8478402, goals=1)], goalies=[], fail_first=2)
    lines = fetch_game_lines("2026-10-08", "2026-10-08", scoring=LEAGUE, client=api.client())
    assert len(lines) == 1


def test_persistent_errors_raise(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    api = FakeStatsAPI(skaters=[], goalies=[], fail_first=99)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_game_lines("2026-10-08", "2026-10-08", scoring=LEAGUE, client=api.client())


def test_reversed_date_window_is_rejected():
    with pytest.raises(ValueError, match="after end_date"):
        fetch_game_lines("2026-10-09", "2026-10-08", scoring=LEAGUE, client=FakeStatsAPI([], []).client())


# --- reconcile -----------------------------------------------------------------


def test_reconcile_reports_every_difference():
    lines = [skater(goals=1), skater(assists=1).model_copy(update={"game_id": 2026020002}), goalie(win=1)]
    totals = {
        8478402: PlayerTotal(player_id=8478402, name="A", group="skater", position="C", teams="EDM",
                             gp=2, goals=1, assists=1),
        8476883: PlayerTotal(player_id=8476883, name="B", group="goalie", position="G", teams="TBL",
                             gp=1, win=1),
    }
    assert reconcile(lines, totals) == []
    totals[8476883] = totals[8476883].model_copy(update={"shutout": 1})
    totals[8471214] = PlayerTotal(player_id=8471214, name="C", group="skater", position="L", teams="WSH", gp=1)
    assert reconcile(lines, totals) == [
        "8471214 C: NHL total has 1 GP but no game lines",
        "8476883 B: shutout lines=0 nhl=1",
    ]
