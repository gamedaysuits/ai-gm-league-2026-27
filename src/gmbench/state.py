"""League state, rebuilt deterministically by replaying ledger events."""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from gmbench.config import call_name
from gmbench.ledger import canonical


@dataclass
class TeamState:
    id: str
    lab: str
    model: str | None
    roster: list[int] = field(default_factory=list)  # NHL player IDs, in acquisition order
    groups: Counter = field(default_factory=Counter)  # F/D/G counts
    queue: list[int] = field(default_factory=list)
    persona: dict[str, Any] | None = None
    lineup: dict[str, Any] | None = None
    lineup_history: list[dict[str, Any]] = field(default_factory=list)  # one entry per LINEUP_SET, in order

    @property
    def is_bot(self) -> bool:
        return self.model is None


@dataclass
class LeagueState:
    teams: dict[str, TeamState] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # round-1 draft order
    snapshot: dict[str, Any] | None = None
    picks: list[dict[str, Any]] = field(default_factory=list)
    owner: dict[int, str] = field(default_factory=dict)  # player_id -> team_id
    player_group: dict[int, str] = field(default_factory=dict)  # for rostered players
    says: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)  # game date -> SCORE_DAY payload (latest)
    trades: dict[str, dict[str, Any]] = field(default_factory=dict)  # trade_id -> record incl. status
    chat: list[dict[str, Any]] = field(default_factory=list)
    week: dict[str, Any] | None = None  # the open (or last) week
    transactions: list[dict[str, Any]] = field(default_factory=list)  # executed trades and waiver wins
    head_seq: int = 0

    def points(self) -> dict[str, int]:
        totals = {tid: 0 for tid in self.teams}
        for day in self.scores.values():
            for tid, pts in day["team_points"].items():
                totals[tid] = totals.get(tid, 0) + int(pts)
        return totals

    def team_label(self, team_id: str) -> str:
        return call_name(team_id)


def apply(state: LeagueState, event: dict[str, Any]) -> None:
    kind, p = event["type"], event["payload"]
    if kind == "LEAGUE_CREATED":
        state.teams = {t["id"]: TeamState(id=t["id"], lab=t["lab"], model=t.get("model")) for t in p["teams"]}
    elif kind == "SNAPSHOT_TAKEN":
        state.snapshot = p
    elif kind == "DRAFT_ORDER_SET":
        state.order = list(p["order"])
    elif kind == "PERSONA_CREATED":
        state.teams[p["team"]].persona = p["persona"]
    elif kind == "QUEUE_UPDATED":
        state.teams[p["team"]].queue = list(p["queue"])
    elif kind == "DRAFT_PICK":
        team = state.teams[p["team"]]
        pid = int(p["player_id"])
        team.roster.append(pid)
        team.groups[p["group"]] += 1
        state.owner[pid] = team.id
        state.player_group[pid] = p["group"]
        state.picks.append({**p, "seq": event.get("seq")})
    elif kind == "LINEUP_SET":
        entry = {"week": p.get("week"), "active": p["active"], "bench": p["bench"], "source": p.get("source")}
        state.teams[p["team"]].lineup = entry
        state.teams[p["team"]].lineup_history.append(entry)
    elif kind in ("SCORE_DAY", "SCORE_CORRECTION"):
        state.scores[p["date"]] = p
    elif kind == "WEEK_OPENED":
        state.week = dict(p, locked=False)
    elif kind == "WEEK_LOCKED" and state.week:
        state.week["locked"] = True
    elif kind == "CHAT_POSTED":
        state.chat.append({**p, "seq": event["seq"]})
    elif kind == "TRADE_PROPOSED":
        state.trades[p["trade_id"]] = {**p, "status": "open", "responses": []}
    elif kind == "TRADE_RESPONDED":
        t = state.trades[p["trade_id"]]
        t["responses"].append(p)
        t["status"] = {"accept": "accepted", "reject": "rejected", "counter": "countered"}[p["action"]]
    elif kind == "TRADE_EXECUTED":
        state.trades[p["trade_id"]]["status"] = "executed"
        _move(state, p["give"], p["proposer"], p["recipient"], p["groups"])
        _move(state, p["get"], p["recipient"], p["proposer"], p["groups"])
        state.transactions.append({"kind": "trade", **p, "seq": event["seq"]})
    elif kind == "TRADE_VOIDED":
        state.trades[p["trade_id"]]["status"] = "voided"
    elif kind == "WAIVERS_PROCESSED":
        for r in p["results"]:
            if r["status"] == "won":
                team = state.teams[r["team"]]
                team.roster.remove(r["drop"])
                team.groups[r["drop_group"]] -= 1
                state.owner.pop(r["drop"], None)
                team.roster.append(r["add"])
                team.groups[r["add_group"]] += 1
                state.owner[r["add"]] = team.id
                state.player_group[r["add"]] = r["add_group"]
                state.transactions.append({"kind": "waiver", **r, "week": p["week"], "seq": event["seq"]})
    elif kind == "SAY":
        state.says.append({**p, "seq": event["seq"]})
    elif kind == "VIOLATION":
        state.violations.append({**p, "seq": event["seq"]})
    elif kind == "REPORT_SUBMITTED":
        state.reports.append({**p, "seq": event["seq"]})
    state.head_seq = event["seq"]


def _move(state: LeagueState, players: list[int], src: str, dst: str, groups: dict[str, str]) -> None:
    for pid in players:
        g = groups[str(pid)]
        state.teams[src].roster.remove(pid)
        state.teams[src].groups[g] -= 1
        state.teams[dst].roster.append(pid)
        state.teams[dst].groups[g] += 1
        state.owner[pid] = dst


def replay(events: Iterable[dict[str, Any]]) -> LeagueState:
    state = LeagueState()
    for event in events:
        apply(state, event)
    return state


def state_sha256(state: LeagueState) -> str:
    view = {
        "teams": {
            t.id: {"roster": t.roster, "groups": dict(sorted(t.groups.items())), "queue": t.queue,
                   "persona": t.persona, "lineup": t.lineup}
            for t in sorted(state.teams.values(), key=lambda t: t.id)
        },
        "order": state.order,
        "snapshot": state.snapshot,
        "picks": state.picks,
        "head_seq": state.head_seq,
    }
    return hashlib.sha256(canonical(view).encode()).hexdigest()
