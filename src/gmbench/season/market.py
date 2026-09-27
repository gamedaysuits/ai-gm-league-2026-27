"""Roster moves as pure functions: waiver claims and trades, validated and resolved in canonical order."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from gmbench.rules import GROUPS, RosterRules


@dataclass
class Claim:
    team: str
    add: int
    drop: int
    rank: int


@dataclass
class ClaimResult:
    team: str
    add: int
    drop: int
    rank: int
    status: str  # won | lost_to_higher_priority | add_unavailable | drop_not_on_roster | illegal_roster | over_limit


@dataclass
class Trade:
    id: str
    proposer: str
    recipient: str
    give: list[int]  # proposer's players
    get: list[int]  # recipient's players
    message: str = ""
    counter_of: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def roster_after(groups: Counter, out: list[str], into: list[str]) -> Counter:
    after = groups.copy()
    for g in out:
        after[g] -= 1
    for g in into:
        after[g] += 1
    return after


def roster_problem(after: Counter, rules: RosterRules) -> str | None:
    short = [f"{rules.minimums[g]} {g}" for g in GROUPS if after.get(g, 0) < rules.minimums[g]]
    if short:
        return "roster would fall below the lineup minimums (" + ", ".join(short) + ")"
    if after.get("G", 0) > rules.max_goalies:
        return f"roster would exceed {rules.max_goalies} goalies"
    return None


def claim_problem(roster: list[int], group_of: dict[int, str], owned: set[int], claim: Claim, rules: RosterRules) -> str | None:
    if claim.add in owned:
        return "add_unavailable"
    if claim.drop not in roster:
        return "drop_not_on_roster"
    groups = Counter(group_of[p] for p in roster)
    if roster_problem(roster_after(groups, [group_of[claim.drop]], [group_of[claim.add]]), rules):
        return "illegal_roster"
    return None


def resolve_waivers(rosters: dict[str, list[int]], group_of: dict[int, str], owned: set[int],
                    claims: dict[str, list[Claim]], priority: list[str], rules: RosterRules,
                    max_adds: int = 2) -> tuple[list[ClaimResult], dict[str, list[int]]]:
    """Round-robin passes in fixed priority order; each team wins at most one claim per pass.

    Independent of the order claims were submitted (only priority and each team's own ranking matter).
    """
    rosters = {t: list(r) for t, r in rosters.items()}
    owned = set(owned)
    results: list[ClaimResult] = []
    remaining = {t: sorted(claims.get(t, []), key=lambda c: c.rank) for t in priority}
    adds: Counter = Counter()
    for _ in range(max_adds):
        progressed = False
        for team in priority:
            while remaining[team]:
                claim = remaining[team].pop(0)
                problem = claim_problem(rosters[team], group_of, owned, claim, rules)
                if problem:
                    status = "lost_to_higher_priority" if problem == "add_unavailable" and claim.add in _won(results) else problem
                    results.append(ClaimResult(team, claim.add, claim.drop, claim.rank, status))
                    continue
                rosters[team].remove(claim.drop)
                rosters[team].append(claim.add)
                owned.discard(claim.drop)
                owned.add(claim.add)
                adds[team] += 1
                results.append(ClaimResult(team, claim.add, claim.drop, claim.rank, "won"))
                progressed = True
                break
        if not progressed:
            break
    for team in priority:
        for claim in remaining[team]:
            results.append(ClaimResult(team, claim.add, claim.drop, claim.rank, "over_limit"))
    return results, rosters


def _won(results: list[ClaimResult]) -> set[int]:
    return {r.add for r in results if r.status == "won"}


def trade_problem(trade: Trade, rosters: dict[str, list[int]], group_of: dict[int, str], rules: RosterRules,
                  *, bot_ids: set[str], max_per_side: int) -> str | None:
    if trade.proposer == trade.recipient:
        return "you can't trade with yourself"
    if trade.recipient in bot_ids or trade.proposer in bot_ids:
        return "the control bot doesn't trade"
    if not trade.give or len(trade.give) != len(trade.get):
        return "trades must swap an equal number of players (1 to 3 each way)"
    if len(trade.give) > max_per_side:
        return f"at most {max_per_side} players each way"
    if len(set(trade.give)) != len(trade.give) or len(set(trade.get)) != len(trade.get):
        return "duplicate player in trade"
    if any(p not in rosters[trade.proposer] for p in trade.give):
        return "proposer no longer has every player it offered"
    if any(p not in rosters[trade.recipient] for p in trade.get):
        return "recipient no longer has every player requested"
    give_g = [group_of[p] for p in trade.give]
    get_g = [group_of[p] for p in trade.get]
    for team, out, into in ((trade.proposer, give_g, get_g), (trade.recipient, get_g, give_g)):
        groups = Counter(group_of[p] for p in rosters[team])
        problem = roster_problem(roster_after(groups, out, into), rules)
        if problem:
            return f"{team}: {problem}"
    return None


def execute_trade(trade: Trade, rosters: dict[str, list[int]]) -> None:
    for p in trade.give:
        rosters[trade.proposer].remove(p)
        rosters[trade.recipient].append(p)
    for p in trade.get:
        rosters[trade.recipient].remove(p)
        rosters[trade.proposer].append(p)
