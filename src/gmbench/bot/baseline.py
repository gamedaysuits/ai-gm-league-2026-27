"""The autodraft control bot: deterministic, transparent, never trades.

It drafts the best available player by the house projection that keeps its
roster legal, and sets lineups by projection while benching players listed as Out.
"""
from __future__ import annotations

from gmbench.data.models import Player, Snapshot
from gmbench.rules import RosterRules, pick_problem
from gmbench.state import LeagueState


def house_value(p: Player) -> float:
    if p.projection is not None:
        return p.projection.fantasy_points
    return float(p.seasons[0].fantasy_points) if p.seasons else 0.0


def ranked_pool(snapshot: Snapshot, state: LeagueState) -> list[Player]:
    pool = [p for p in snapshot.players.values() if p.id not in state.owner]
    return sorted(pool, key=lambda p: (-house_value(p), p.id))


def bot_pick(state: LeagueState, snapshot: Snapshot, rules: RosterRules, team_id: str) -> int:
    team = state.teams[team_id]
    for p in ranked_pool(snapshot, state):
        if pick_problem(team.groups, p.group, rules) is None:
            return p.id
    raise RuntimeError(f"no legal pick available for {team_id}")


def queue_pick(state: LeagueState, snapshot: Snapshot, rules: RosterRules, team_id: str) -> int | None:
    """First legal, available player from the team's own queue (used only when a GM fails to pick)."""
    team = state.teams[team_id]
    for pid in team.queue:
        p = snapshot.players.get(pid)
        if p and pid not in state.owner and pick_problem(team.groups, p.group, rules) is None:
            return pid
    return None


def bot_lineup(state: LeagueState, snapshot: Snapshot, rules: RosterRules, team_id: str) -> tuple[list[int], list[int]]:
    team = state.teams[team_id]

    def key(pid: int) -> tuple[int, float, int]:
        p = snapshot.players[pid]
        out = 1 if (p.injury and p.injury.status.lower().startswith(("out", "injured"))) else 0
        return (out, -house_value(p), pid)

    active: list[int] = []
    for group, n in rules.minimums.items():
        members = sorted((pid for pid in team.roster if snapshot.players[pid].group == group), key=key)
        active.extend(members[:n])
    bench = [pid for pid in team.roster if pid not in active]
    return active, bench
