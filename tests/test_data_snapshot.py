"""Snapshot assembly (with fetchers stubbed, no network) and canonical save/load."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from gmbench.data import espn, nhl
from gmbench.data import snapshot as snapmod
from gmbench.data.models import NewsItem, Player, SeasonLine, Snapshot
from gmbench.data.snapshot import load_snapshot, save_snapshot, snapshot_bytes


def _roster(pid, first, last, team, pos, born="1997-10-02"):
    return {"id": pid, "first_name": first, "last_name": last, "team": team, "position": pos, "sweater": None,
            "shoots": "L", "birth_date": born}  # fmt: skip


def _line(season, gp, g, a, name, pos="C", teams="EDM"):
    return {"season": season, "teams": teams, "name": name, "position": pos, "gp": gp, "goals": g, "assists": a,
            "points": g + a, "toi_per_game_s": 1000.0}  # fmt: skip


def _gline(season, gs, w, otl, so, name, teams="EDM"):
    return {"season": season, "teams": teams, "name": name, "position": "G", "gp": gs, "gs": gs, "wins": w,
            "losses": gs - w - otl, "ot_losses": otl, "shutouts": so, "goals": 0, "assists": 0, "points": 0}  # fmt: skip


ROSTERS = [
    _roster(1, "Connor", "Test", "EDM", "C", born="1997-01-13"),
    _roster(2, "Goalie", "One", "EDM", "G"),
    _roster(3, "Goalie", "Two", "EDM", "G"),
    _roster(4, "Elias", "Pettersson", "VAN", "D", born="2004-02-12"),
    _roster(5, "Goalie", "Three", "VAN", "G"),
    _roster(6, "Goalie", "Four", "VAN", "G"),
]
LINES = {
    1: [_line("20252026", 82, 48, 90, "Connor Test"), _line("20242025", 67, 26, 74, "Connor Test"),
        _line("20232024", 76, 32, 100, "Connor Test"), _line("20222023", 82, 64, 89, "Connor Test")],
    2: [_gline("20252026", 50, 28, 6, 3, "Goalie One")],
    4: [_line("20252026", 70, 2, 8, "Elias Pettersson", pos="D", teams="VAN")],
    98: [_line("20252026", 60, 10, 10, "Retired Vet", teams="VAN")],     # landing: inactive
    99: [_line("20252026", 70, 20, 25, "Injured Star", teams="VAN")],    # landing: active with VAN
    97: [_line("20232024", 40, 5, 5, "Long Term", pos="D", teams="EDM")],  # ESPN-injured, no 2025-26 games
}  # fmt: skip
INJURIES = {"injuries": [
    {"id": "6", "displayName": "Edmonton Oilers", "injuries": [
        {"status": "Day-To-Day", "date": "2026-09-25T10:00Z", "shortComment": "Maintenance day, expected to play.",
         "athlete": {"firstName": "Connor", "lastName": "Test", "displayName": "Connor Test",
                     "position": {"abbreviation": "C"}}},
        {"status": "Injured Reserve", "date": "2026-09-20T10:00Z", "shortComment": "",
         "athlete": {"firstName": "Long", "lastName": "Term", "displayName": "Long Term",
                     "position": {"abbreviation": "D"}}},
    ]},
    {"id": "22", "displayName": "Vancouver Canucks", "injuries": [
        {"status": "Out", "date": "2026-09-24T10:00Z", "shortComment": "Out two weeks with a lower-body injury.",
         "athlete": {"firstName": "Injured", "lastName": "Star", "displayName": "Injured Star",
                     "position": {"abbreviation": "RW"}}},
    ]},
]}  # fmt: skip
GAME_DATES = [(date(2026, 9, 29) + timedelta(days=2 * i)).isoformat() for i in range(84)]


@pytest.fixture
def stubbed(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(nhl, "TEAMS", ("EDM", "VAN"))

    def fetch_rosters(season, *, sources=None):
        sources[f"nhl:roster:*:{season}"] = "a" * 64
        return [dict(r) for r in ROSTERS]

    def fetch_season_lines(first, last, game_type=2, *, sources=None):
        calls["window"] = (first, last)
        return {pid: [dict(d) for d in ds] for pid, ds in LINES.items()}

    def fetch_team_games(season, *, sources=None, first_puck=None):
        return {"EDM": GAME_DATES, "VAN": GAME_DATES}

    def fetch_contracted_players(ids, *, sources=None, failures=None):
        calls["landing_ids"] = sorted(ids)
        pages = {99: _roster(99, "Injured", "Star", "VAN", "R"), 97: _roster(97, "Long", "Term", "EDM", "D")}
        return [pages[i] for i in sorted(ids) if i in pages]

    def fetch_news(players, limit=50, *, sources=None, stats=None):
        return [NewsItem(published="2026-09-26T00:00:00Z", headline="Camp notes", teams=["EDM"], player_ids=[1])]

    monkeypatch.setattr(nhl, "fetch_rosters", fetch_rosters)
    monkeypatch.setattr(nhl, "fetch_season_lines", fetch_season_lines)
    monkeypatch.setattr(nhl, "fetch_team_games", fetch_team_games)
    monkeypatch.setattr(nhl, "fetch_contracted_players", fetch_contracted_players)
    monkeypatch.setattr(espn, "fetch_injuries_payload", lambda *, sources=None: INJURIES)
    monkeypatch.setattr(espn, "fetch_news", fetch_news)
    return calls


def test_build_snapshot_assembles_players(stubbed):
    report: dict = {}
    snap = snapmod.build_snapshot("test", "2026-09-28", report=report)
    assert (snap.kind, snap.season, snap.as_of_date) == ("test", "20262027", "2026-09-28")
    assert stubbed["window"] == ("20232024", "20262027")
    # off-roster check: played last season (98, 99) or ESPN-injured with an exact stats-name match (97)
    assert stubbed["landing_ids"] == [97, 98, 99]
    assert sorted(snap.players) == [1, 2, 3, 4, 5, 6, 97, 99]
    assert not snap.players[99].on_roster and snap.players[99].team == "VAN"
    assert snap.players[1].on_roster

    mcd = snap.players[1]
    assert [s.season for s in mcd.seasons] == ["20252026", "20242025", "20232024"]  # newest first, max 3
    assert mcd.seasons[0].fantasy_points == 138
    assert mcd.age == 29.71  # (2026-10-01 - 1997-01-13) / 365.25
    assert mcd.projection and mcd.projection.method.startswith("gds-baseline-v1")
    assert mcd.injury and mcd.injury.status == "Day-To-Day"
    assert snap.players[99].injury.status == "Out" and snap.players[97].injury.status == "Injured Reserve"

    goalie = snap.players[2]
    assert goalie.group == "G" and goalie.seasons[0].gs == 50 and goalie.seasons[0].fantasy_points == 2 * 28 + 6 + 2 * 3
    assert snap.players[3].seasons == [] and snap.players[3].projection.games == 5.0  # no NHL games
    assert report["season_games"] == 84 and report["injuries"]["matched"] == 3
    assert snap.team_games["EDM"][0] == "2026-09-29" and len(snap.news) == 1


def test_save_and_load_are_canonical(tmp_path):
    def make(order):
        players = {
            pid: Player(id=pid, name=f"P {pid}", first_name="P", last_name=str(pid), team="EDM", position="C",
                        group="F", seasons=[SeasonLine(season="20252026", gp=10, goals=1, assists=2, points=3,
                                                       fantasy_points=3)])
            for pid in order
        }  # fmt: skip
        return Snapshot(kind="test", taken_at="2026-09-26T00:00:00Z", season="20262027", as_of_date="2026-09-28",
                        players=players, team_games={"EDM": ["2026-09-29"]}, sources={"x": "0" * 64})  # fmt: skip

    a, b = make([8478402, 8480012]), make([8480012, 8478402])
    assert snapshot_bytes(a) == snapshot_bytes(b)
    path = save_snapshot(a, tmp_path)
    assert path.parent == tmp_path and path.name.startswith("test-2026-09-28-") and len(path.stem.split("-")[-1]) == 12
    assert save_snapshot(b, tmp_path) == path
    loaded = load_snapshot(path)
    assert loaded == a and set(loaded.players) == {8478402, 8480012}
    assert snapshot_bytes(loaded) == path.read_bytes()
    assert list(json.loads(path.read_bytes())) == sorted(json.loads(path.read_bytes()))

    tampered = path.read_bytes().replace(b'"gp":10', b'"gp":11')
    path.write_bytes(tampered)
    with pytest.raises(ValueError):
        load_snapshot(path)
    assert load_snapshot(path, verify=False).players[8478402].seasons[0].gp == 11
