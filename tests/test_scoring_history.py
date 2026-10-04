"""Rescore the two AI pools that preceded GM-Bench, from official NHL data by player ID.

* 2026 AI Playoff Pool: 12 GMs, 10 players each. Pool rules: goal 1, assist 1,
  goalie win 1, shutout +2 bonus (goalie goals and assists do not count).
  Tiebreak: the closest guess to total goals scored in the 2026 playoffs.
* GDS AI Hockey Draft 2025-26: an October 2025 auction, 10 GMs, 11 skaters
  each, $1,000 budget. Scored three ways: raw goals + assists, best 10 of 11,
  and best 10 minus 1 point per $10 left unspent.

Drafted names are resolved to NHL player IDs from the NHL's 2025-26 stats,
disambiguated by club and position, so stats are never joined on names.

Rebuild exports/history/:  uv run python tests/test_scoring_history.py
Check the standings:       GMBENCH_LIVE=1 uv run pytest tests/test_scoring_history.py
"""
from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable, Mapping
from itertools import groupby
from pathlib import Path
from typing import Any

import pytest
import yaml

from gmbench.config import ROOT
from gmbench.pool import Directory, norm
from gmbench.season import scoring as sc

SOURCE = Path(os.environ.get("GMBENCH_HISTORY_SOURCE", "/Users/gamedaysuits/local projects/AI Draft Regular Season"))
PLAYOFF_DIR = SOURCE / "AI-Draft-Playoffs"
PLAYOFF_CSV = PLAYOFF_DIR / "output" / "draft_results.csv"
PLAYOFF_ROSTER_IDS = PLAYOFF_DIR / "data" / "nhl_api_rosters.json"  # the pool's own draft-day ID list
REGULAR_CSV = SOURCE / "draft_results.csv"
OUT_DIR = ROOT / "exports" / "history"

SEASON = 20252026
PLAYOFF_WINDOW = ("2026-04-01", "2026-07-31")  # gameTypeId=3 selects the playoff games inside it
PLAYOFF_RULES = {"skater": {"goal": 1, "assist": 1}, "goalie": {"win": 1, "shutout": 2}}
GOAL_GUESSES = {
    "Grok": 674, "Claude": 700, "DeepSeek": 812, "Perplexity": 823, "GPT-5": 797, "Cohere": 725,
    "Llama 4": 742, "Gemini": 700, "Gemma": 224, "Hermes": 700, "Mistral": 700, "Qwen": 750,
}
CLUB_FIX = {"VEG": "VGK"}

BUDGET = 1000
ROSTER_SIZE = 11
# Team labels in the auction file -> the model that actually drafted in October 2025.
REGULAR_MODELS = {
    "Grok": ("Grok 4", "x-ai/grok-4"),
    "Claude": ("Claude 3.5 Sonnet", "anthropic/claude-3.5-sonnet"),
    "ChatGPT5": ("GPT-4o", "openai/gpt-4o"),
    "ChatGPT": ("GPT-4o mini", "openai/gpt-4o-mini"),
    "ClaudeOpus": ("Claude 3 Opus", "anthropic/claude-3-opus"),
    "Perplexity": ("Sonar", "perplexity/sonar"),
    "DeepSeek": ("DeepSeek V3.1", "deepseek/deepseek-chat-v3.1"),
    "QwQ": ("QwQ 32B", "qwen/qwq-32b"),
    "Mixtral": ("Mixtral 8x7B", "mistralai/mixtral-8x7b-instruct"),
    "MistralLarge": ("Mistral Small 3.1", "mistralai/mistral-small-3.1-24b-instruct"),
}
# Two Elias Petterssons played for VAN in 2025-26. The auction drafted the forward, not the defenceman (8483678).
OVERRIDES = {("elias pettersson", "VAN"): 8480012}

# Final standings, verified against the NHL data by these tests.
PLAYOFF_STANDINGS = [
    ("Gemini", 89), ("Llama 4", 88), ("GPT-5", 88), ("Qwen", 84), ("Perplexity", 76), ("Cohere", 69),
    ("Grok", 68), ("Gemma", 62), ("Hermes", 57), ("DeepSeek", 48), ("Claude", 44), ("Mistral", 37),
]
REGULAR_STANDINGS = {  # team: (raw, best 10, best 10 minus unspent), in best-10-minus-unspent order
    "Grok": (816, 777, 777), "Claude": (819, 784, 764), "Mixtral": (745, 728, 728),
    "DeepSeek": (727, 718, 718), "MistralLarge": (738, 710, 703), "Perplexity": (744, 712, 676),
    "ChatGPT5": (737, 693, 651), "ChatGPT": (709, 663, 587), "ClaudeOpus": (645, 606, 578), "QwQ": (609, 575, 575),
}


# --- 2026 AI Playoff Pool ----------------------------------------------------------


def build_playoff_pool() -> dict[str, Any]:
    lines = sc.fetch_game_lines(*PLAYOFF_WINDOW, sc.PLAYOFFS, scoring=PLAYOFF_RULES)
    playoff_totals = sc.fetch_season_totals(SEASON, sc.PLAYOFFS)
    mismatches = sc.reconcile(lines, playoff_totals)
    directory = Directory(sc.fetch_season_totals(SEASON, sc.REGULAR), playoff_totals, overrides=OVERRIDES)
    sums = sc.sum_lines(lines)
    total_goals = sum(ln.goals for ln in lines)
    config = yaml.safe_load((PLAYOFF_DIR / "config.yaml").read_text(encoding="utf-8"))
    models = {team: spec.get("slug") for team, spec in (config.get("models") or {}).items()}

    teams: dict[str, dict[str, Any]] = {}
    for row in _dict_rows(PLAYOFF_CSV):
        club = CLUB_FIX.get(row["NHL_Team"], row["NHL_Team"])
        pid, how = directory.resolve(row["Player"], club, row["Position"])
        s = sums.get(pid) or dict.fromkeys((*sc.TOTAL_FIELDS, "fantasy_points"), 0)
        team = teams.setdefault(row["Team"], {"team": row["Team"], "nickname": row["Nickname"], "players": []})
        team["players"].append({
            "pick": int(row["Pick_Order"]), "player": row["Player"], "nhl_name": directory.name(pid),
            "player_id": pid, "club": club, "position": row["Position"], "gp": s["gp"],
            "goals": s["goals"], "assists": s["assists"], "wins": s["win"], "shutouts": s["shutout"],
            "points": s["fantasy_points"], "no_playoff_games": pid not in sums, "matched_by": how,
        })

    ordered = sorted(
        teams.values(),
        key=lambda t: (-sum(p["points"] for p in t["players"]), abs(GOAL_GUESSES[t["team"]] - total_goals)),
    )
    standings = []
    for t in ordered:
        guess = GOAL_GUESSES[t["team"]]
        standings.append({
            "rank": 0, "team": t["team"], "nickname": t["nickname"], "model": models.get(t["team"]),
            "points": sum(p["points"] for p in t["players"]), "goals_guess": guess,
            "guess_off_by": abs(guess - total_goals),
            "players_with_games": sum(not p["no_playoff_games"] for p in t["players"]),
            "players": sorted(t["players"], key=lambda p: p["pick"]),
        })
    for t, rank in zip(standings, _ranks(standings, lambda t: (t["points"], t["guess_off_by"]))):
        t["rank"] = rank

    tiebreaks = []
    for points, tied in groupby(standings, key=lambda t: t["points"]):
        tied = list(tied)
        if len(tied) > 1:
            order = ", ahead of ".join(f"{t['team']} (guessed {t['goals_guess']}, off by {t['guess_off_by']})" for t in tied)
            tiebreaks.append(f"Tied on {points} points, broken by the goals guess: {order}.")

    dates = sorted({ln.game_date for ln in lines})
    return {
        "pool": "2026 AI Playoff Pool",
        "rules": {
            "scoring": PLAYOFF_RULES,
            "notes": "goal 1, assist 1, goalie win 1, shutout +2 bonus; goalie goals and assists do not count",
            "tiebreak": "closest guess to the total goals scored in the 2026 playoffs",
        },
        "stats": {
            "source": "NHL stats REST per-game summaries (skater/summary + goalie/summary, isGame=true), gameTypeId=3",
            "season": str(SEASON),
            "first_game": dates[0],
            "last_game": dates[-1],
            "games": len({ln.game_id for ln in lines}),
            "player_game_lines": len(lines),
            "total_goals": total_goals,
            "mismatches_vs_nhl_playoff_totals": len(mismatches),
        },
        "tiebreaks": tiebreaks,
        "teams": standings,
    }


def playoff_markdown(d: Mapping[str, Any]) -> str:
    s = d["stats"]
    out = [
        "# 2026 AI Playoff Pool: final standings",
        "",
        f"Rescored by NHL player ID from the NHL's per-game stats for all {s['games']} games of the 2026 Stanley Cup "
        f"Playoffs ({s['first_game']} to {s['last_game']}). Pool rules: goal 1, assist 1, goalie win 1, "
        "shutout +2 bonus. Goalie goals and assists do not count. Ties go to the closest guess of total playoff "
        f"goals, which came to **{s['total_goals']}**.",
        "",
        "| Rank | GM | Nickname | Model | Pts | Goals guess | Off by |",
        "|---:|:---|:---|:---|---:|---:|---:|",
    ]
    for t in d["teams"]:
        model = f"`{t['model']}`" if t["model"] else ""
        out.append(f"| {t['rank']} | {t['team']} | {t['nickname']} | {model} | **{t['points']}** "
                   f"| {t['goals_guess']} | {t['guess_off_by']} |")
    if d["tiebreaks"]:
        out += ["", *d["tiebreaks"]]
    out += ["", "## Rosters", "",
            "Points are under the pool rules. *No playoff games* marks a player who did not play in the 2026 playoffs."]
    for t in d["teams"]:
        out += ["", f"### {t['rank']}. {t['team']}, {t['nickname']}: {t['points']} pts", "",
                "| Pick | Player | Club | Pos | GP | G | A | W | SO | Pts |",
                "|---:|:---|:---|:---|---:|---:|---:|---:|---:|---:|"]
        for p in t["players"]:
            goalie = p["position"] == "G"
            name = p["player"] + (" (*no playoff games*)" if p["no_playoff_games"] else "")
            g, a = ("–", "–") if goalie else (p["goals"], p["assists"])
            w, so = (p["wins"], p["shutouts"]) if goalie else ("–", "–")
            out.append(f"| {p['pick']} | {name} | {p['club']} | {p['position']} | {p['gp']} | {g} | {a} | {w} | {so} "
                       f"| {p['points']} |")
    out += ["", "## Method", "",
            f"- Stats: {s['source']}, {s['player_game_lines']:,} player-game lines. Summed per player, they match the "
            "NHL's own 2026 playoff totals for every player (games, goals, assists, wins, losses, OT losses, shutouts): "
            f"{s['mismatches_vs_nhl_playoff_totals']} mismatches.",
            "- Drafted players are matched to NHL player IDs by name, club and position, using the NHL's 2025-26 stats. "
            "Club codes are as drafted, except VEG, which is Vegas (VGK)."]
    out += _renamed_lines(p for t in d["teams"] for p in t["players"])
    return "\n".join(out) + "\n"


# --- GDS AI Hockey Draft 2025-26 (regular-season auction) --------------------------


def build_regular_pool() -> dict[str, Any]:
    totals = sc.fetch_season_totals(SEASON, sc.REGULAR)
    season_lines = sc.fetch_game_lines("2025-09-01", "2026-05-31", sc.REGULAR)  # cross-check only
    mismatches = sc.reconcile(season_lines, totals)
    directory = Directory(totals, overrides=OVERRIDES)
    with REGULAR_CSV.open(newline="", encoding="utf-8-sig") as fh:
        header, *rows = list(csv.reader(fh))
    name_col, club_col = header.index("Name"), header.index("Team")

    rosters: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        drafted_by, price = row[-2], row[-1]  # one row has a stray extra field, so read these from the end
        if not drafted_by:
            continue
        pid, how = directory.resolve(row[name_col], row[club_col])
        t = totals[pid]
        rosters.setdefault(drafted_by, []).append({
            "player": row[name_col], "nhl_name": t.name, "player_id": pid, "position": t.position,
            "club_at_draft": row[club_col], "nhl_clubs": t.teams, "price": int(price), "gp": t.gp,
            "goals": t.goals, "assists": t.assists, "points": t.goals + t.assists, "matched_by": how,
        })

    teams = []
    for label, players in rosters.items():
        if len(players) != ROSTER_SIZE:
            raise ValueError(f"{label} has {len(players)} players, expected {ROSTER_SIZE}")
        players.sort(key=lambda p: (-p["points"], -p["price"], p["player"]))
        dropped = players[-1]
        spent = sum(p["price"] for p in players)
        raw = sum(p["points"] for p in players)
        best10 = raw - dropped["points"]
        model, slug = REGULAR_MODELS[label]
        for p in players:
            p["in_best10"] = p is not dropped
        teams.append({
            "team": label, "model": model, "model_slug": slug, "spent": spent, "unspent": BUDGET - spent,
            "raw": raw, "best10": best10, "dropped": dropped["player"],
            "best10_minus_unspent": best10 - (BUDGET - spent) // 10,  # prices move in $10 steps
            "players": players,
        })

    for variant in ("raw", "best10", "best10_minus_unspent"):
        ordered = sorted(teams, key=lambda t: -t[variant])
        for t, rank in zip(ordered, _ranks(ordered, lambda t: t[variant])):
            t[f"rank_{variant}"] = rank
    teams.sort(key=lambda t: (t["rank_best10_minus_unspent"], t["team"]))
    return {
        "pool": "GDS AI Hockey Draft 2025-26 (regular-season auction)",
        "rules": {
            "budget": BUDGET,
            "roster_size": ROSTER_SIZE,
            "points": "NHL regular-season goals + assists",
            "variants": {
                "raw": "goals + assists of all 11 players",
                "best10": "drop each team's lowest scorer",
                "best10_minus_unspent": "best 10, minus 1 point per $10 of budget left unspent",
            },
        },
        "stats": {
            "source": "NHL stats REST season summaries (skater/summary, isGame=false), seasonId=20252026, gameTypeId=2",
            "season": str(SEASON),
            "games": len({ln.game_id for ln in season_lines}),
            "player_game_lines": len(season_lines),
            "mismatches_vs_summed_game_lines": len(mismatches),
        },
        "teams": teams,
    }


def regular_markdown(d: Mapping[str, Any]) -> str:
    out = [
        "# GDS AI Hockey Draft 2025-26: final standings",
        "",
        "October 2025 auction draft: 10 AI GMs, $1,000 budget, 11 skaters each. Rescored by NHL player ID with the "
        "official 2025-26 regular-season goals and assists, counted three ways:",
        "",
        "- **Raw**: goals + assists of all 11 players.",
        "- **Best 10**: each team's lowest scorer is dropped.",
        "- **Best 10 − unspent**: best 10, minus 1 point per $10 of budget left unspent.",
        "",
        "Rank is by best 10 − unspent. The other columns show each variant's rank in brackets.",
        "",
        "| Rank | GM (file label) | Model | Spent | Best 10 − unspent | Best 10 | Raw |",
        "|---:|:---|:---|---:|---:|---:|---:|",
    ]
    for t in d["teams"]:
        out.append(f"| {t['rank_best10_minus_unspent']} | {t['team']} | {t['model']} (`{t['model_slug']}`) "
                   f"| ${t['spent']:,} | **{t['best10_minus_unspent']}** | {t['best10']} ({t['rank_best10']}) "
                   f"| {t['raw']} ({t['rank_raw']}) |")
    out += ["", "## Rosters"]
    for t in d["teams"]:
        unspent = f"${t['unspent']:,} unspent (−{t['unspent'] // 10})" if t["unspent"] else "nothing unspent"
        out += ["", f"### {t['rank_best10_minus_unspent']}. {t['team']} ({t['model']}): {t['best10_minus_unspent']}", "",
                f"Spent ${t['spent']:,}, {unspent}. Raw {t['raw']}, best 10 {t['best10']}.", "",
                "| Player | Club | Pos | Price | GP | G | A | Pts |",
                "|:---|:---|:---|---:|---:|---:|---:|---:|"]
        for p in t["players"]:
            name = p["player"] + ("" if p["in_best10"] else " (*dropped from best 10*)")
            out.append(f"| {name} | {p['nhl_clubs']} | {p['position']} | ${p['price']} | {p['gp']} | {p['goals']} "
                       f"| {p['assists']} | {p['points']} |")
    s = d["stats"]
    out += ["", "## Method", "",
            f"- Stats: {s['source']}, by player ID. As a cross-check, the {s['player_game_lines']:,} per-game lines of "
            f"all {s['games']:,} regular-season games were summed per player and compared with these totals: "
            f"{s['mismatches_vs_summed_game_lines']} mismatches.",
            "- Drafted players are matched to NHL player IDs by name and club, using the NHL's 2025-26 stats. "
            "Club shows every NHL club the player played for in 2025-26.",
            "- GM names are the labels in the draft file. ChatGPT5 was actually GPT-4o, and MistralLarge was "
            "Mistral Small 3.1.",
            "- One row of the source file (Drake Batherson) has a stray extra field, so DraftedBy and Price are read "
            "as each row's last two fields."]
    out += _renamed_lines(p for t in d["teams"] for p in t["players"])
    return "\n".join(out) + "\n"


# --- shared helpers -----------------------------------------------------------------


def _dict_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _ranks(items: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> list[int]:
    """Competition ranks (1, 2, 2, 4) for items already sorted best first."""
    ranks: list[int] = []
    for i, item in enumerate(items):
        ranks.append(ranks[-1] if i and key(item) == key(items[i - 1]) else i + 1)
    return ranks


def _renamed_lines(players) -> list[str]:
    notes = []
    for p in players:
        if p["matched_by"] == "override":
            notes.append(f"  - {p['player']}: the forward, {p['nhl_name']} ({p['player_id']}, {p['position']}). "
                         "A defenceman with the same name also played for Vancouver in 2025-26.")
        elif p["matched_by"] == "fuzzy":
            notes.append(f"  - {p['player']} is {p['nhl_name']} ({p['player_id']}) in NHL data.")
    return ["- Name notes:", *notes] if notes else []


def write_exports(out_dir: Path = OUT_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for stem, data, render in (
        ("playoffs-2026", build_playoff_pool(), playoff_markdown),
        ("regular-2025-26", build_regular_pool(), regular_markdown),
    ):
        for path, text in ((out_dir / f"{stem}.json", json.dumps(data, indent=2, ensure_ascii=False) + "\n"),
                           (out_dir / f"{stem}.md", render(data))):
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written


# --- tests (live: NHL API + the pools' source files) --------------------------------

pytestmark = pytest.mark.skipif(
    os.environ.get("GMBENCH_LIVE") != "1" or not SOURCE.exists(),
    reason="live NHL API check on the historical pool files; set GMBENCH_LIVE=1",
)


@pytest.fixture(scope="module")
def playoff_pool() -> dict[str, Any]:
    return build_playoff_pool()


@pytest.fixture(scope="module")
def regular_pool() -> dict[str, Any]:
    return build_regular_pool()


def test_playoff_pool_standings(playoff_pool):
    assert [(t["team"], t["points"]) for t in playoff_pool["teams"]] == PLAYOFF_STANDINGS
    assert [t["rank"] for t in playoff_pool["teams"]] == list(range(1, 13))  # the goals guess broke every tie
    assert playoff_pool["stats"]["total_goals"] == 486
    assert playoff_pool["stats"]["mismatches_vs_nhl_playoff_totals"] == 0


def test_playoff_pool_idle_players_and_name_matches(playoff_pool):
    players = [p for t in playoff_pool["teams"] for p in t["players"]]
    assert {p["player"] for p in players if p["no_playoff_games"]} == {
        "Adin Hill", "Roope Hintz", "Victor Hedman", "Sam Montembeault", "Kevin Fiala"}
    assert {p["player"]: p["nhl_name"] for p in players if p["matched_by"] != "exact"} == {
        "Sam Montembeault": "Samuel Montembeault", "Mats Zuccarello": "Mats Zuccarello Aasen",
        "Matthew Savoie": "Matt Savoie"}
    zuccarello = next(p for p in players if p["player"] == "Mats Zuccarello")
    assert (zuccarello["player_id"], zuccarello["points"]) == (8475692, 9)  # the points a name-only match misses


def test_playoff_pool_ids_agree_with_the_pools_own_roster_file(playoff_pool):
    listed = {(norm(r["name"]), r["team"]): r["id"] for r in json.loads(PLAYOFF_ROSTER_IDS.read_text())}
    checked = 0
    for p in (p for t in playoff_pool["teams"] for p in t["players"]):
        pool_id = listed.get((norm(p["player"]), p["club"])) or listed.get((norm(p["nhl_name"]), p["club"]))
        if pool_id is not None:  # the file has no VGK players
            assert pool_id == p["player_id"], p
            checked += 1
    assert checked >= 100


def test_regular_pool_standings(regular_pool):
    teams = regular_pool["teams"]
    assert {t["team"]: (t["raw"], t["best10"], t["best10_minus_unspent"]) for t in teams} == REGULAR_STANDINGS
    assert [t["team"] for t in teams] == list(REGULAR_STANDINGS)
    assert all(t["unspent"] % 10 == 0 and len(t["players"]) == ROSTER_SIZE for t in teams)
    assert regular_pool["stats"]["games"] == 1312
    assert regular_pool["stats"]["mismatches_vs_summed_game_lines"] == 0


def test_regular_pool_elias_pettersson_is_the_forward(regular_pool):
    [p] = [p for t in regular_pool["teams"] for p in t["players"] if p["player"] == "Elias Pettersson"]
    assert (p["player_id"], p["position"], p["points"]) == (8480012, "C", 51)


def test_exports_are_current(playoff_pool, regular_pool):
    for stem, data in (("playoffs-2026", playoff_pool), ("regular-2025-26", regular_pool)):
        path = OUT_DIR / f"{stem}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == data, (
            f"{path} is stale; rebuild with: uv run python tests/test_scoring_history.py")


if __name__ == "__main__":
    for written in write_exports():
        print(written)
