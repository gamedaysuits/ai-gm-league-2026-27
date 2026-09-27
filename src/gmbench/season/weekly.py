"""The weekly front office: three sealed rounds, then a pure resolution in canonical order.

R1 front office (everyone, parallel): lineup, ranked waiver claims, trade proposals, one chat
   message, one press quote.
R2 trade desk (only GMs with offers): accept / reject / counter each offer.
R3 final call (everyone): answer counters to your own proposals, set the final lineup.
Resolution never depends on who answered first. Trades execute in (proposer waiver priority,
proposal order); waivers resolve against post-trade rosters by priority.
"""
from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gmbench.agent.briefing import build_system
from gmbench.agent.loop import Budget, run_session
from gmbench.agent.tools import (
    GET_NEWS, GET_PLAYER, GET_TEAM_SCHEDULE, NOTES_READ, NOTES_WRITE, SEARCH_PLAYERS, Action, ToolContext,
    ToolOutcome, _fn, dispatch as base_dispatch, injury_label,
)
from gmbench.agent.untrusted import looks_like_injection, wrap
from gmbench.bot.baseline import bot_lineup
from gmbench.draft.run import DraftDeps
from gmbench.rules import RosterRules, lineup_problem
from gmbench.season.market import Claim, Trade, execute_trade, resolve_waivers, trade_problem
from gmbench.state import LeagueState, apply, replay

# ------------------------------------------------------------------ tools

GET_WEEK = _fn("get_week", "This week's standings, your roster with season-to-date stats and games this week, "
               "pending trades, waiver priority, and recent league chat.", {}, [])
SET_LINEUP = _fn("set_lineup", "Set this week's lineup: exactly 6 F, 4 D, 2 G active from your roster; the rest bench. "
                 "You can call it again to change it before the round ends.",
                 {"active": {"type": "array", "items": {"type": "integer"}},
                  "bench": {"type": "array", "items": {"type": "integer"}}}, ["active", "bench"])
SUBMIT_CLAIMS = _fn("submit_waiver_claims", "Submit ranked free-agent claims (up to 4, best first; you can win at most "
                    "2). Each claim adds an unowned player and drops one of yours. Replaces any earlier submission.",
                    {"claims": {"type": "array", "items": {"type": "object", "properties": {
                        "add": {"type": "integer", "description": "Free agent's player ID."},
                        "drop": {"type": "integer", "description": "Your player to release."}}, "required": ["add", "drop"]}}},
                    ["claims"])
PROPOSE_TRADE = _fn("propose_trade", "Offer a trade (at most 2 offers per week). Equal numbers of players, 1 to 3 "
                    "each way; both rosters must stay legal. Your message is shown to the other GM.",
                    {"to_team": {"type": "string"}, "give": {"type": "array", "items": {"type": "integer"}},
                     "get": {"type": "array", "items": {"type": "integer"}},
                     "message": {"type": "string", "description": "Your pitch, max 400 characters."}},
                    ["to_team", "give", "get", "message"])
RESPOND_TRADE = _fn("respond_trade", "Answer a trade offer made to you: accept, reject, or counter once with different "
                    "players (a counter goes back to them to accept or reject).",
                    {"trade_id": {"type": "string"}, "action": {"type": "string", "enum": ["accept", "reject", "counter"]},
                     "give": {"type": "array", "items": {"type": "integer"}, "description": "Counter only: players you'd send."},
                     "get": {"type": "array", "items": {"type": "integer"}, "description": "Counter only: players you want."},
                     "message": {"type": "string", "description": "Max 400 characters."}},
                    ["trade_id", "action", "message"])
POST_CHAT = _fn("post_chat", "Post one message to the public league chat this week (max 280 characters; PG-13, hockey only).",
                {"message": {"type": "string"}}, ["message"])
SUBMIT_PRESSER = _fn("submit_presser", "Your weekly press-conference quote for the Hot Stove show, in your own voice "
                     "(max 280 characters).", {"line": {"type": "string"}}, ["line"])
END_TURN = _fn("end_turn", "Finish this round. Anything you submitted stands.", {}, [])

R1_TOOLS = [GET_WEEK, SEARCH_PLAYERS, GET_PLAYER, GET_TEAM_SCHEDULE, GET_NEWS, NOTES_READ, NOTES_WRITE,
            SET_LINEUP, SUBMIT_CLAIMS, PROPOSE_TRADE, POST_CHAT, SUBMIT_PRESSER, END_TURN]
R2_TOOLS = [GET_WEEK, GET_PLAYER, SEARCH_PLAYERS, NOTES_READ, NOTES_WRITE, RESPOND_TRADE, END_TURN]
R3_TOOLS = [GET_WEEK, GET_PLAYER, NOTES_READ, NOTES_WRITE, RESPOND_TRADE, SET_LINEUP, END_TURN]

TASKS = {
    "r1": """## Your task now: this week's front office
Set your lineup for the week, submit any waiver claims, make up to 2 trade offers if you want (trading window permitting), and optionally post one chat message and your weekly press quote. Offers are delivered after every GM finishes; other GMs will answer next round. Call end_turn when done.""",
    "r2": """## Your task now: the trade desk
You have trade offers. For each one: accept, reject, or counter once. Counters go back to the other GM to accept or reject. Call end_turn when done.""",
    "r3": """## Your task now: final call
Answer any counter-offers made to your proposals (accept or reject), then confirm your lineup for the week (players you just traded away or dropped are gone). Call end_turn when done.""",
}


# ------------------------------------------------------------------ week context

@dataclass
class WeekInfo:
    week: str  # Monday (or season opener) YYYY-MM-DD
    week_end: str
    lock_at: str
    priority: list[str]
    trades_open: bool
    standings: list[tuple[str, int, int]]  # team, points, last 7 days
    stats: dict[int, dict[str, Any]]  # season-to-date per player
    games_this_week: dict[str, int]  # NHL club -> games


@dataclass
class Pending:
    lineup: dict[str, list[int]] | None = None
    claims: list[dict[str, int]] | None = None
    proposals: list[dict[str, Any]] = field(default_factory=list)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    chat: str | None = None
    presser: str | None = None


def season_stats(games_dir: Path, until: str) -> dict[int, dict[str, Any]]:
    """Season-to-date and last-14-day totals from the stored per-game lines (all players)."""
    stats: dict[int, dict[str, Any]] = defaultdict(lambda: {"gp": 0, "g": 0, "a": 0, "w": 0, "otl": 0, "so": 0, "fp": 0, "fp14": 0})
    horizon = (date.fromisoformat(until) - timedelta(days=13)).isoformat()
    for path in sorted(games_dir.glob("*.json")) if games_dir.exists() else []:
        day = path.stem
        if day > until:
            continue
        for ln in json.loads(path.read_text()):
            s = stats[int(ln["player_id"])]
            s["gp"] += 1
            s["g"] += ln["goals"]
            s["a"] += ln["assists"]
            s["w"] += ln["win"]
            s["otl"] += ln["otl"]
            s["so"] += ln["shutout"]
            s["fp"] += ln["fantasy_points"]
            if day >= horizon:
                s["fp14"] += ln["fantasy_points"]
    return dict(stats)


def std_label(pid: int, info: WeekInfo) -> str:
    s = info.stats.get(pid)
    if not s:
        return "26-27: no games yet"
    return f"26-27: {s['gp']}gp {s['g']}g {s['a']}a" + (f" {s['w']}w {s['so']}so" if s["w"] or s["so"] else "") + \
        f" → {s['fp']}fp (last 14d {s['fp14']})"


def week_digest(team_id: str, state: LeagueState, ctx: ToolContext, info: WeekInfo) -> str:
    snap = ctx.snapshot
    out = [f"Week of {info.week} to {info.week_end}. Lineups lock {info.lock_at}. "
           f"Trading is {'OPEN (deadline Mar 1)' if info.trades_open else 'CLOSED this week'}.", "", "Standings:"]
    for i, (tid, pts, last7) in enumerate(info.standings, 1):
        out.append(f"{i:>2}. {state.team_label(tid)}: {pts} pts (last 7 days {last7})")
    team = state.teams[team_id]
    out.append("")
    out.append(f"Your roster (current lineup marked *):")
    active = set((team.lineup or {}).get("active") or [])
    for pid in team.roster:
        p = snap.players.get(pid)
        if p:
            games = info.games_this_week.get(p.team, 0)
            out.append(f" {'*' if pid in active else ' '} {pid} | {p.name} | {p.team} {p.position} | {games} games this week | "
                       f"{std_label(pid, info)} | {injury_label(p)}")
    out.append(f"Waiver priority: you are #{info.priority.index(team_id) + 1} of {len(info.priority)}.")
    fa = [p for p in snap.players.values() if p.id not in state.owner]
    fa.sort(key=lambda p: (-(info.stats.get(p.id, {}).get("fp14", 0)), -(info.stats.get(p.id, {}).get("fp", 0)), p.id))
    out.append("")
    out.append("Top free agents by the last 14 days:")
    for p in fa[:25]:
        out.append(f"  {p.id} | {p.name} | {p.team} {p.position} | {info.games_this_week.get(p.team, 0)} games this week | "
                   f"{std_label(p.id, info)} | {injury_label(p)}")
    open_trades = [t for t in state.trades.values() if t["status"] in ("open", "countered")
                   and team_id in (t["proposer"], t["recipient"]) and t["week"] == info.week]
    if open_trades:
        out.append("")
        out.append("Trades involving you this week:")
        for t in open_trades:
            out.append(trade_line(t, state, snap))
    recent = state.chat[-10:]
    if recent:
        out.append("")
        out.append("Recent league chat:")
        for c in recent:
            out.append(f"{state.team_label(c['team'])}: " + wrap(c["message"], c["team"]).replace("\n", " "))
    return "\n".join(out)


def trade_line(t: dict[str, Any], state: LeagueState, snap) -> str:
    def names(ids: list[int]) -> str:
        return ", ".join(f"{snap.players[i].name} ({i})" if i in snap.players else str(i) for i in ids)
    msg = wrap(t.get("message", ""), t["proposer"]).replace("\n", " ")
    return (f"[{t['trade_id']}] {state.team_label(t['proposer'])} gives {names(t['give'])} for "
            f"{names(t['get'])} from {state.team_label(t['recipient'])} — status {t['status']}. Message: {msg}")


# ------------------------------------------------------------------ dispatch

def make_dispatch(info: WeekInfo, pending: Pending, rules: RosterRules, bot_ids: set[str], round_name: str):
    def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        st, tid = ctx.state, ctx.team_id
        team = st.teams[tid]
        if name == "get_week":
            return ToolOutcome(week_digest(tid, st, ctx, info))
        if name == "end_turn":
            return ToolOutcome("Round finished.", action=Action("end_turn", {}), terminal=True)
        if name == "set_lineup":
            roster = {p: st.player_group[p] for p in team.roster}
            active, bench = [int(x) for x in args["active"]], [int(x) for x in args["bench"]]
            problem = lineup_problem(roster, active, bench, rules)
            if problem:
                return ToolOutcome(f"ERROR: {problem}.", ok=False)
            pending.lineup = {"active": active, "bench": bench}
            return ToolOutcome("Lineup saved for this week.")
        if name == "submit_waiver_claims":
            claims = []
            for c in (args.get("claims") or [])[:4]:
                add, drop = int(c["add"]), int(c["drop"])
                if add not in ctx.snapshot.players:
                    return ToolOutcome(f"ERROR: {add} is not in the player pool.", ok=False)
                if add in st.owner:
                    return ToolOutcome(f"ERROR: {ctx.snapshot.players[add].name} is owned by {st.owner[add]}.", ok=False)
                if drop not in team.roster:
                    return ToolOutcome(f"ERROR: {drop} is not on your roster.", ok=False)
                claims.append({"add": add, "drop": drop})
            pending.claims = claims
            return ToolOutcome(f"{len(claims)} claim(s) saved; they resolve by waiver priority after every GM submits.")
        if name == "propose_trade":
            if not info.trades_open or round_name != "r1":
                return ToolOutcome("ERROR: trade offers are only accepted in the front-office round while trading is open.", ok=False)
            if len(pending.proposals) >= 2:
                return ToolOutcome("ERROR: at most 2 trade offers per week.", ok=False)
            to = str(args["to_team"])
            if to not in st.teams:
                return ToolOutcome(f"ERROR: unknown team {to!r}.", ok=False)
            trade = Trade("draft", tid, to, [int(x) for x in args["give"]], [int(x) for x in args["get"]], str(args.get("message", ""))[:400])
            rosters = {t: list(v.roster) for t, v in st.teams.items()}
            problem = trade_problem(trade, rosters, st.player_group, rules, bot_ids=bot_ids, max_per_side=3)
            if problem:
                return ToolOutcome(f"ERROR: {problem}.", ok=False)
            pending.proposals.append({"to": to, "give": trade.give, "get": trade.get, "message": trade.message})
            return ToolOutcome(f"Offer to {to} saved; it's delivered when the round closes.")
        if name == "respond_trade":
            trade_id = str(args["trade_id"])
            t = st.trades.get(trade_id)
            if t is None:
                return ToolOutcome(f"ERROR: no trade {trade_id}.", ok=False)
            action = args["action"]
            if round_name == "r2" and (t["recipient"] != tid or t["status"] != "open"):
                return ToolOutcome("ERROR: you can only answer open offers made to you.", ok=False)
            if round_name == "r3" and (t["recipient"] != tid or t["status"] != "open" or not t.get("counter_of")):
                return ToolOutcome("ERROR: in the final call you can only answer counter-offers made to you.", ok=False)
            if action == "counter":
                if round_name != "r2" or t.get("counter_of"):
                    return ToolOutcome("ERROR: you can counter an original offer once, in the trade-desk round.", ok=False)
                counter = Trade("draft", tid, t["proposer"], [int(x) for x in args.get("give") or []],
                                [int(x) for x in args.get("get") or []])
                rosters = {x: list(v.roster) for x, v in st.teams.items()}
                problem = trade_problem(counter, rosters, st.player_group, rules, bot_ids=bot_ids, max_per_side=3)
                if problem:
                    return ToolOutcome(f"ERROR: counter: {problem}.", ok=False)
            pending.responses[trade_id] = {"action": action, "give": [int(x) for x in args.get("give") or []],
                                           "get": [int(x) for x in args.get("get") or []],
                                           "message": str(args.get("message", ""))[:400]}
            return ToolOutcome(f"Response to {trade_id} saved: {action}.")
        if name == "post_chat":
            msg = " ".join(str(args["message"]).split())[:280]
            pending.chat = msg
            return ToolOutcome("Chat message queued.")
        if name == "submit_presser":
            pending.presser = " ".join(str(args["line"]).split())[:280]
            return ToolOutcome("Press quote saved.")
        return base_dispatch(name, args, ctx)
    return dispatch


# ------------------------------------------------------------------ orchestration

def week_bounds(week_start: date) -> tuple[date, date]:
    return week_start, week_start + timedelta(days=6 - week_start.weekday())


def lock_time(snapshot, week_start: date, week_end: date) -> str:
    """15 minutes before the week's first puck drop (UTC). Falls back to 16:00 UTC on the first game date."""
    dates = sorted({g for games in snapshot.team_games.values() for g in games if week_start.isoformat() <= g <= week_end.isoformat()})
    first = dates[0] if dates else week_start.isoformat()
    start = (getattr(snapshot, "first_puck_utc", None) or {}).get(first)
    dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else datetime.fromisoformat(first + "T16:00:00+00:00")
    return (dt - timedelta(minutes=15)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_week(deps: DraftDeps, *, week_start: date, games_dir: Path, workers: int = 13,
             now: datetime | None = None) -> LeagueState:
    cfg, snap = deps.cfg, deps.snapshot
    rules = RosterRules.from_config(cfg.rules)
    state = replay(deps.ledger.events())
    bot_ids = {t for t, v in state.teams.items() if v.is_bot}
    wk_start, wk_end = week_bounds(week_start)
    trades_cfg = cfg.rules["trades"]
    trades_open = trades_cfg["open"] <= wk_start.isoformat() <= trades_cfg["deadline"]
    totals = state.points()
    last_days = sorted(state.scores)[-7:]
    last7 = {t: sum(int(state.scores[d]["team_points"].get(t, 0)) for d in last_days) for t in state.teams}
    standings = sorted(state.teams, key=lambda t: (-totals.get(t, 0), t))
    order_rev = list(reversed(state.order))
    priority = sorted(state.teams, key=lambda t: (totals.get(t, 0), order_rev.index(t)))  # worst first
    games = {club: sum(1 for d in days if wk_start.isoformat() <= d <= wk_end.isoformat())
             for club, days in snap.team_games.items()}
    info = WeekInfo(week=wk_start.isoformat(), week_end=wk_end.isoformat(), lock_at=lock_time(snap, wk_start, wk_end),
                    priority=priority, trades_open=trades_open,
                    standings=[(t, totals.get(t, 0), last7.get(t, 0)) for t in standings],
                    stats=season_stats(games_dir, (wk_start - timedelta(days=1)).isoformat()),
                    games_this_week=games)
    apply(state, deps.append("WEEK_OPENED", "league", {"week": info.week, "week_end": info.week_end, "lock_at": info.lock_at,
                                                       "priority": priority, "trades_open": trades_open}))
    humans = [t for t in state.order if t not in bot_ids]
    if (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(info.lock_at.replace("Z", "+00:00")):
        deps.log(f"week {info.week}: past the lock ({info.lock_at}); carrying lineups over, no GM sessions")
        humans = []

    def round_(name: str, teams: list[str], tools: list[dict], pendings: dict[str, Pending]) -> None:
        budget = Budget(max_tool_calls=16 if name == "r1" else 8, effort=cfg.harness["effort"]["weekly"],
                        max_tokens=int(cfg.harness["max_tokens"]))

        def session(team_id: str):
            ctx = ToolContext(team_id=team_id, state=state, snapshot=snap, rules=rules, slots=[],
                              notebook_path=deps.notebook(team_id), phase=f"week:{name}")
            system = build_system(team_id, state, cfg, today=deps.today, as_of=deps.as_of, task=TASKS[name])
            return run_session(deps.client, cfg.team(team_id), session_id=f"week:{info.week}:{name}:{team_id}", system=system,
                               user=week_digest(team_id, state, ctx, info) + "\n\nAct with your tools, then call end_turn.",
                               tools=tools, ctx=ctx, dispatch=make_dispatch(info, pendings[team_id], rules, bot_ids, name),
                               terminal_tools={"end_turn"}, budget=budget,
                               transcript_dir=deps.root / "transcripts" / "weekly" / info.week,
                               nudge_text="Call end_turn when you're done.")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = dict(zip(teams, pool.map(session, teams)))
        for team_id in teams:  # sealed: nothing is applied until every session has finished
            r = results[team_id]
            deps.append("SESSION_COMPLETED", f"gm:{team_id}", {"kind": f"week_{name}", "week": info.week, **r.summary()},
                        session=r.session_id, refs={"transcript": r.transcript_path, "transcript_sha256": r.transcript_sha256})

    # R1: front office
    pend = {t: Pending() for t in humans}
    round_("r1", humans, R1_TOOLS, pend)
    counter = 0
    for team_id in humans:  # canonical order: draft order
        p = pend[team_id]
        if p.chat:
            if looks_like_injection(p.chat):
                deps.append("VIOLATION", "league", {"team": team_id, "kind": "suspected_injection", "field": "chat", "text": p.chat})
            apply(state, deps.append("CHAT_POSTED", f"gm:{team_id}", {"team": team_id, "week": info.week, "message": p.chat}))
        if p.presser:
            apply(state, deps.append("REPORT_SUBMITTED", f"gm:{team_id}", {"team": team_id, "kind": "presser", "week": info.week, "line": p.presser}))
        if p.claims is not None:
            deps.append("WAIVER_CLAIMS_SUBMITTED", f"gm:{team_id}", {"team": team_id, "week": info.week, "claims": p.claims})
        for prop in p.proposals:
            counter += 1
            apply(state, deps.append("TRADE_PROPOSED", f"gm:{team_id}", {
                "trade_id": f"{info.week}-T{counter:02d}", "week": info.week, "proposer": team_id, "recipient": prop["to"],
                "give": prop["give"], "get": prop["get"], "message": prop["message"], "counter_of": None}))

    # R2: trade desk
    with_offers = [t for t in humans if any(tr["recipient"] == t and tr["status"] == "open" and tr["week"] == info.week
                                            for tr in state.trades.values())]
    pend2 = {t: Pending() for t in with_offers}
    if with_offers:
        round_("r2", with_offers, R2_TOOLS, pend2)
    for team_id in with_offers:
        for trade_id, resp in sorted(pend2[team_id].responses.items()):
            apply(state, deps.append("TRADE_RESPONDED", f"gm:{team_id}", {"trade_id": trade_id, "team": team_id, **resp}))
            if resp["action"] == "counter":
                orig = state.trades[trade_id]
                counter += 1
                apply(state, deps.append("TRADE_PROPOSED", f"gm:{team_id}", {
                    "trade_id": f"{info.week}-T{counter:02d}", "week": info.week, "proposer": team_id, "recipient": orig["proposer"],
                    "give": resp["give"], "get": resp["get"], "message": resp["message"], "counter_of": trade_id}))
    _execute_accepted(deps, state, info, rules, bot_ids)

    # Waivers against post-trade rosters.
    submitted = {e["payload"]["team"]: e["payload"]["claims"] for e in deps.ledger.events({"WAIVER_CLAIMS_SUBMITTED"})
                 if e["payload"]["week"] == info.week}
    claims = {t: [Claim(t, c["add"], c["drop"], i + 1) for i, c in enumerate(cs)] for t, cs in submitted.items()}
    rosters = {t: list(v.roster) for t, v in state.teams.items()}
    group_of = {**state.player_group, **{p.id: p.group for p in snap.players.values()}}
    results, _ = resolve_waivers(rosters, group_of, set(state.owner), claims, priority, rules,
                                 max_adds=int(cfg.rules["waivers"]["max_adds_per_week"]))
    if claims:
        apply(state, deps.append("WAIVERS_PROCESSED", "league", {"week": info.week, "results": [
            {"team": r.team, "add": r.add, "drop": r.drop, "rank": r.rank, "status": r.status,
             "add_group": group_of[r.add], "drop_group": group_of[r.drop]} for r in results]}))

    # R3: final call
    pend3 = {t: Pending(lineup=None) for t in humans}
    round_("r3", humans, R3_TOOLS, pend3)
    for team_id in humans:
        for trade_id, resp in sorted(pend3[team_id].responses.items()):
            if resp["action"] in ("accept", "reject"):
                apply(state, deps.append("TRADE_RESPONDED", f"gm:{team_id}", {"trade_id": trade_id, "team": team_id, **resp}))
    _execute_accepted(deps, state, info, rules, bot_ids)

    # Lock: final lineups (the GM's last valid lineup, repaired if players left; else carried over / bot rule).
    for team_id in state.order:
        team = state.teams[team_id]
        roster = {p: state.player_group[p] for p in team.roster}
        chosen = (pend3.get(team_id) and pend3[team_id].lineup) or (pend.get(team_id) and pend[team_id].lineup)
        source = "gm"
        if chosen is None or lineup_problem(roster, chosen["active"], chosen["bench"], rules):
            prev = team.lineup
            if prev and not lineup_problem(roster, prev["active"], prev["bench"], rules) and team_id not in bot_ids:
                chosen, source = {"active": prev["active"], "bench": prev["bench"]}, "carryover"
            else:
                active, bench = bot_lineup(state, snap, rules, team_id)
                chosen, source = {"active": active, "bench": bench}, ("bot" if team_id in bot_ids else "auto_repair")
        apply(state, deps.append("LINEUP_SET", f"{'gm' if source == 'gm' else 'league'}:{team_id}",
                                 {"team": team_id, "week": info.week, **chosen, "source": source}))
    apply(state, deps.append("WEEK_LOCKED", "league", {"week": info.week}))
    return state


def _execute_accepted(deps: DraftDeps, state: LeagueState, info: WeekInfo, rules: RosterRules, bot_ids: set[str]) -> None:
    accepted = [t for t in state.trades.values() if t["status"] == "accepted" and t["week"] == info.week]
    accepted.sort(key=lambda t: (info.priority.index(t["proposer"]), t["trade_id"]))
    for t in accepted:
        rosters = {x: list(v.roster) for x, v in state.teams.items()}
        trade = Trade(t["trade_id"], t["proposer"], t["recipient"], t["give"], t["get"])
        problem = trade_problem(trade, rosters, state.player_group, rules, bot_ids=bot_ids, max_per_side=3)
        if problem:
            apply(state, deps.append("TRADE_VOIDED", "league", {"trade_id": t["trade_id"], "reason": problem}))
            continue
        groups = {str(p): state.player_group[p] for p in t["give"] + t["get"]}
        apply(state, deps.append("TRADE_EXECUTED", "league", {"trade_id": t["trade_id"], "proposer": t["proposer"],
                                                             "recipient": t["recipient"], "give": t["give"], "get": t["get"],
                                                             "groups": groups, "week": info.week}))
