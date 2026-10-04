"""Auction pools score goals + assists from committed game lines, resolving unlisted players by name."""
from __future__ import annotations

import json
from pathlib import Path

from gmbench.pool import Directory, check_markdown, check_pool, score_pool
from gmbench.season.scoring import PlayerTotal

POOL = {
    "name": "Test Pool", "slug": "test-pool", "season": "20262027", "count_from": "2026-09-30",
    "budget": 1000, "roster": {"F": 1, "D": 1}, "scoring": "goals + assists", "prizes": [800, 300],
    "teams": [
        {"manager": "Jeff H", "players": [
            {"slot": "F1", "listed": "Kucherov", "name": "Nikita Kucherov", "club": "TBL", "price": 300, "id": 1},
            {"slot": "D1", "listed": "Q. Hughes", "name": "Quinn Hughes", "club": "MIN", "price": 200},
        ]},
        {"manager": "Justin", "players": [
            {"slot": "F1", "listed": "Petterson", "name": "Elias Pettersson", "club": "VAN", "price": 60},
            {"slot": "F2", "listed": "Stennberg", "name": "Ivar Stenberg", "club": None, "price": 50},
        ]},
    ],
}


def _total(pid: int, name: str, position: str, teams: str) -> PlayerTotal:
    return PlayerTotal(player_id=pid, name=name, group="skater", position=position, teams=teams)


def _line(pid: int, goals: int, assists: int, team: str = "TBL", group: str = "skater") -> dict:
    return {"player_id": pid, "team": team, "group": group, "goals": goals, "assists": assists}


def test_pool_scores_from_count_from_and_resolves_names(tmp_path: Path) -> None:
    games = tmp_path / "games"
    games.mkdir()
    days = {
        "2026-09-29": [_line(1, 5, 5)],                                       # before count_from: ignored
        "2026-09-30": [_line(1, 1, 1), _line(2, 0, 2, "MIN"), _line(4, 1, 0, "VAN")],
        "2026-10-01": [_line(1, 0, 1), _line(3, 9, 9, "VAN"), _line(5, 0, 0, "UTA", "goalie")],
    }
    for day, lines in days.items():
        (games / f"{day}.json").write_text(json.dumps(lines))
    directory = Directory({
        2: _total(2, "Quinn Hughes", "D", "MIN"),
        3: _total(3, "Elias Pettersson", "C", "VAN"),   # the forward the pool drafted
        4: _total(4, "Elias Pettersson", "D", "VAN"),   # same name, defenceman
    })
    doc = score_pool(POOL, games, directory)

    jeff, justin = doc["teams"]
    assert doc["dates"] == ["2026-09-30", "2026-10-01"] and doc["days_scored"] == 2
    assert [jeff["id"], justin["id"]] == ["jeff-h", "justin"]
    assert jeff["points"] == 5 and jeff["series"] == [4, 5] and jeff["last_7"] == 5
    assert jeff["spent"] == 500 and jeff["left"] == 500
    kucherov, hughes = jeff["players"]
    assert (kucherov["matched"], kucherov["points"], kucherov["gp"]) == ("listed", 3, 2)
    assert (hughes["pid"], hughes["matched"], hughes["club"]) == (2, "exact", "MIN")
    pettersson, stenberg = justin["players"]
    assert (pettersson["pid"], pettersson["points"]) == (3, 18)          # position breaks the name tie
    assert stenberg["pid"] is None and stenberg["points"] == 0
    assert doc["unresolved"] == [{"manager": "Justin", "slot": "F2", "name": "Ivar Stenberg"}]


def test_pool_without_directory_scores_listed_ids_only(tmp_path: Path) -> None:
    doc = score_pool(POOL, tmp_path / "missing", None)
    assert doc["days_scored"] == 0 and doc["teams"][0]["points"] == 0
    assert len(doc["unresolved"]) == 3


def test_fuzzy_match_without_a_club_needs_a_unique_surname() -> None:
    directory = Directory({7: _total(7, "Ivar Stenberg", "C", "TOR")})
    assert directory.resolve("Ivar Stenberg", "", "F") == (7, "exact")
    assert directory.resolve("I. Stenberg", "", "F") == (7, "fuzzy")


def test_pool_check_reads_player_pages_and_searches_names() -> None:
    pages = {
        "https://api-web.nhle.com/v1/player/1/landing": {
            "firstName": {"default": "Nikita"}, "lastName": {"default": "Kucherov"}, "position": "R",
            "currentTeamAbbrev": "TBL", "isActive": True},
    }
    search = [{"playerId": "2", "name": "Quinn Hughes", "positionCode": "D", "teamAbbrev": "MIN", "active": True},
              {"playerId": "9", "name": "Jack Hughes", "positionCode": "C", "teamAbbrev": "NJD", "active": True}]

    def get_json(url: str):
        return (pages[url] if url in pages else search if "Hughes" in url else []), "sha"

    rows = {r["listed"]: r for r in check_pool(POOL, get_json)}
    assert (rows["Kucherov"]["id"], rows["Kucherov"]["how"], rows["Kucherov"]["flags"]) == (1, "listed id", [])
    assert (rows["Q. Hughes"]["id"], rows["Q. Hughes"]["team"]) == (2, "MIN")
    assert rows["Petterson"]["flags"][0].startswith("no single match")
    assert "| Jeff H | F1 | Kucherov | Nikita Kucherov | 1 |" in check_markdown(list(rows.values()))


def test_pool_check_falls_back_to_a_surname_search() -> None:
    pool = {"teams": [{"manager": "Jeff H", "players": [
        {"slot": "F3", "listed": "Savoie", "name": "Matthew Savoie", "club": None, "price": 10}]}]}
    found = [{"playerId": "8", "name": "Matt Savoie", "positionCode": "C", "teamAbbrev": "EDM", "active": True},
             {"playerId": "6", "name": "Carson Savoie", "positionCode": "D", "teamAbbrev": None, "active": False}]

    def get_json(url: str):
        return (found if url.lower().endswith("q=savoie") else []), "sha"

    (row,) = check_pool(pool, get_json)
    assert (row["id"], row["nhl_name"], row["how"], row["flags"]) == (8, "Matt Savoie", "surname search", [])
