"""Daily scoring uses the lineup locked for each week, and is idempotent with corrections."""
from __future__ import annotations

from pathlib import Path

from gmbench.ledger import Ledger
from gmbench.season.daily import run_scoring
from gmbench.season.scoring import GameLine
from gmbench.state import replay

TEAMS = [{"id": "a", "lab": "x", "model": "m/a"}, {"id": "b", "lab": "x", "model": "m/b"}]


def _league(tmp: Path) -> Ledger:
    led = Ledger(tmp / "l.jsonl")
    led.append("LEAGUE_CREATED", "league", {"teams": TEAMS})
    led.append("LINEUP_SET", "gm:a", {"team": "a", "week": "2026-09-29", "active": [1], "bench": [2], "source": "gm"})
    led.append("LINEUP_SET", "gm:b", {"team": "b", "week": "2026-09-29", "active": [3], "bench": [], "source": "gm"})
    # week 2: a benches player 1 and activates player 2
    led.append("LINEUP_SET", "gm:a", {"team": "a", "week": "2026-10-05", "active": [2], "bench": [1], "source": "gm"})
    return led


def _line(day: str, pid: int, g: int, a: int = 0, gid: int = 1) -> GameLine:
    return GameLine(game_date=day, game_id=gid, player_id=pid, team="EDM", group="skater", goals=g, assists=a,
                    fantasy_points=g + a)


def test_scores_use_the_weeks_locked_lineup(tmp_path: Path) -> None:
    led = _league(tmp_path)
    lines = [_line("2026-10-01", 1, 2), _line("2026-10-01", 2, 1), _line("2026-10-01", 3, 0, 1),
             _line("2026-10-06", 1, 3, gid=2), _line("2026-10-06", 2, 1, gid=2)]
    run_scoring(led, start=None, end=None, games_dir=tmp_path / "g", fetch=lambda s, e, t: lines, log=lambda _: None)
    st = replay(led.events())
    assert st.scores["2026-10-01"]["team_points"] == {"a": 2, "b": 1}   # player 1 active week 1, player 2 benched
    assert st.scores["2026-10-06"]["team_points"] == {"a": 1, "b": 0}   # week 2 swap: only player 2 counts
    assert st.points() == {"a": 3, "b": 1}


def test_rescoring_is_idempotent_and_emits_corrections(tmp_path: Path) -> None:
    led = _league(tmp_path)
    first = [_line("2026-10-01", 1, 1)]
    run_scoring(led, start=None, end=None, games_dir=tmp_path / "g", fetch=lambda s, e, t: first, log=lambda _: None)
    n = sum(1 for _ in led.events())
    run_scoring(led, start=None, end=None, games_dir=tmp_path / "g", fetch=lambda s, e, t: first, log=lambda _: None)
    assert sum(1 for _ in led.events()) == n  # unchanged data → no new events
    revised = [_line("2026-10-01", 1, 1, 1)]  # NHL adds an assist
    run_scoring(led, start=None, end=None, games_dir=tmp_path / "g", fetch=lambda s, e, t: revised, log=lambda _: None)
    kinds = [e["type"] for e in led.events()]
    assert kinds[-1] == "SCORE_CORRECTION"
    assert replay(led.events()).points()["a"] == 2
    assert led.verify()[0] == n + 1
