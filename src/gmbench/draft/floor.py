"""Floor manager: deterministic rules for who speaks on the Draft Night broadcast, besides the picks.

Every GM's own pick call already reacts to the pick right before it, so extra lines are rare:
a GM whose queued target just got sniped (when they aren't the next picker) groans about it,
and early rounds let a named rival chirp. At each round break, two GMs rotate through
table talk. The house projection only decides who is cued; it is never shown to the GMs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from gmbench.bot.baseline import house_value
from gmbench.data.models import Snapshot
from gmbench.rules import Slot
from gmbench.state import LeagueState

ROUND_QUESTIONS = (
    "Round {r} is in the books. Who had the best round, and who's in trouble?",
    "What was the most surprising pick of round {r}?",
    "Round {r} is done. Whose Stanley Cup pick already looks shaky?",
    "Whose roster after round {r} would you least like to face this season, and why?",
)


@dataclass(frozen=True)
class Cue:
    team_id: str
    kind: str  # reaction | table_talk
    pick_no: int | None
    round: int
    prompt: str
    addressed_to: str | None = None


class FloorManager:
    def __init__(self, snapshot: Snapshot, slots: list[Slot], *, max_reactors: int = 1,
                 max_reactions_per_round: int = 4, table_talk_per_round: int = 2, always_react_rounds: int = 0,
                 comeback_rounds: int = 3, comebacks_per_round: int = 5) -> None:
        ranked = sorted(snapshot.players.values(), key=lambda p: (-house_value(p), p.id))
        self.house_rank = {p.id: i for i, p in enumerate(ranked, 1)}
        self.snapshot = snapshot
        self.slots = slots
        self.max_reactors = max_reactors
        self.max_reactions_per_round = max_reactions_per_round
        self.table_talk_per_round = table_talk_per_round
        self.always_react_rounds = always_react_rounds  # early rounds: every pick gets at least one chirp
        self.comeback_rounds = comeback_rounds  # early rounds: whoever got ribbed always fires back
        self.comebacks_per_round = comebacks_per_round  # later rounds: this many comebacks per round
        self.round_comebacks: Counter = Counter()
        self.reacted: dict[int, set[str]] = defaultdict(set)
        self.round_reactions: Counter = Counter()
        self.talk_cursor = 0
        self.firsts: set[str] = set()

    def after_pick(self, state: LeagueState, pick: dict, queues_before: dict[str, list[int]]) -> list[Cue]:
        pid, pick_no, rnd, picker = int(pick["player_id"]), int(pick["pick_no"]), int(pick["round"]), pick["team"]
        auto = pick.get("auto") or {}
        if auto and auto.get("reason") != "control_bot":
            return []  # the league picked for an unreachable GM: nobody should react as if it chose
        p = self.snapshot.players[pid]
        last = p.seasons[0] if p.seasons else None
        who = "Autodraft, the league's control bot," if auto.get("reason") == "control_bot" else state.team_label(picker)
        if last is None:
            line = "no NHL games last season"
        elif p.group == "G":
            line = f"{last.wins or 0} wins and {last.shutouts or 0} shutouts in {last.gp} games in 2025-26, {last.fantasy_points} fantasy points"
        else:
            line = f"{last.goals} goals and {last.assists} assists in {last.gp} games in 2025-26, {last.fantasy_points} fantasy points"
        fact = f"{who} just took {p.name} ({p.team} {p.position}; {line}) with pick #{pick_no}."
        candidates: list[tuple[int, str, str]] = []
        nxt_slot = next((s for s in self.slots if s.pick_no == pick_no + 1), None)
        next_picker = nxt_slot.team_id if nxt_slot else None  # reacts in its own pick call instead
        for tid, queue in queues_before.items():
            if tid not in (picker, next_picker) and pid in queue[:3]:
                candidates.append((0, tid, fact + " He was near the top of your own draft queue."))
        rank = self.house_rank.get(pid, 9999)
        notable = rank + 12 < pick_no or rank > pick_no + 30
        if p.group in ("G", "D") and p.group not in self.firsts:
            self.firsts.add(p.group)
            notable = True
            fact += " He's the first " + ("goalie" if p.group == "G" else "defenceman") + " off the board."
        if notable:
            rivals = [t.id for t in state.teams.values() if t.id not in (picker, next_picker) and not t.is_bot
                      and picker in ((t.persona or {}).get("rivals") or [])]
            if rivals:
                candidates.append((1, rivals[0], fact + " That's your rival. React to the pick."))
        if rnd <= self.always_react_rounds and not candidates:
            rivals = [t.id for t in state.teams.values()
                      if t.id != picker and not t.is_bot and picker in ((t.persona or {}).get("rivals") or [])]
            nxt = [s.team_id for s in self.slots if s.pick_no > pick_no + 1 and s.team_id != picker
                   and not state.teams[s.team_id].is_bot]
            for tid in rivals + nxt:
                if tid not in self.reacted[rnd]:
                    candidates.append((2, tid, fact + " React to the pick."))
                    break
        cap = self.max_reactions_per_round if rnd > self.always_react_rounds else len(state.teams) + self.max_reactors
        cues: list[Cue] = []
        for _, tid, text in sorted(candidates):
            if len(cues) >= self.max_reactors or self.round_reactions[rnd] >= cap:
                break
            if tid == picker or state.teams[tid].is_bot or tid in self.reacted[rnd] or any(c.team_id == tid for c in cues):
                continue
            self.reacted[rnd].add(tid)
            self.round_reactions[rnd] += 1
            cues.append(Cue(team_id=tid, kind="reaction", pick_no=pick_no, round=rnd, prompt=text, addressed_to=picker))
        comeback = self.comeback(state, pick, next_picker)
        if comeback and all(c.team_id != comeback.team_id for c in cues):
            cues.append(comeback)
        return cues

    def comeback(self, state: LeagueState, pick: dict, next_picker: str | None) -> Cue | None:
        """The GM who just got talked about at the table fires back: the back-and-forth of a real draft party."""
        if not pick.get("on_air_call") or len(state.picks) < 2:
            return None
        prev, picker, rnd = state.picks[-2], pick["team"], int(pick["round"])
        target = prev["team"]
        prev_auto = (prev.get("auto") or {}).get("reason")
        if (target == picker or target == next_picker or state.teams[target].is_bot
                or (prev_auto and prev_auto != "control_bot")):
            return None
        if rnd > self.comeback_rounds and self.round_comebacks[rnd] >= self.comebacks_per_round:
            return None
        self.round_comebacks[rnd] += 1
        player = self.snapshot.players.get(int(prev["player_id"]))
        took = player.name if player else "your last pick"
        return Cue(team_id=target, kind="comeback", pick_no=int(pick["pick_no"]), round=rnd, addressed_to=picker,
                   prompt=(f"{state.team_label(picker)} just talked about your pick of {took} at the table (their exact "
                           "words are in what was just said). Fire back at them with one comeback line: specific to "
                           "what they said or who they just took."))

    def round_break(self, state: LeagueState, rnd: int) -> list[Cue]:
        speakers = [t for t in state.order if not state.teams[t].is_bot]
        if not speakers:
            return []
        cues = []
        for i in range(min(self.table_talk_per_round, len(speakers))):
            tid = speakers[(self.talk_cursor + i) % len(speakers)]
            question = ROUND_QUESTIONS[(rnd - 1 + i) % len(ROUND_QUESTIONS)].format(r=rnd)
            cues.append(Cue(team_id=tid, kind="table_talk", pick_no=None, round=rnd, prompt=f"The host asks you: {question}"))
        self.talk_cursor = (self.talk_cursor + self.table_talk_per_round) % len(speakers)
        return cues
