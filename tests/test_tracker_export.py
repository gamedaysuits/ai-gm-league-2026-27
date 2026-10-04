"""The tracker feed agrees with the standings and follows players through waivers."""
from __future__ import annotations

import json
from pathlib import Path

from gmbench.config import load_config
from gmbench.export.site import write_standings_export
from gmbench.export.tracker import write_tracker_export
from gmbench.ledger import Ledger
from gmbench.season.daily import run_scoring
from gmbench.season.scoring import GameLine
from gmbench.state import replay

TEAMS = [{"id": "opus", "lab": "Anthropic", "model": "anthropic/claude-opus-5.5"},
         {"id": "autodraft", "lab": "Control", "model": None}]


def _pick(no: int, team: str, pid: int, name: str, group: str = "F") -> dict:
    return {"pick_no": no, "round": (no + 1) // 2, "team": team, "player_id": pid, "player_name": name,
            "nhl_team": "EDM", "position": "C" if group == "F" else group, "group": group, "projected_points": 50,
            "auto": {"reason": "control_bot"} if team == "autodraft" else None}


def _line(day: str, pid: int, g: int, gid: int) -> GameLine:
    return GameLine(game_date=day, game_id=gid, player_id=pid, team="EDM", group="skater", goals=g, fantasy_points=g)


def _score(led: Ledger, games: Path, lines: list[GameLine]) -> None:
    run_scoring(led, start=None, end=None, games_dir=games, fetch=lambda s, e, t: lines, log=lambda _: None)


def test_tracker_matches_standings_and_follows_waivers(tmp_path: Path) -> None:
    cfg = load_config()
    led = Ledger(tmp_path / "l.jsonl")
    games, out = tmp_path / "games", tmp_path / "exports"
    led.append("LEAGUE_CREATED", "league", {"teams": TEAMS})
    led.append("DRAFT_ORDER_SET", "league", {"order": ["opus", "autodraft"]})
    led.append("PERSONA_CREATED", "gm:opus", {"team": "opus", "persona": {
        "franchise_name": "Outlook Overthinkers", "gm_name": "Professor Hootsworth", "primary_color": "#5A3A22",
        "private_notes": "never exported"}})
    for no, team, pid, name in [(1, "opus", 1, "One"), (2, "autodraft", 2, "Two"),
                                (3, "autodraft", 3, "Three"), (4, "opus", 4, "Four")]:
        led.append("DRAFT_PICK", f"gm:{team}", _pick(no, team, pid, name))
    led.append("LINEUP_SET", "gm:opus", {"team": "opus", "week": "2026-09-29", "active": [1], "bench": [4]})
    led.append("LINEUP_SET", "bot", {"team": "autodraft", "week": "2026-09-29", "active": [2, 3], "bench": []})
    # week 1: player 4 scores on opus's bench; player 9 is an unowned free agent
    _score(led, games, [_line("2026-09-30", 1, 2, 1), _line("2026-09-30", 4, 1, 1), _line("2026-09-30", 2, 1, 2),
                        _line("2026-09-30", 9, 3, 2)])
    # opus drops player 1 for free agent 9 and starts him in week 2
    led.append("WAIVERS_PROCESSED", "league", {"week": "2026-10-05", "results": [
        {"team": "opus", "add": 9, "drop": 1, "rank": 1, "status": "won", "add_group": "F", "drop_group": "F"}]})
    led.append("LINEUP_SET", "gm:opus", {"team": "opus", "week": "2026-10-05", "active": [9], "bench": [4]})
    _score(led, games, [_line("2026-10-06", 9, 1, 3), _line("2026-10-06", 1, 5, 4)])

    state = replay(led.events())
    (out).mkdir()
    (out / "tracker.json").write_text(json.dumps({"players": {"9": {"name": "Nine", "pos": "L", "group": "F",
                                                                    "nhl": "CGY"}}}))
    doc = json.loads(write_tracker_export(state, cfg, out, games_dir=games, today="2026-10-07").read_text())
    standings = json.loads(write_standings_export(state, cfg, out, today="2026-10-07").read_text())

    teams = {t["id"]: t for t in doc["teams"]}
    assert [t["id"] for t in doc["teams"]] == ["opus", "autodraft"]
    assert {r["team"]: r["points"] for r in standings["standings"]} == {t: teams[t]["points"] for t in teams}
    assert teams["opus"]["points"] == 3 and teams["opus"]["series"] == [2, 3]
    assert teams["opus"]["franchise"] == "Outlook Overthinkers" and "private_notes" not in json.dumps(doc)
    assert teams["autodraft"]["bot"] and teams["autodraft"]["franchise"] is None

    assert teams["opus"]["roster"] == [{"pid": 4, "counted": 0, "active": False},
                                       {"pid": 9, "counted": 1, "active": True}]
    assert teams["opus"]["former"] == [{"pid": 1, "counted": 2}]

    picks = {p["pid"]: p for p in doc["picks"]}
    assert picks[1]["owner"] is None and picks[4]["owner"] == "opus" and picks[2]["auto"]
    players = doc["players"]
    assert players["1"]["pts"] == 7 and players["1"]["gp"] == 2     # still scores after being dropped
    assert players["4"]["pts"] == 1                                  # bench points count for the player, not the team
    assert players["9"]["name"] == "Nine" and players["9"]["pts"] == 4  # name carried forward, points from game lines
    assert doc["days_scored"] == 2 and doc["last_game_date"] == "2026-10-06"
