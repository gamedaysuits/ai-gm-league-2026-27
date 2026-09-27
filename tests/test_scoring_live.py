"""Live checks of the scoring engine against the NHL's own numbers.

These tests need the network, so they are skipped unless GMBENCH_LIVE=1:

    GMBENCH_LIVE=1 uv run pytest tests/test_scoring_live.py -v
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict

import httpx
import pytest

from gmbench.season import scoring as sc

pytestmark = pytest.mark.skipif(os.environ.get("GMBENCH_LIVE") != "1", reason="live NHL API check; set GMBENCH_LIVE=1")

SEASON = 20252026
PLAYOFF_WINDOW = ("2026-04-01", "2026-07-31")  # wider than the real bounds; gameTypeId=3 selects the games
BOXSCORE = "https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore"
DECISIONS = {"W": "win", "L": "loss", "O": "otl"}


@pytest.fixture(scope="module")
def playoff_lines() -> list[sc.GameLine]:
    return sc.fetch_game_lines(*PLAYOFF_WINDOW, sc.PLAYOFFS)


def test_2026_playoff_bounds_and_decisions(playoff_lines):
    dates = sorted({ln.game_date for ln in playoff_lines})
    assert (dates[0], dates[-1]) == ("2026-04-18", "2026-06-14")
    games = {ln.game_id for ln in playoff_lines}
    assert len(games) == 82
    per_game: dict[int, Counter] = defaultdict(Counter)
    for ln in playoff_lines:
        if ln.group == "goalie":
            per_game[ln.game_id].update(win=ln.win, loss=ln.loss, otl=ln.otl)
    assert set(per_game) == games
    assert all(c["win"] == 1 and c["loss"] == 1 and c["otl"] == 0 for c in per_game.values())  # playoff OT loss = L


def test_2026_playoff_lines_sum_to_nhl_aggregates(playoff_lines):
    totals = sc.fetch_season_totals(SEASON, sc.PLAYOFFS)
    assert len(totals) == len({ln.player_id for ln in playoff_lines}) > 300
    assert sc.reconcile(playoff_lines, totals) == []


def test_2026_playoff_goal_total_matches_team_goals(playoff_lines):
    team_games = httpx.get(f"{sc.STATS_BASE}/team/summary", timeout=60, params={
        "isAggregate": "false", "isGame": "true", "start": 0, "limit": -1,
        "cayenneExp": f'gameDate>="{PLAYOFF_WINDOW[0]}" and gameDate<="{PLAYOFF_WINDOW[1]}" and gameTypeId=3',
    }).raise_for_status().json()["data"]
    assert sum(ln.goals for ln in playoff_lines) == sum(r["goalsFor"] for r in team_games) == 486


def test_vasilevskiy_1_0_overtime_win_is_w_plus_so(playoff_lines):
    [line] = [ln for ln in playoff_lines if ln.player_id == 8476883 and ln.game_date == "2026-05-01"]
    assert (line.win, line.shutout, line.fantasy_points) == (1, 1, 4)


@pytest.mark.parametrize(
    "game_id",
    [
        2025030126,  # TBL 1-0 MTL in OT: Vasilevskiy W + SO
        2025030216,  # BUF 8-3 MTL: both starters pulled; Luukkonen wins in relief, no shutout
        2025030416,  # Stanley Cup-clinching game
    ],
)
def test_game_lines_match_the_boxscore(playoff_lines, game_id):
    box = httpx.get(BOXSCORE.format(game_id=game_id), timeout=60).raise_for_status().json()
    want_skaters, want_goalies = {}, {}
    for side in ("awayTeam", "homeTeam"):
        club, stats = box[side]["abbrev"], box["playerByGameStats"][side]
        for p in stats["forwards"] + stats["defense"]:
            want_skaters[p["playerId"]] = (club, p["goals"], p["assists"])
        played = [g for g in stats["goalies"] if g["toi"] != "00:00"]
        for g in played:
            decision = DECISIONS.get(g.get("decision") or "")
            line = {"win": 0, "loss": 0, "otl": 0, **({decision: 1} if decision else {})}
            line["shutout"] = int(decision == "win" and g["goalsAgainst"] == 0 and len(played) == 1)
            want_goalies[g["playerId"]] = (club, line["win"], line["loss"], line["otl"], line["shutout"])
        assert sum(p["goals"] for p in stats["forwards"] + stats["defense"]) == box[side]["score"]

    game = [ln for ln in playoff_lines if ln.game_id == game_id]
    got_skaters = {ln.player_id: (ln.team, ln.goals, ln.assists) for ln in game if ln.group == "skater"}
    got_goalies = {ln.player_id: (ln.team, ln.win, ln.loss, ln.otl, ln.shutout) for ln in game if ln.group == "goalie"}
    assert got_skaters == want_skaters
    assert got_goalies == want_goalies


def test_full_regular_season_pages_past_the_row_window_and_matches_aggregates():
    lines = sc.fetch_game_lines("2025-09-01", "2026-05-01", sc.REGULAR)
    assert sum(ln.group == "skater" for ln in lines) > sc.RESULT_WINDOW  # keyset paging was needed
    assert len({ln.game_id for ln in lines}) == 1312
    assert sc.reconcile(lines, sc.fetch_season_totals(SEASON, sc.REGULAR)) == []
