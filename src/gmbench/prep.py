"""War-room prep: before the draft, each GM researches the pool, writes its strategy notebook,
and sets a ranked queue. Parallel and sealed; nothing here changes rosters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from gmbench.agent.briefing import build_system
from gmbench.agent.loop import Budget, run_session
from gmbench.agent.tools import (
    GET_LEAGUE_STATE, GET_MY_TEAM, GET_NEWS, GET_PLAYER, GET_TEAM_SCHEDULE, NOTES_READ, NOTES_WRITE, SEARCH_PLAYERS,
    UPDATE_DRAFT_QUEUE, Action, ToolContext, ToolOutcome, _fn, dispatch as base_dispatch,
)
from gmbench.draft.run import DraftDeps
from gmbench.rules import RosterRules, snake_order
from gmbench.state import LeagueState, apply, replay

FINISH_PREP = _fn(
    "finish_prep",
    "End your war-room session. Your notebook and queue carry into the draft.",
    {"summary": {"type": "string", "description": "One or two sentences on your plan (private; max 300 characters)."}},
    ["summary"],
)

PREP_TASK = """\
## Your task now: war-room prep
The draft starts Monday. This is your one prep session: study the player pool with the research tools, decide your strategy, write what you'll need into your notebook (it's your only memory once the draft starts), and set a ranked draft queue of up to 60 players with update_draft_queue. Your draft slot and the snake order are in the league table (you pick at the position shown). When you're done, call finish_prep."""


def run_prep(deps: DraftDeps, *, teams: list[str] | None = None, max_tool_calls: int = 20, workers: int = 13) -> LeagueState:
    state = replay(deps.ledger.events())
    rules = RosterRules.from_config(deps.cfg.rules)
    slots = snake_order(state.order, int(deps.cfg.rules["rounds"]))
    todo = [t for t in state.order if not state.teams[t].is_bot and (not teams or t in teams)]
    tools = [SEARCH_PLAYERS, GET_PLAYER, GET_TEAM_SCHEDULE, GET_NEWS, GET_LEAGUE_STATE, GET_MY_TEAM, NOTES_READ, NOTES_WRITE,
             UPDATE_DRAFT_QUEUE, FINISH_PREP]
    budget = Budget(max_tool_calls=max_tool_calls, effort=deps.cfg.harness["effort"]["media_day"],
                    max_tokens=int(deps.cfg.harness["max_tokens"]))

    def dispatch(name: str, args: dict, ctx: ToolContext) -> ToolOutcome:
        if name == "finish_prep":
            return ToolOutcome("War room closed. See you at the draft.",
                               action=Action("finish_prep", {"summary": str(args.get("summary", ""))[:300]}), terminal=True)
        return base_dispatch(name, args, ctx)

    def session(team_id: str):
        ctx = ToolContext(team_id=team_id, state=state, snapshot=deps.snapshot, rules=rules, slots=slots,
                          notebook_path=deps.notebook(team_id), phase="prep")
        system = build_system(team_id, state, deps.cfg, today=deps.today, as_of=deps.as_of, task=PREP_TASK)
        mine = [s.pick_no for s in slots if s.team_id == team_id]
        user = (f"Your picks: {', '.join(f'#{n}' for n in mine)} of {len(slots)}. "
                "Research, write your notebook, set your queue, then call finish_prep.")
        queued: list[dict] = []

        def on_action(action: Action) -> None:
            queued.append({"name": action.name, "args": action.args})

        result = run_session(deps.client, deps.cfg.team(team_id), session_id=f"prep:{team_id}", system=system, user=user,
                             tools=tools, ctx=ctx, dispatch=dispatch, terminal_tools={"finish_prep"}, budget=budget,
                             transcript_dir=deps.root / "transcripts" / "prep", on_action=on_action,
                             nudge_text="Call finish_prep when your notebook and queue are ready.")
        return result, queued

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip(todo, pool.map(session, todo)))

    for team_id in todo:
        result, queued = results[team_id]
        for q in queued:
            if q["name"] == "update_draft_queue":
                apply(state, deps.append("QUEUE_UPDATED", f"gm:{team_id}", {"team": team_id, **q["args"]}, session=result.session_id))
            elif q["name"] == "notes_write":
                deps.append("NOTES_UPDATED", f"gm:{team_id}", {"team": team_id, **q["args"]}, session=result.session_id)
        deps.append("SESSION_COMPLETED", f"gm:{team_id}", {"kind": "prep", **result.summary()}, session=result.session_id,
                    refs={"transcript": result.transcript_path, "transcript_sha256": result.transcript_sha256})
        deps.log(f"{team_id:<9} {result.outcome:<10} calls={result.calls} tools={result.tool_calls} "
                 f"queue={len(state.teams[team_id].queue)} ${result.cost_usd:.3f} {result.wall_s}s")
    return state
