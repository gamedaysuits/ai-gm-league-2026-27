"""Post-draft session: Week 1 lineup, draft grades for every rival, and a predicted final table.

Runs in parallel and sealed after the last pick. The control bot's lineup is set by
its own rule. Grades and predictions become the Draft Night closer and a season-long
calibration check.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from gmbench.agent.briefing import build_system
from gmbench.agent.loop import Budget, run_session
from gmbench.agent.factcheck import FactChecker, league_names
from gmbench.agent.tools import (
    GET_LEAGUE_STATE, GET_MY_TEAM, GET_PLAYER, GET_TEAM_SCHEDULE, NOTES_READ, NOTES_WRITE, SEARCH_PLAYERS,
    Action, ToolContext, ToolOutcome, _fn, dispatch as base_dispatch, fact_check, fact_error, player_row,
)
from gmbench.agent.prompts import spoken_length, tag_problem
from gmbench.bot.baseline import bot_lineup
from gmbench.draft.run import DraftDeps
from gmbench.rules import RosterRules, lineup_problem
from gmbench.state import LeagueState, apply, replay

GRADES = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D", "F"]

SUBMIT_POST_DRAFT = _fn(
    "submit_post_draft",
    "Submit your Week 1 lineup, a grade for every other team's draft, your predicted final standings, and your "
    "own season projection. Grade comments may be read on air in your voice.",
    {
        "active": {"type": "array", "items": {"type": "integer"}, "description": "12 player IDs: exactly 6 F, 4 D, 2 G from your roster."},
        "bench": {"type": "array", "items": {"type": "integer"}, "description": "Your other 2 player IDs."},
        "grades": {"type": "array", "description": "One entry for every other team (13).", "items": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team id."},
                "grade": {"type": "string", "enum": GRADES},
                "comment": {"type": "string", "description": "One spoken sentence, max 160 characters."},
            },
            "required": ["team", "grade", "comment"]}},
        "predicted_standings": {"type": "array", "items": {"type": "string"},
                                "description": "All 14 team ids (including yours and autodraft), predicted finish 1st to 14th."},
        "projected_points": {"type": "integer", "description": "Your team's projected final fantasy points."},
        "range_low": {"type": "integer", "description": "Low end of your 80% range."},
        "range_high": {"type": "integer", "description": "High end of your 80% range."},
    },
    ["active", "bench", "grades", "predicted_standings", "projected_points", "range_low", "range_high"],
)

POST_DRAFT_TASK = """\
## Your task now: after the draft
The draft is over. Before Week 1 locks (Tuesday Sep 29, 15 minutes before the first puck drop):
1. Set your Week 1 lineup: exactly 6 F, 4 D and 2 G active from your roster; the other 2 on the bench. Only active players score. Check injuries and how many games each club plays this week.
2. Grade every other team's draft (A+ to F) with one spoken sentence each, like you're grading your buddies' drafts at the end of the party. These get read on air, so the on-air rules apply: funny, a set-up then the joke (roasts welcome, PG-13, hockey only), at most one number per comment, delivery tags allowed.
3. Predict the final standings of all 14 teams, and project your own final points with an 80% range.
Research as needed, then call submit_post_draft once."""


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def week_window(start: date) -> tuple[date, date]:
    monday = start - timedelta(days=start.weekday())
    return start, monday + timedelta(days=6)


def rosters_digest(state: LeagueState, ctx: ToolContext) -> str:
    lines = ["All rosters after the draft:"]
    for tid in state.order:
        t = state.teams[tid]
        names = []
        for pid in t.roster:
            p = ctx.snapshot.players.get(pid)
            if p:
                fp = p.seasons[0].fantasy_points if p.seasons else 0
                names.append(f"{p.name} {p.position} {p.team} ({fp}fp)")
        lines.append(f"- {state.team_label(tid)}: " + "; ".join(names))
    return "\n".join(lines)


def my_week(state: LeagueState, ctx: ToolContext, start: date) -> str:
    lo, hi = week_window(start)
    lines = [f"Your roster for Week 1 ({lo.isoformat()} to {hi.isoformat()}), with games this week:"]
    for pid in state.teams[ctx.team_id].roster:
        p = ctx.snapshot.players[pid]
        games = sum(1 for g in ctx.snapshot.team_games.get(p.team, []) if lo.isoformat() <= g <= hi.isoformat())
        lines.append(f"  [{games} games] " + player_row(p, state))
    return "\n".join(lines)


def _make_dispatch(rules: RosterRules, team_ids: list[str]):
    def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        if name != "submit_post_draft":
            return base_dispatch(name, args, ctx)
        team = ctx.state.teams[ctx.team_id]
        roster = {pid: ctx.state.player_group[pid] for pid in team.roster}
        active = [int(x) for x in args.get("active") or []]
        bench = [int(x) for x in args.get("bench") or []]
        problem = lineup_problem(roster, active, bench, rules)
        if problem:
            return ToolOutcome(f"ERROR: lineup: {problem}.", ok=False)
        others = [t for t in team_ids if t != ctx.team_id]
        grades = {g.get("team"): g for g in args.get("grades") or [] if isinstance(g, dict)}
        missing = [t for t in others if t not in grades]
        if missing:
            return ToolOutcome(f"ERROR: grades missing for {', '.join(missing)}.", ok=False)
        bad = [t for t, g in grades.items() if g.get("grade") not in GRADES or spoken_length(str(g.get("comment", ""))) > 160
               or tag_problem(str(g.get("comment", "")))]
        if bad:
            return ToolOutcome(f"ERROR: grade must be one of {GRADES} with a comment of at most 160 characters: {bad}.", ok=False)
        standings = list(args.get("predicted_standings") or [])
        if sorted(standings) != sorted(team_ids):
            return ToolOutcome(f"ERROR: predicted_standings must list all 14 team ids exactly once: {team_ids}.", ok=False)
        proj, lo, hi = int(args["projected_points"]), int(args["range_low"]), int(args["range_high"])
        if not (0 <= lo <= proj <= hi <= 5000):
            return ToolOutcome("ERROR: need 0 <= range_low <= projected_points <= range_high.", ok=False)
        spoken = "\n".join(f"About {t}'s draft: {grades[t]['comment']}" for t in others)
        status, problems = fact_check(ctx, spoken, [])  # the checker resolves the players the comments name
        if problems:
            return ToolOutcome(fact_error(ctx, problems, spoken, [], "grade comments", "submit_post_draft"), ok=False)
        payload = {"active": active, "bench": bench,
                   "grades": [{"team": t, "grade": grades[t]["grade"], "comment": " ".join(str(grades[t]["comment"]).split())} for t in others],
                   "predicted_standings": standings, "projected_points": proj, "range_80": [lo, hi],
                   "fact_check": status}
        return ToolOutcome("Submitted. Good luck in Week 1.", action=Action("submit_post_draft", payload), terminal=True)
    return dispatch


def run_post_draft(deps: DraftDeps, *, week_start: date, teams: list[str] | None = None, workers: int = 13) -> LeagueState:
    state = replay(deps.ledger.events())
    rules = RosterRules.from_config(deps.cfg.rules)
    team_ids = list(state.order)
    done = {r["team"] for r in state.reports if r.get("kind") == "post_draft"}
    todo = [t for t in team_ids if not state.teams[t].is_bot and t not in done and (not teams or t in teams)]
    tools = [GET_MY_TEAM, GET_LEAGUE_STATE, GET_PLAYER, SEARCH_PLAYERS, GET_TEAM_SCHEDULE, NOTES_READ, NOTES_WRITE,
             SUBMIT_POST_DRAFT]
    budget = Budget(max_tool_calls=10, effort=deps.cfg.harness["effort"]["weekly"], max_tokens=int(deps.cfg.harness["max_tokens"]))
    checker = (None if type(deps.client).__name__ == "FakeClient"
               else FactChecker(deps.snapshot, week_start, league_names=league_names(state)))

    def session(team_id: str):
        ctx = ToolContext(team_id=team_id, state=state, snapshot=deps.snapshot, rules=rules, slots=[],
                          notebook_path=deps.notebook(team_id), phase="post_draft", extra={"factcheck": checker})
        system = build_system(team_id, state, deps.cfg, today=deps.today, as_of=deps.as_of, task=POST_DRAFT_TASK)
        user = my_week(state, ctx, week_start) + "\n\n" + rosters_digest(state, ctx) + "\n\nCall submit_post_draft when ready."
        return run_session(deps.client, deps.cfg.team(team_id), session_id=f"postdraft:{team_id}", system=system, user=user,
                           tools=tools, ctx=ctx, dispatch=_make_dispatch(rules, team_ids), terminal_tools={"submit_post_draft"},
                           budget=budget, transcript_dir=deps.root / "transcripts" / "postdraft",
                           nudge_text="Call submit_post_draft now.")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip(todo, pool.map(session, todo)))

    for team_id in todo:
        r = results[team_id]
        deps.append("SESSION_COMPLETED", f"gm:{team_id}", {"kind": "post_draft", **r.summary()}, session=r.session_id,
                    refs={"transcript": r.transcript_path, "transcript_sha256": r.transcript_sha256})
        if r.outcome == "ok" and r.action:
            a = r.action.args
            apply(state, deps.append("LINEUP_SET", f"gm:{team_id}", {"team": team_id, "week": week_start.isoformat(),
                                                                     "active": a["active"], "bench": a["bench"], "source": "gm"},
                                     session=r.session_id))
            apply(state, deps.append("REPORT_SUBMITTED", f"gm:{team_id}", {"team": team_id, "kind": "post_draft",
                                                                           **{k: a[k] for k in ("grades", "predicted_standings",
                                                                                                "projected_points", "range_80",
                                                                                                "fact_check")}},
                                     session=r.session_id))
            deps.log(f"{team_id:<9} lineup set; predicts itself {ordinal(a['predicted_standings'].index(team_id) + 1)}, {a['projected_points']} pts")
        else:
            deps.log(f"{team_id:<9} ✗ {r.outcome} {r.error or ''} — falling back to auto lineup")

    for team_id in team_ids:  # bot, and anyone without a lineup, gets the rule-based lineup
        if state.teams[team_id].lineup is None:
            active, bench = bot_lineup(state, deps.snapshot, rules, team_id)
            source = "bot" if state.teams[team_id].is_bot else "auto_repair"
            apply(state, deps.append("LINEUP_SET", f"league:{team_id}", {"team": team_id, "week": week_start.isoformat(),
                                                                          "active": active, "bench": bench, "source": source}))
    return state
