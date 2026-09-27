"""League rules as pure functions: snake order, roster feasibility, lineups, scoring."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

GROUPS = ("F", "D", "G")


@dataclass(frozen=True)
class RosterRules:
    minimums: dict[str, int]  # active lineup slots per group
    bench: int
    max_goalies: int

    @property
    def roster_size(self) -> int:
        return sum(self.minimums.values()) + self.bench

    @classmethod
    def from_config(cls, rules: dict) -> RosterRules:
        lineup = rules["lineup"]
        return cls(
            minimums={g: int(lineup[g]) for g in GROUPS},
            bench=int(lineup["bench"]),
            max_goalies=int(rules.get("max_goalies", lineup["G"] + 1)),
        )


@dataclass(frozen=True)
class Slot:
    pick_no: int  # 1-based overall pick
    round: int  # 1-based
    team_id: str


def snake_order(order: list[str], rounds: int) -> list[Slot]:
    slots: list[Slot] = []
    for r in range(1, rounds + 1):
        seq = order if r % 2 == 1 else list(reversed(order))
        for team_id in seq:
            slots.append(Slot(pick_no=len(slots) + 1, round=r, team_id=team_id))
    return slots


def unmet_minimums(groups: Counter, rules: RosterRules) -> int:
    return sum(max(0, rules.minimums[g] - groups.get(g, 0)) for g in GROUPS)


def pick_problem(groups: Counter, player_group: str, rules: RosterRules) -> str | None:
    """Why this pick is illegal for a roster with these group counts, or None if legal.

    Feasibility: after the pick, the remaining open roster spots must still be able
    to fill every unmet lineup minimum (so a GM can never draft itself into an
    illegal lineup).
    """
    size = sum(groups.values())
    if size >= rules.roster_size:
        return f"roster is full ({rules.roster_size} players)"
    after = groups.copy()
    after[player_group] += 1
    if player_group == "G" and after["G"] > rules.max_goalies:
        return f"goalie limit is {rules.max_goalies}"
    spots_left = rules.roster_size - (size + 1)
    needed = unmet_minimums(after, rules)
    if needed > spots_left:
        short = ", ".join(
            f"{rules.minimums[g] - after.get(g, 0)} {g}" for g in GROUPS if after.get(g, 0) < rules.minimums[g]
        )
        return f"you would have {spots_left} open spots left but still need {short}"
    return None


def lineup_problem(roster_groups: dict[int, str], active: list[int], bench: list[int], rules: RosterRules) -> str | None:
    if sorted(active + bench) != sorted(roster_groups):
        return "active + bench must be exactly your roster, each player once"
    counts = Counter(roster_groups[p] for p in active)
    wrong = [f"{g}: {counts.get(g, 0)} (need {n})" for g, n in rules.minimums.items() if counts.get(g, 0) != n]
    if wrong:
        return "active lineup must be exactly " + ", ".join(f"{n} {g}" for g, n in rules.minimums.items()) + " — got " + "; ".join(wrong)
    return None


def fantasy_points(scoring: dict, *, group: str, goals: int = 0, assists: int = 0,
                   win: int = 0, otl: int = 0, shutout: int = 0) -> int:
    if group == "G":
        g = scoring["goalie"]
        return win * g["win"] + otl * g["otl"] + shutout * g["shutout"]
    s = scoring["skater"]
    return goals * s["goal"] + assists * s["assist"]
