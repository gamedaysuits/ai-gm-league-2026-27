"""Waiver and trade resolution: legality, priority, and independence from submission order."""
from __future__ import annotations

import random

from gmbench.rules import RosterRules
from gmbench.season.market import Claim, Trade, execute_trade, resolve_waivers, trade_problem

RULES = RosterRules(minimums={"F": 2, "D": 1, "G": 1}, bench=1, max_goalies=2)
GROUP = {1: "F", 2: "F", 3: "F", 4: "D", 5: "G",  # team a
         11: "F", 12: "F", 13: "D", 14: "D", 15: "G",  # team b
         21: "F", 22: "D", 23: "G", 24: "F"}  # free agents
ROSTERS = {"a": [1, 2, 3, 4, 5], "b": [11, 12, 13, 14, 15]}


def test_priority_wins_contested_claim_and_losers_fall_through() -> None:
    claims = {"a": [Claim("a", 21, 3, 1)], "b": [Claim("b", 21, 11, 1), Claim("b", 24, 12, 2)]}
    results, rosters = resolve_waivers(ROSTERS, GROUP, {1, 2, 3, 4, 5, 11, 12, 13, 14, 15}, claims, ["b", "a"], RULES)
    won = {(r.team, r.add) for r in results if r.status == "won"}
    assert won == {("b", 21), ("b", 24)}  # b has priority: 21 in pass 1, its second claim in pass 2
    assert [r.status for r in results if r.team == "a"] == ["lost_to_higher_priority"]
    assert 21 in rosters["b"] and 11 not in rosters["b"] and 24 in rosters["b"]


def test_illegal_claim_is_refused() -> None:
    # a would drop its only D for a forward → below minimum D
    results, _ = resolve_waivers(ROSTERS, GROUP, set(GROUP) - {21, 22, 23, 24}, {"a": [Claim("a", 21, 4, 1)]}, ["a", "b"], RULES)
    assert results[0].status == "illegal_roster"


def test_result_independent_of_submission_order() -> None:
    base = {"a": [Claim("a", 21, 3, 1), Claim("a", 24, 2, 2)], "b": [Claim("b", 24, 12, 1), Claim("b", 21, 11, 2)]}
    owned = {1, 2, 3, 4, 5, 11, 12, 13, 14, 15}
    ref, _ = resolve_waivers(ROSTERS, GROUP, owned, base, ["a", "b"], RULES)
    for seed in range(5):
        shuffled = {t: random.Random(seed).sample(cs, len(cs)) for t, cs in base.items()}
        got, _ = resolve_waivers(ROSTERS, GROUP, owned, shuffled, ["a", "b"], RULES)
        assert [(r.team, r.add, r.status) for r in got if r.status == "won"] == [(r.team, r.add, r.status) for r in ref if r.status == "won"]


def test_trade_validation_and_execution() -> None:
    ok = Trade("t1", "a", "b", give=[3], get=[12])
    assert trade_problem(ok, ROSTERS, GROUP, RULES, bot_ids=set(), max_per_side=3) is None
    uneven = Trade("t2", "a", "b", give=[3, 2], get=[12])
    assert "equal" in trade_problem(uneven, ROSTERS, GROUP, RULES, bot_ids=set(), max_per_side=3)
    breaks_d = Trade("t3", "a", "b", give=[4], get=[11])  # a would have 0 D
    assert "minimums" in trade_problem(breaks_d, ROSTERS, GROUP, RULES, bot_ids=set(), max_per_side=3)
    assert "control bot" in trade_problem(ok, ROSTERS, GROUP, RULES, bot_ids={"b"}, max_per_side=3)
    rosters = {t: list(r) for t, r in ROSTERS.items()}
    execute_trade(ok, rosters)
    assert 12 in rosters["a"] and 3 in rosters["b"]
