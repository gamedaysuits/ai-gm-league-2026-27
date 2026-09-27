"""Draft runner: one adaptive tool-using session per pick, resumable from the ledger.

There is no pick clock. A GM that can't be reached after retries pauses the
draft for the operator (or, in rehearsals, autopicks from its own queue).
Broadcast reactions run in a background pool so they never delay the next pick.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import date
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gmbench.agent.briefing import build_system, draft_briefing
from gmbench.agent.factcheck import FactChecker
from gmbench.agent.loop import Budget, ChatClient, SessionResult, run_session
from gmbench.agent.prompts import DRAFT_TASK, SAY_TASK
from gmbench.agent.tools import DRAFT_TOOLS, SAY_TOOLS, Action, ToolContext, dispatch
from gmbench.agent.untrusted import wrap
from gmbench.bot.baseline import bot_pick, queue_pick
from gmbench.config import LeagueConfig
from gmbench.data.models import Snapshot
from gmbench.draft.floor import Cue, FloorManager
from gmbench.export.site import write_draft_export
from gmbench.ledger import Ledger
from gmbench.rules import RosterRules, Slot, snake_order
from gmbench.state import LeagueState, apply, replay


class DraftPaused(Exception):
    def __init__(self, slot: Slot, result: SessionResult) -> None:
        super().__init__(f"draft paused at pick #{slot.pick_no} ({slot.team_id}): {result.outcome} {result.error or ''}")
        self.slot, self.result = slot, result


@dataclass
class DraftDeps:
    cfg: LeagueConfig
    ledger: Ledger
    snapshot: Snapshot
    client: ChatClient
    root: Path  # run directory: transcripts/, notebooks/, exports/
    today: str
    as_of: str
    log: Callable[[str], None] = print
    factcheck: Any = None  # FactChecker for on-air lines; created by run_draft for real models
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts" / "draft"

    def notebook(self, team_id: str) -> Path:
        return self.root / "private" / "notebooks" / f"{team_id}.md"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def append(self, type: str, actor: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        with self._lock:
            return self.ledger.append(type, actor, payload, **kw)


def run_draft(deps: DraftDeps, *, stop_after: int | None = None, on_failure: str = "pause",
              attempts: int = 3, reactions: bool = True, say_workers: int = 4) -> LeagueState:
    cfg = deps.cfg
    rules = RosterRules.from_config(cfg.rules)
    state = replay(deps.ledger.events())
    if not state.order:
        raise RuntimeError("draft order not set (DRAFT_ORDER_SET missing)")
    slots = snake_order(state.order, int(cfg.rules["rounds"]))
    floor = FloorManager(deps.snapshot, slots)
    on_air: deque[str] = deque(maxlen=6)
    pool = ThreadPoolExecutor(max_workers=say_workers)
    pending: list[Future] = []
    budget = Budget(max_tool_calls=int(cfg.harness["draft"]["max_tool_calls"]), effort=cfg.harness["effort"]["draft"],
                    max_tokens=int(cfg.harness["max_tokens"]))
    if deps.factcheck is None and type(deps.client).__name__ != "FakeClient":
        deps.factcheck = FactChecker(deps.snapshot, date.fromisoformat(deps.today))

    try:
        for slot in slots:
            if slot.pick_no <= len(state.picks):
                continue
            if stop_after is not None and slot.pick_no > stop_after:
                break
            team = state.teams[slot.team_id]
            queues_before = {t.id: list(t.queue) for t in state.teams.values()}

            if team.is_bot:
                pid = bot_pick(state, deps.snapshot, rules, team.id)
                payload = _pick_payload(slot, deps.snapshot, pid, {
                    "on_air_call": "",
                    "public_rationale": "Autodraft takes the best available player by the house projection.",
                    "projected_points": None, "range_80": None}, auto={"reason": "control_bot", "source": "baseline"})
                event = deps.append("DRAFT_PICK", f"bot:{team.id}", payload)
            else:
                event = _llm_pick(deps, state, slot, slots, rules, budget, attempts, on_failure)
            apply(state, event)
            pick = event["payload"]
            deps.log(f"#{slot.pick_no:>3} r{slot.round:<2} {slot.team_id:<9} {pick['player_name']} ({pick['nhl_team']} {pick['position']})"
                     + (f"  [auto: {pick['auto']['reason']}]" if pick.get("auto") else ""))
            with deps._lock:
                on_air.append(f"{state.team_label(slot.team_id)}: {pick.get('on_air_call') or pick.get('public_rationale') or ''}")
            write_draft_export(state, deps.snapshot, cfg, deps.exports)

            if reactions:
                cues = floor.after_pick(state, pick, queues_before)
                if slot.pick_no % len(state.order) == 0:
                    cues += floor.round_break(state, slot.round)
                for cue in cues:
                    pending.append(pool.submit(_say, deps, state, cue, list(on_air), on_air))
    finally:
        for fut in pending:
            try:
                fut.result()
            except Exception as exc:  # a failed broadcast line never stops the draft
                deps.log(f"say failed: {exc}")
        pool.shutdown(wait=True)
    final = replay(deps.ledger.events())
    write_draft_export(final, deps.snapshot, cfg, deps.exports)
    return final


def _llm_pick(deps: DraftDeps, state: LeagueState, slot: Slot, slots: list[Slot], rules: RosterRules,
              budget: Budget, attempts: int, on_failure: str) -> dict[str, Any]:
    spec = deps.cfg.team(slot.team_id)
    last: SessionResult | None = None
    for attempt in range(1, attempts + 1):
        ctx = ToolContext(team_id=slot.team_id, state=state, snapshot=deps.snapshot, rules=rules, slots=slots,
                          notebook_path=deps.notebook(slot.team_id), current_slot=slot,
                          extra={"factcheck": deps.factcheck})
        system = build_system(slot.team_id, state, deps.cfg, today=deps.today, as_of=deps.as_of, task=DRAFT_TASK)
        session_id = f"draft:p{slot.pick_no:03d}:{slot.team_id}:a{attempt}"

        def on_action(action: Action, _team: str = slot.team_id, _sid: str = session_id) -> None:
            if action.name == "update_draft_queue":
                apply(state, deps.append("QUEUE_UPDATED", f"gm:{_team}", {"team": _team, **action.args}, session=_sid))
            elif action.name == "notes_write":
                deps.append("NOTES_UPDATED", f"gm:{_team}", {"team": _team, **action.args}, session=_sid)

        result = run_session(deps.client, spec, session_id=session_id, system=system, user=draft_briefing(ctx),
                             tools=DRAFT_TOOLS, ctx=ctx, dispatch=dispatch, terminal_tools={"make_pick"},
                             budget=budget, transcript_dir=deps.transcripts, on_action=on_action)
        deps.append("SESSION_COMPLETED", f"gm:{slot.team_id}", {"kind": "draft_pick", **result.summary()},
                    session=session_id, refs={"transcript": result.transcript_path, "transcript_sha256": result.transcript_sha256})
        for v in result.violations:
            deps.append("VIOLATION", "league", {"team": slot.team_id, **v}, session=session_id)
        last = result
        if result.outcome == "ok" and result.action is not None:
            return deps.append("DRAFT_PICK", f"gm:{slot.team_id}",
                               _pick_payload(slot, deps.snapshot, int(result.action.args["player_id"]), result.action.args,
                                             session=session_id), session=session_id)
        deps.log(f"   pick #{slot.pick_no} {slot.team_id}: attempt {attempt} {result.outcome} {result.error or ''}")
        if result.outcome in ("out_of_credit", "bad_request"):  # permanent: retrying won't help
            break

    assert last is not None
    if on_failure == "pause":
        raise DraftPaused(slot, last)
    pid = queue_pick(state, deps.snapshot, rules, slot.team_id)
    source = "queue"
    if pid is None:
        pid, source = bot_pick(state, deps.snapshot, rules, slot.team_id), "baseline"
    return deps.append("DRAFT_PICK", f"league:{slot.team_id}", _pick_payload(
        slot, deps.snapshot, pid, {"public_rationale": "", "projected_points": None, "range_80": None},
        auto={"reason": last.outcome, "source": source}))


def _pick_payload(slot: Slot, snapshot: Snapshot, player_id: int, args: dict[str, Any], *,
                  auto: dict[str, Any] | None = None, session: str | None = None) -> dict[str, Any]:
    p = snapshot.players[player_id]
    return {
        "pick_no": slot.pick_no, "round": slot.round, "team": slot.team_id,
        "player_id": player_id, "player_name": p.name, "nhl_team": p.team, "position": p.position, "group": p.group,
        "on_air_call": args.get("on_air_call") or "",
        "public_rationale": args.get("public_rationale") or "",
        "joke_logic": args.get("joke_logic") or "",
        "fact_check": args.get("fact_check") or "off",
        "projected_points": args.get("projected_points"), "range_80": args.get("range_80"),
        "auto": auto, "session": session,
    }


def _say(deps: DraftDeps, state: LeagueState, cue: Cue, recent: list[str], on_air: deque[str]) -> None:
    spec = deps.cfg.team(cue.team_id)
    system = build_system(cue.team_id, state, deps.cfg, today=deps.today, as_of=deps.as_of, task=SAY_TASK)
    heard = "\n".join(wrap(line, "broadcast").replace("\n", " ") for line in recent[-4:])
    user = f"{cue.prompt}\n\nWhat was just said on air:\n{heard or '(nothing yet)'}\n\nCall say with your one line."
    ctx = ToolContext(team_id=cue.team_id, state=state, snapshot=deps.snapshot,
                      rules=RosterRules.from_config(deps.cfg.rules), slots=[], notebook_path=deps.notebook(cue.team_id),
                      phase="broadcast", extra={"factcheck": deps.factcheck})
    session_id = f"say:r{cue.round:02d}:{cue.kind}:{cue.pick_no or 0:03d}:{cue.team_id}"
    result = run_session(deps.client, spec, session_id=session_id, system=system, user=user, tools=SAY_TOOLS,
                         ctx=ctx, dispatch=dispatch, terminal_tools={"say"},
                         budget=Budget(max_tool_calls=0, max_nudges=1, effort=deps.cfg.harness["effort"]["say"], max_tokens=4000),
                         transcript_dir=deps.root / "transcripts" / "broadcast", nudge_text="Call say with your one line now.")
    deps.append("SESSION_COMPLETED", f"gm:{cue.team_id}", {"kind": "say", **result.summary()}, session=session_id,
                refs={"transcript": result.transcript_path, "transcript_sha256": result.transcript_sha256})
    if result.outcome != "ok" or result.action is None:
        return
    line = result.action.args["line"]
    event = deps.append("SAY", f"gm:{cue.team_id}", {"team": cue.team_id, "kind": cue.kind, "round": cue.round,
                                                     "pick_no": cue.pick_no, "cue": cue.prompt, "line": line,
                                                     "fact_check": result.action.args.get("fact_check") or "off",
                                                     "addressed_to": result.action.args.get("addressed_to") or cue.addressed_to},
                        session=session_id)
    with deps._lock:
        state.says.append({**event["payload"], "seq": event["seq"]})
        on_air.append(f"{state.team_label(cue.team_id)}: {line}")
    deps.log(f"      🎙  {cue.team_id}: {line}")
