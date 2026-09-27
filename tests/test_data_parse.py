"""Parser tests on tiny inline fixtures shaped like the live NHL / ESPN payloads (no network)."""
from __future__ import annotations

from gmbench.data.espn import PlayerIndex, nhl_team, normalize_name, parse_injuries, parse_news
from gmbench.data.models import Player, SeasonLine
from gmbench.data.nhl import merge_season_rows, parse_player_landing, parse_roster, parse_schedule

ROSTER = {
    "forwards": [
        {"id": 8478402, "firstName": {"default": "Connor"}, "lastName": {"default": "McDavid"}, "positionCode": "C",
         "sweaterNumber": 97, "shootsCatches": "L", "birthDate": "1997-01-13"},
    ],
    "defensemen": [
        {"id": 8477934, "firstName": {"default": "Evan"}, "lastName": {"default": "Bouchard"}, "positionCode": "D",
         "sweaterNumber": 2, "shootsCatches": "R", "birthDate": "1999-10-20"},
    ],
    "goalies": [
        {"id": 8475883, "firstName": {"default": "Camp"}, "lastName": {"default": "Invitee"}, "positionCode": "G",
         "shootsCatches": "L", "birthDate": "2005-02-01"},
    ],
}  # fmt: skip


def test_parse_roster_flattens_groups_and_attaches_team():
    players = parse_roster(ROSTER, "EDM")
    assert [p["id"] for p in players] == [8478402, 8477934, 8475883]
    assert {p["team"] for p in players} == {"EDM"}
    assert players[0] == {
        "id": 8478402, "first_name": "Connor", "last_name": "McDavid", "team": "EDM", "position": "C",
        "sweater": 97, "shoots": "L", "birth_date": "1997-01-13",
    }  # fmt: skip
    assert players[2]["sweater"] is None


def test_merge_season_rows_joins_reports_newest_first():
    summary = [
        {"playerId": 1, "seasonId": 20242025, "skaterFullName": "A B", "teamAbbrevs": "TOR,VAN", "positionCode": "C",
         "gamesPlayed": 80, "goals": 30, "assists": 40, "points": 70, "plusMinus": 5, "ppPoints": 20, "shots": 200,
         "timeOnIcePerGame": 1100.123},
        {"playerId": 1, "seasonId": 20252026, "skaterFullName": "A B", "teamAbbrevs": "VAN", "positionCode": "C",
         "gamesPlayed": 82, "goals": 35, "assists": 45, "points": 80, "plusMinus": -3, "ppPoints": 25, "shots": 210,
         "timeOnIcePerGame": 1150.0},
    ]
    toi = [{"playerId": 1, "seasonId": 20252026, "ppTimeOnIcePerGame": 190.97058, "timeOnIcePerGame": 1150.0}]
    goalies = [
        {"playerId": 2, "seasonId": 20252026, "goalieFullName": "G K", "teamAbbrevs": "DET,ANA", "gamesPlayed": 13,
         "gamesStarted": 11, "wins": 2, "losses": 6, "otLosses": 3, "shutouts": 0, "savePct": 0.88988,
         "goalsAgainstAverage": 3.47001, "goals": 0, "assists": 1, "points": 1},
    ]  # fmt: skip
    merged = merge_season_rows(summary, toi, goalies)
    skater = [SeasonLine.model_validate(d) for d in merged[1]]
    assert [s.season for s in skater] == ["20252026", "20242025"]
    assert skater[0].pp_toi_per_game_s == 191.0 and skater[1].pp_toi_per_game_s is None
    assert skater[1].teams == "TOR,VAN" and skater[1].toi_per_game_s == 1100.1
    assert skater[0].gs is None and merged[1][0]["position"] == "C" and merged[1][0]["name"] == "A B"
    goalie = SeasonLine.model_validate(merged[2][0])
    assert (goalie.gs, goalie.wins, goalie.ot_losses, goalie.save_pct, goalie.gaa) == (11, 2, 3, 0.8899, 3.47)
    assert merged[2][0]["position"] == "G"


def test_parse_schedule_keeps_sorted_unique_regular_season_dates():
    payload = {"games": [
        {"gameDate": "2026-10-03", "gameType": 2},
        {"gameDate": "2026-09-19", "gameType": 1},
        {"gameDate": "2026-09-29", "gameType": 2},
        {"gameDate": "2026-09-29", "gameType": 2},
        {"gameDate": "2027-04-20", "gameType": 3},
    ]}  # fmt: skip
    assert parse_schedule(payload) == ["2026-09-29", "2026-10-03"]


def test_parse_player_landing_requires_active_nhl_club():
    page = {"playerId": 8478007, "isActive": True, "currentTeamAbbrev": "CBJ", "firstName": {"default": "Elvis"},
            "lastName": {"default": "Merzlikins"}, "position": "G", "sweaterNumber": 90, "shootsCatches": "L",
            "birthDate": "1994-04-13"}  # fmt: skip
    assert parse_player_landing(page)["team"] == "CBJ"
    assert parse_player_landing(page | {"isActive": False}) is None
    assert parse_player_landing(page | {"currentTeamAbbrev": None}) is None


# --------------------------------------------------------------------------- ESPN


def test_normalize_name():
    assert normalize_name("Tim Stützle") == "tim stutzle"
    assert normalize_name("Jonas Røndbjerg") == "jonas rondbjerg"
    assert normalize_name("J.T. Miller") == "jt miller"
    assert normalize_name("Ryan O'Reilly") == "ryan oreilly"
    assert normalize_name("Oliver Ekman-Larsson") == "oliver ekman larsson"
    assert normalize_name("  Martin  St. Louis Jr. ") == "martin st louis"


def test_nhl_team_mapping():
    assert nhl_team("Los Angeles Kings") == "LAK"
    assert nhl_team("Montréal Canadiens") == "MTL"
    assert nhl_team(None, "LA") == nhl_team(None, "la") == "LAK"
    assert nhl_team(None, "UTAH") == nhl_team(None, None, 129764) == "UTA"
    assert nhl_team(None, "TOR") == "TOR"
    assert nhl_team("Springfield Isotopes", "SPR", 999) is None


def _p(pid, first, last, team, pos, group):
    return Player(id=pid, name=f"{first} {last}", first_name=first, last_name=last, team=team, position=pos, group=group)


PLAYERS = [
    _p(8480012, "Elias", "Pettersson", "VAN", "C", "F"),
    _p(8483678, "Elias", "Pettersson", "VAN", "D", "D"),
    _p(8478427, "Sebastian", "Aho", "CAR", "C", "F"),
    _p(8480222, "Sebastian", "Aho", "NYI", "D", "D"),
    _p(8479318, "Matthew", "Knies", "TOR", "L", "F"),
    _p(8476453, "Nikita", "Kucherov", "TBL", "R", "F"),
    _p(8481540, "Tim", "Stützle", "OTT", "C", "F"),
]


def _injury(first, last, pos, status, date, short="", details=None):
    return {"status": status, "date": date, "shortComment": short, "longComment": "Longer note about the injury.",
            "details": details or {}, "athlete": {"firstName": first, "lastName": last, "displayName": f"{first} {last}",
                                                  "position": {"abbreviation": pos}}}  # fmt: skip


def test_parse_injuries_matches_namesakes_nicknames_and_moves():
    payload = {"injuries": [
        {"id": "22", "displayName": "Vancouver Canucks", "injuries": [
            _injury("Elias", "Pettersson", "D", "Out", "2026-09-20T10:00Z"),
            _injury("Elias", "Pettersson", "C", "Day-To-Day", "2026-09-21T10:00Z"),
        ]},
        {"id": "21", "displayName": "Toronto Maple Leafs", "injuries": [
            _injury("Matt", "Knies", "LW", "Out", "2026-09-22T10:00Z", "Knies (upper body) will miss the opener.",
                    {"type": "Upper Body", "detail": "Not Specified", "returnDate": "2026-10-10"}),
            _injury("Matt", "Knies", "LW", "Injured Reserve", "2026-09-25T10:00Z", "Placed on IR ahead of the season."),
            _injury("Henry", "Prospect", "D", "Out", "2026-09-22T10:00Z"),
        ]},
        {"id": "7", "displayName": "Carolina Hurricanes", "injuries": [
            _injury("Sebastian", "Aho", "C", "Out", "2026-09-23T10:00Z"),
        ]},
        {"id": "20", "displayName": "Tampa Bay Lightning", "injuries": [  # Stützle listed under a stale club
            _injury("Tim", "Stutzle", "C", "Out", "2026-09-24T10:00Z"),
        ]},
    ]}  # fmt: skip
    stats: dict = {}
    injuries, unmatched = parse_injuries(payload, PLAYERS, stats=stats)
    assert injuries[8483678].status == "Out" and injuries[8480012].status == "Day-To-Day"
    assert injuries[8478427].status == "Out" and 8480222 not in injuries
    assert injuries[8479318].status == "Injured Reserve"  # newest report wins
    assert injuries[8481540].status == "Out"  # unique name, club mismatch
    assert [u["name"] for u in unmatched] == ["Henry Prospect"]
    assert stats == {"entries": 7, "matched": 6, "players": 5}
    first_knies = parse_injuries({"injuries": [payload["injuries"][1] | {"injuries": payload["injuries"][1]["injuries"][:1]}]}, PLAYERS)
    assert first_knies[0][8479318].note == "[Upper Body; est. return 2026-10-10] Knies (upper body) will miss the opener."


def test_player_index_refuses_ambiguous_names_without_context():
    index = PlayerIndex(PLAYERS)
    assert index.match("Elias Pettersson", teams={"VAN"}) is None
    assert index.match("Elias Pettersson", teams={"VAN"}, group="D") == 8483678
    assert index.match("Sebastian Aho") is None
    assert index.match("Sebastian Aho", teams={"NYI"}) == 8480222
    assert index.match("Sebastian Aho", teams={"BOS"}, group="G") is None  # off-club hit must not contradict group
    assert index.match("Nikita Kucherov") == 8476453


def test_parse_news_tags_clubs_and_players():
    def cat_team(tid, name, ab):
        return {"type": "team", "teamId": tid, "description": name, "team": {"id": tid, "abbreviation": ab}}

    def cat_athlete(name):
        return {"type": "athlete", "athleteId": 1, "description": name}

    payload = {"articles": [
        {"headline": "Older story", "published": "2026-09-24T12:00:00Z", "description": "body text never stored",
         "categories": [cat_team(12, "New York Islanders", "NYI"), cat_athlete("Sebastian Aho")]},
        {"headline": "  Canucks   notebook ", "published": "2026-09-26T02:50:09Z",
         "categories": [cat_team(22, "Vancouver Canucks", "VAN"), cat_team(8, "Los Angeles Kings", "LA"),
                        cat_athlete("Elias Pettersson"), cat_athlete("Unknown Prospect"),
                        {"type": "league", "description": "NHL"}]},
        {"headline": "", "published": "2026-09-26T03:00:00Z", "categories": []},
    ]}  # fmt: skip
    stats: dict = {}
    news = parse_news(payload, PLAYERS, stats=stats)
    assert [n.headline for n in news] == ["Canucks notebook", "Older story"]
    assert news[0].teams == ["LAK", "VAN"] and news[0].player_ids == []  # two VAN Elias Petterssons: ambiguous
    assert news[1].teams == ["NYI"] and news[1].player_ids == [8480222]
    assert stats["athlete_tags"] == 3 and stats["athlete_tags_matched"] == 1
    assert stats["unmatched_athletes"] == ["Elias Pettersson", "Unknown Prospect"]
    assert "body" not in news[1].model_dump_json()
