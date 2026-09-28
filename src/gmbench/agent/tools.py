"""The GM tool suite: identical research and action tools for every competitor.

Research tools read only the frozen snapshot plus current league state, so every
GM sees the same information. Action tools validate their arguments and return
an `Action` for the caller to write to the ledger; nothing here writes directly.
House projections are never exposed.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from gmbench.agent.prompts import (ON_AIR_CALL_MAX, backstory_problem, catchphrase_problem, construction_problem,
                                   delivery_problem, repeat_problem, slop_problem, spoken_length, tag_problem)
from gmbench.data.models import Player, Snapshot
from gmbench.rules import RosterRules, Slot, pick_problem, unmet_minimums
from gmbench.state import LeagueState

MAX_RESULT_CHARS = 8000
NOTEBOOK_MAX_CHARS = 6000


@dataclass
class Action:
    name: str
    args: dict[str, Any]


@dataclass
class ToolOutcome:
    text: str
    ok: bool = True
    action: Action | None = None
    terminal: bool = False


@dataclass
class ToolContext:
    team_id: str
    state: LeagueState
    snapshot: Snapshot
    rules: RosterRules
    slots: list[Slot]
    notebook_path: Path
    current_slot: Slot | None = None
    phase: str = "draft"
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- schemas

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        },
    }


SEARCH_PLAYERS = _fn(
    "search_players",
    "Search the player pool. Returns compact stat lines (last 3 NHL regular seasons, injury status, "
    "and who owns the player). Fantasy points (fp) use this league's scoring.",
    {
        "query": {"type": "string", "description": "Part of a player's name (optional)."},
        "group": {"type": "string", "enum": ["F", "D", "G"], "description": "Position group (optional)."},
        "position": {"type": "string", "enum": ["C", "L", "R", "D", "G"], "description": "Exact position (optional)."},
        "team": {"type": "string", "description": "NHL club abbreviation, e.g. EDM (optional)."},
        "available_only": {"type": "boolean", "description": "Only undrafted/unowned players (default true)."},
        "sort_by": {"type": "string", "enum": ["last_season_fp", "fp_per_game_3yr", "age_asc", "age_desc"],
                    "description": "Sort order (default last_season_fp)."},
        "limit": {"type": "integer", "description": "1-50 rows (default 25)."},
    },
    [],
)
GET_PLAYER = _fn(
    "get_player",
    "Full profile for one player: bio, each of the last 3 NHL seasons, injury status and note, "
    "recent headlines, and current owner.",
    {"player_id": {"type": "integer", "description": "NHL player ID."}},
    ["player_id"],
)
GET_TEAM_SCHEDULE = _fn(
    "get_team_schedule",
    "An NHL club's 2026-27 regular-season schedule density: total games, games per week for the first "
    "weeks, and back-to-backs.",
    {"team": {"type": "string", "description": "NHL club abbreviation, e.g. TOR."}},
    ["team"],
)
GET_NEWS = _fn(
    "get_news",
    "Recent NHL headlines from the snapshot, optionally filtered by text, club, or player.",
    {
        "query": {"type": "string", "description": "Text to match in headlines (optional)."},
        "team": {"type": "string", "description": "NHL club abbreviation (optional)."},
        "player_id": {"type": "integer", "description": "NHL player ID (optional)."},
        "limit": {"type": "integer", "description": "1-20 (default 10)."},
    },
    [],
)
GET_LEAGUE_STATE = _fn(
    "get_league_state",
    "The draft board so far, every team's roster needs, and the upcoming pick order.",
    {},
    [],
)
GET_MY_TEAM = _fn(
    "get_my_team",
    "Your roster, positional needs, remaining picks, draft queue, and notebook size.",
    {},
    [],
)
NOTES_READ = _fn("notes_read", "Read your private GM notebook (persists all season).", {}, [])
NOTES_WRITE = _fn(
    "notes_write",
    f"Write to your private GM notebook (max {NOTEBOOK_MAX_CHARS} characters total). Use it to keep your "
    "strategy, targets, and lessons between sessions.",
    {
        "text": {"type": "string", "description": "Text to write."},
        "mode": {"type": "string", "enum": ["replace", "append"], "description": "Default append."},
    },
    ["text"],
)
UPDATE_DRAFT_QUEUE = _fn(
    "update_draft_queue",
    "Set your ranked draft queue (up to 60 player IDs, best first). If you ever fail to pick, the "
    "league picks the first available player from this queue for you.",
    {"player_ids": {"type": "array", "items": {"type": "integer"}, "description": "Ranked NHL player IDs."}},
    ["player_ids"],
)
MAKE_PICK = _fn(
    "make_pick",
    "Draft a player. This ends your turn. `on_air_call` is what you say out loud at the draft party: react to the "
    f"pick right before yours, then announce yours (max {ON_AIR_CALL_MAX} spoken characters, delivery tags allowed). "
    "`public_rationale` is your sober reasoning for the record (max 280 characters, not read aloud).",
    {
        "player_id": {"type": "integer", "description": "NHL player ID of an available player."},
        "on_air_call": {"type": "string", "description": "Your turn at the table, 2-4 short sentences: react to the "
                        "previous pick, then name your guy (full name once) and why; delivery tags allowed."},
        "public_rationale": {"type": "string", "description": "Your reasoning for the record, max 280 characters."},
        "joke_logic": {"type": "string", "description": "In one sentence, why your on_air_call is funny: what the "
                       "comparison or jab says about the pick. Not read aloud. Max 200 characters."},
        "projected_points": {"type": "integer", "description": "Your projection of his 2026-27 fantasy points for your team."},
        "range_low": {"type": "integer", "description": "Low end of your 80% range for his fantasy points."},
        "range_high": {"type": "integer", "description": "High end of your 80% range for his fantasy points."},
    },
    ["player_id", "on_air_call", "public_rationale", "joke_logic", "projected_points", "range_low", "range_high"],
)

SAY = _fn(
    "say",
    "Say one line on the broadcast, in your own voice: max 25 words / 200 spoken characters, delivery tags "
    "allowed, no markdown, emojis or hashtags.",
    {
        "line": {"type": "string", "description": "What you say on air."},
        "addressed_to": {"type": "string", "description": "Team id you're talking to, if any (optional)."},
    },
    ["line"],
)

DRAFT_TOOLS = [SEARCH_PLAYERS, GET_PLAYER, GET_TEAM_SCHEDULE, GET_NEWS, GET_LEAGUE_STATE, GET_MY_TEAM,
               NOTES_READ, NOTES_WRITE, UPDATE_DRAFT_QUEUE, MAKE_PICK]
SAY_TOOLS = [SAY]


# ---------------------------------------------------------------- formatting

def fp_per_game(p: Player) -> float:
    gp = sum(s.gp for s in p.seasons)
    if p.group == "G":
        gs = sum(s.gs or 0 for s in p.seasons)
        return sum(s.fantasy_points for s in p.seasons) / gs if gs else 0.0
    return sum(s.fantasy_points for s in p.seasons) / gp if gp else 0.0


def _mmss(seconds: float | None) -> str:
    if not seconds:
        return "-"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _season_label(season: str) -> str:
    return f"{season[2:4]}-{season[6:8]}"


def injury_label(p: Player) -> str:
    status = f"injury: {p.injury.status}" if p.injury else "healthy"
    if not p.on_roster:
        status += ", not on the club's current NHL roster list (IR, minors or unsigned)"
    return status


def player_row(p: Player, state: LeagueState) -> str:
    last = p.seasons[0] if p.seasons else None
    owner = state.owner.get(p.id)
    own = f"owned by {owner}" if owner else "available"
    age = f"{int(p.age)}y" if p.age is not None else "?y"  # floor: a 26.96-year-old is 26, not 27
    if last is None:
        line = "no NHL games in last 3 seasons"
    elif p.group == "G":
        line = (f"{_season_label(last.season)}: {last.gp}gp {last.gs or 0}gs {last.wins or 0}w {last.losses or 0}l "
                f"{last.ot_losses or 0}otl {last.shutouts or 0}so sv{(last.save_pct or 0):.3f} → {last.fantasy_points}fp")
    else:
        line = (f"{_season_label(last.season)}: {last.gp}gp {last.goals}g {last.assists}a → {last.fantasy_points}fp, "
                f"toi {_mmss(last.toi_per_game_s)} pp {_mmss(last.pp_toi_per_game_s)}")
    per = "fp/gs" if p.group == "G" else "fp/gp"
    return (f"{p.id} | {p.name} | {p.team} {p.position} {age}{moved_label(p)} | {line} | 3yr {fp_per_game(p):.2f} {per} | "
            f"{injury_label(p)} | {own}")


def moved_label(p: Player) -> str:
    """Recent team changes, so no GM chirps a player for a team he no longer plays for."""
    if not p.seasons or not p.seasons[0].teams:
        return ""
    last = p.seasons[0]
    teams = [t.strip() for t in last.teams.split(",") if t.strip()]
    label = _season_label(last.season)
    if len(teams) > 1:
        return f" (traded {'→'.join(teams)} in {label})"
    if teams and teams[-1] != p.team:
        return f" (new: was {teams[-1]} in {label})"
    return ""


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n…(truncated; narrow your search)"


# ---------------------------------------------------------------- dispatch

def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    handler = HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(f"ERROR: unknown tool {name!r}", ok=False)
    try:
        return handler(args, ctx)
    except (KeyError, TypeError, ValueError) as exc:
        return ToolOutcome(f"ERROR: invalid arguments for {name}: {exc}", ok=False)


def _search_players(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    query = str(args.get("query") or "").strip().lower()
    group, position = args.get("group"), args.get("position")
    team = str(args.get("team") or "").strip().upper()
    available_only = args.get("available_only", True) is not False
    sort_by = args.get("sort_by") or "last_season_fp"
    limit = max(1, min(50, int(args.get("limit") or 25)))
    rows = []
    for p in ctx.snapshot.players.values():
        if available_only and p.id in ctx.state.owner:
            continue
        if query and query not in p.name.lower():
            continue
        if group and p.group != group:
            continue
        if position and p.position != position:
            continue
        if team and p.team != team:
            continue
        rows.append(p)
    keys: dict[str, Callable[[Player], Any]] = {
        "last_season_fp": lambda p: (-(p.seasons[0].fantasy_points if p.seasons else 0), p.id),
        "fp_per_game_3yr": lambda p: (-fp_per_game(p), p.id),
        "age_asc": lambda p: (p.age if p.age is not None else 99, p.id),
        "age_desc": lambda p: (-(p.age or 0), p.id),
    }
    rows.sort(key=keys.get(sort_by, keys["last_season_fp"]))
    if not rows:
        return ToolOutcome("No players match those filters.")
    lines = [player_row(p, ctx.state) for p in rows[:limit]]
    return ToolOutcome(_clip(f"{len(rows)} match; showing {len(lines)}:\n" + "\n".join(lines)))


def _get_player(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    pid = int(args["player_id"])
    p = ctx.snapshot.players.get(pid)
    if p is None:
        return ToolOutcome(f"ERROR: no player with ID {pid} in the pool.", ok=False)
    out = [f"{p.name} (ID {p.id}) — {p.team} {p.position}, age {int(p.age)}{moved_label(p)}" if p.age
           else f"{p.name} (ID {p.id}) — {p.team} {p.position}{moved_label(p)}",
           f"born {p.birth_date or '?'}, shoots {p.shoots or '?'}, #{p.sweater or '?'}",
           f"status: {injury_label(p)}" + (f" — {p.injury.note}" if p.injury and p.injury.note else ""),
           f"owner: {ctx.state.owner.get(p.id) or 'available'}"]
    for s in p.seasons:
        if p.group == "G":
            out.append(f"{_season_label(s.season)} ({s.teams}): {s.gp}gp {s.gs or 0}gs {s.wins or 0}w {s.losses or 0}l "
                       f"{s.ot_losses or 0}otl {s.shutouts or 0}so sv%{(s.save_pct or 0):.3f} gaa{(s.gaa or 0):.2f} → {s.fantasy_points}fp")
        else:
            out.append(f"{_season_label(s.season)} ({s.teams}): {s.gp}gp {s.goals}g {s.assists}a {s.points}p "
                       f"+/-{s.plus_minus if s.plus_minus is not None else '?'} ppp {s.pp_points if s.pp_points is not None else '?'} "
                       f"shots {s.shots if s.shots is not None else '?'} toi {_mmss(s.toi_per_game_s)} pp {_mmss(s.pp_toi_per_game_s)} → {s.fantasy_points}fp")
    if not p.seasons:
        out.append("No NHL regular-season games in the last 3 seasons.")
    heads = [n for n in ctx.snapshot.news if p.id in n.player_ids][:5]
    for n in heads:
        out.append(f"news {n.published[:10]}: {n.headline}")
    return ToolOutcome(_clip("\n".join(out)))


def _get_team_schedule(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    team = str(args["team"]).strip().upper()
    games = ctx.snapshot.team_games.get(team)
    if not games:
        return ToolOutcome(f"ERROR: no schedule for club {team!r}.", ok=False)
    days = [date.fromisoformat(g) for g in games]
    b2b = sum(1 for a, b in zip(days, days[1:]) if (b - a).days == 1)
    start = date.fromisoformat(ctx.snapshot.as_of_date)
    monday = start - timedelta(days=start.weekday())
    weeks = []
    for i in range(6):
        lo, hi = monday + timedelta(weeks=i), monday + timedelta(weeks=i + 1)
        weeks.append(f"week of {lo.isoformat()}: {sum(1 for d in days if lo <= d < hi)}")
    return ToolOutcome(f"{team}: {len(days)} regular-season games, {b2b} back-to-backs, first game {days[0]}.\n" + "\n".join(weeks))


def _get_news(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    query = str(args.get("query") or "").lower()
    team = str(args.get("team") or "").upper()
    pid = args.get("player_id")
    limit = max(1, min(20, int(args.get("limit") or 10)))
    items = [n for n in ctx.snapshot.news
             if (not query or query in n.headline.lower()) and (not team or team in n.teams)
             and (pid is None or int(pid) in n.player_ids)]
    if not items:
        return ToolOutcome("No headlines match.")
    return ToolOutcome("\n".join(f"{n.published[:10]} [{','.join(n.teams) or '-'}] {n.headline}" for n in items[:limit]))


def _get_league_state(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    st = ctx.state
    lines = [f"Picks made: {len(st.picks)} of {len(ctx.slots)}"]
    for pk in st.picks[-40:]:
        p = ctx.snapshot.players.get(int(pk["player_id"]))
        who = f"{p.name} {p.team} {p.position}" if p else pk["player_id"]
        lines.append(f"#{pk['pick_no']} {st.team_label(pk['team'])}: {who}")
    lines.append("Roster counts (F/D/G, open spots):")
    for tid in st.order or st.teams:
        t = st.teams[tid]
        spots = ctx.rules.roster_size - len(t.roster)
        lines.append(f"  {st.team_label(tid)} [{t.model or 'control bot'}]: F{t.groups.get('F', 0)} D{t.groups.get('D', 0)} G{t.groups.get('G', 0)}, {spots} open")
    upcoming = [s for s in ctx.slots if s.pick_no > len(st.picks)][:16]
    lines.append("Next picks: " + ", ".join(f"#{s.pick_no} {s.team_id}" for s in upcoming))
    return ToolOutcome(_clip("\n".join(lines)))


def _get_my_team(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    team = ctx.state.teams[ctx.team_id]
    lines = [f"Team {ctx.state.team_label(team.id)} — {len(team.roster)}/{ctx.rules.roster_size} rostered"]
    for pid in team.roster:
        p = ctx.snapshot.players.get(pid)
        if p:
            lines.append("  " + player_row(p, ctx.state))
    need = {g: max(0, n - team.groups.get(g, 0)) for g, n in ctx.rules.minimums.items()}
    lines.append(f"Still need for a legal lineup: F{need['F']} D{need['D']} G{need['G']} "
                 f"(goalie cap {ctx.rules.max_goalies}); open spots {ctx.rules.roster_size - len(team.roster)}")
    mine = [s.pick_no for s in ctx.slots if s.team_id == team.id and s.pick_no > len(ctx.state.picks)]
    lines.append("Your remaining picks: " + (", ".join(f"#{n}" for n in mine) or "none"))
    queue = [pid for pid in team.queue if pid not in ctx.state.owner][:15]
    lines.append("Queue (available, top 15): " + (", ".join(
        f"{ctx.snapshot.players[q].name} ({q})" for q in queue if q in ctx.snapshot.players) or "empty"))
    size = len(ctx.notebook_path.read_text()) if ctx.notebook_path.exists() else 0
    lines.append(f"Notebook: {size}/{NOTEBOOK_MAX_CHARS} characters")
    return ToolOutcome("\n".join(lines))


def _notes_read(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    text = ctx.notebook_path.read_text() if ctx.notebook_path.exists() else ""
    return ToolOutcome(text or "(your notebook is empty)")


def _notes_write(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    text = str(args["text"])
    mode = args.get("mode") or "append"
    current = ctx.notebook_path.read_text() if ctx.notebook_path.exists() else ""
    new = text if mode == "replace" else (current + ("\n" if current else "") + text)
    if len(new) > NOTEBOOK_MAX_CHARS:
        return ToolOutcome(f"ERROR: notebook would be {len(new)} characters; the limit is {NOTEBOOK_MAX_CHARS}. "
                           "Use mode=replace with a condensed version.", ok=False)
    ctx.notebook_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.notebook_path.write_text(new)
    return ToolOutcome(f"Notebook saved ({len(new)}/{NOTEBOOK_MAX_CHARS} characters).",
                       action=Action("notes_write", {"chars": len(new)}))


def _update_draft_queue(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    ids = [int(x) for x in args["player_ids"]][:60]
    unknown = [i for i in ids if i not in ctx.snapshot.players]
    taken = [i for i in ids if i in ctx.state.owner]
    keep = list(dict.fromkeys(i for i in ids if i in ctx.snapshot.players and i not in ctx.state.owner))
    msg = f"Queue set: {len(keep)} players."
    if unknown:
        msg += f" Ignored unknown IDs: {unknown[:10]}."
    if taken:
        msg += f" Ignored already-drafted IDs: {taken[:10]}."
    return ToolOutcome(msg, action=Action("update_draft_queue", {"queue": keep}))


def _make_pick(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    if ctx.current_slot is None or ctx.current_slot.team_id != ctx.team_id:
        return ToolOutcome("ERROR: you are not on the clock.", ok=False)
    if args.get("player_id") in (None, ""):
        return ToolOutcome("ERROR: make_pick needs player_id: the player's numeric ID from search_players or your "
                           "briefing.", ok=False)
    pid = int(args["player_id"])
    p = ctx.snapshot.players.get(pid)
    if p is None:
        return ToolOutcome(f"ERROR: no player with ID {pid} in the pool. Use search_players to find valid IDs.", ok=False)
    if pid in ctx.state.owner:
        return ToolOutcome(f"ERROR: {p.name} was already drafted by {ctx.state.owner[pid]}.", ok=False)
    team = ctx.state.teams[ctx.team_id]
    problem = pick_problem(team.groups, p.group, ctx.rules)
    if problem:
        return ToolOutcome(f"ERROR: illegal pick ({p.group}): {problem}.", ok=False)
    ctx.extra["pick_id"] = pid
    call = " ".join(str(args.get("on_air_call") or "").split())
    if not call:
        return ToolOutcome("ERROR: on_air_call is required: what you say at the table, in your own voice.", ok=False)
    problem = (tag_problem(call) or delivery_problem(call) or construction_problem(call)
               or catchphrase_problem(call, ctx.state.teams[ctx.team_id].persona) or style_problem(ctx, call))
    if problem:
        return ToolOutcome(f"ERROR: on_air_call: {problem}.", ok=False)
    if spoken_length(call) > ON_AIR_CALL_MAX:
        return ToolOutcome(f"ERROR: on_air_call is {spoken_length(call)} spoken characters; the limit is {ON_AIR_CALL_MAX}. "
                           "Cut filler words, not the joke.", ok=False)
    ids = [pid] + ([int(ctx.state.picks[-1]["player_id"])] if ctx.state.picks else [])
    rationale_text = " ".join(str(args.get("public_rationale") or "").split())
    status, problems, note, rationale_status, rationale_problems = _gates(ctx, f"On air: {call}", call, ids,
                                                                         rationale=rationale_text)
    if problems or rationale_problems:
        both = problems + rationale_problems
        return ToolOutcome(fact_error(ctx, both, call + " " + rationale_text, [pid], "on_air_call or public_rationale",
                                      "make_pick", fields={"on_air_call": call, "public_rationale": rationale_text})
                           + (f" Also, table read: {note}." if note else ""), ok=False)
    if note:
        return ToolOutcome(f"ERROR: table read: {note}. Fix that part in your own words and call make_pick again.", ok=False)
    logic = " ".join(str(args.get("joke_logic") or "").split())
    if not logic:
        return ToolOutcome("ERROR: joke_logic is required: one sentence on why your on_air_call is funny. If you can't "
                           "say why, rewrite the line.", ok=False)
    if len(logic) > 200:
        return ToolOutcome(f"ERROR: joke_logic is {len(logic)} characters; the limit is 200.", ok=False)
    rationale = " ".join(str(args["public_rationale"]).split())
    if not rationale:
        return ToolOutcome("ERROR: public_rationale is required.", ok=False)
    if len(rationale) > 280:
        return ToolOutcome(f"ERROR: public_rationale is {len(rationale)} characters; the limit is 280.", ok=False)
    proj, lo, hi = int(args["projected_points"]), int(args["range_low"]), int(args["range_high"])
    if not (0 <= lo <= proj <= hi <= 300):
        return ToolOutcome("ERROR: need 0 <= range_low <= projected_points <= range_high <= 300.", ok=False)
    return ToolOutcome(
        f"Pick recorded: {p.name}.",
        action=Action("make_pick", {"player_id": pid, "on_air_call": call, "public_rationale": rationale, "joke_logic": logic,
                                    "fact_check": status, "rationale_check": rationale_status,
                                    "projected_points": proj, "range_80": [lo, hi]}),
        terminal=True,
    )


def _say(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    line = " ".join(str(args["line"]).split())
    if not line:
        return ToolOutcome("ERROR: line is empty.", ok=False)
    problem = (tag_problem(line) or delivery_problem(line) or construction_problem(line, max_sentences=2)
               or catchphrase_problem(line, ctx.state.teams[ctx.team_id].persona) or style_problem(ctx, line))
    if problem:
        return ToolOutcome(f"ERROR: {problem}.", ok=False)
    if spoken_length(line) > 200:
        return ToolOutcome(f"ERROR: line is {spoken_length(line)} spoken characters; the limit is 200. Say it shorter.", ok=False)
    status, problems, note, _, _ = _gates(ctx, line, line, [int(ctx.state.picks[-1]["player_id"])] if ctx.state.picks else [])
    if problems:
        return ToolOutcome(fact_error(ctx, problems, line, [], "line", "say")
                           + (f" Also, table read: {note}." if note else ""), ok=False)
    if note:
        return ToolOutcome(f"ERROR: table read: {note}. Fix that part in your own words and call say again.", ok=False)
    to = args.get("addressed_to")
    if to and to not in ctx.state.teams:
        to = None
    return ToolOutcome("On air.", action=Action("say", {"line": line, "addressed_to": to, "fact_check": status}),
                       terminal=True)


def _gates(ctx: ToolContext, fact_text: str, line: str, extra_ids: list[int], rationale: str = ""
           ) -> tuple[str, list[str], str | None, str, list[str]]:
    """The fact checks and the table read (consistency + comedy editor) run at the same time: each is a few seconds
    of outside models, and a pick shouldn't wait for them one after the other. The spoken line and the written
    rationale are checked separately: only the spoken line decides whether a call can air."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(3) as pool:
        facts = pool.submit(fact_check, ctx, fact_text, extra_ids)
        why = pool.submit(fact_check, ctx, f"Written rationale: {rationale}", extra_ids, "rationale") if rationale else None
        read = pool.submit(table_read, ctx, line)
        status, problems = facts.result()
        rationale_status, rationale_problems = why.result() if why else ("off", [])
        note = read.result()
    return status, problems, note, rationale_status, rationale_problems


def lines_so_far(state: LeagueState, n: int = 6) -> list[str]:
    """The last few things said at the table, as of now (table talk lands while the next GM is researching)."""
    said = [(pk.get("seq") or 0, pk["team"], pk.get("on_air_call") or "") for pk in state.picks if pk.get("on_air_call")]
    said += [(s.get("seq") or 0, s["team"], s.get("line") or "") for s in state.says]
    return [f"{state.team_label(team)}: {line}" for _, team, line in sorted(said)[-n:]]


def said_tonight(state: LeagueState) -> list[str]:
    return [pk.get("on_air_call") or "" for pk in state.picks] + [s.get("line") or "" for s in state.says]


_NAME_WORDS: dict[int, frozenset[str]] = {}


def name_words(ctx: ToolContext) -> frozenset[str]:
    """Player, GM and franchise names: shared vocabulary that never makes two lines the same bit."""
    key = id(ctx.snapshot)
    if key not in _NAME_WORDS:
        words = {w for p in ctx.snapshot.players.values() for w in p.name.lower().replace("-", " ").split()}
        _NAME_WORDS[key] = frozenset(words)
    league = {w for t in ctx.state.teams.values() for x in (ctx.state.team_label(t.id), (t.persona or {}).get("gm_name"),
              (t.persona or {}).get("franchise_name"), (t.persona or {}).get("hometown"),
              (t.persona or {}).get("cup_pick")) if x for w in str(x).lower().replace(",", " ").split()}
    return _NAME_WORDS[key] | league


def style_problem(ctx: ToolContext, text: str) -> str | None:
    """The slop gates: invented human life, stock phrases, and a bit someone already did tonight."""
    return (backstory_problem(text) or slop_problem(text)
            or repeat_problem(text, said_tonight(ctx.state), ignore=name_words(ctx)))


MAX_PRODUCER_NOTES = 2  # per line: a rewrite gets read again once; after that the GM's words stand


def table_read(ctx: ToolContext, text: str) -> str | None:
    """One note per line, the GM's rewrite stands. Two reads run in parallel: does the line contradict what happened
    tonight (TableRead), and does it land with a viewer (JokeCheck, the comedy editor)?"""
    reader, editor = ctx.extra.get("tableread"), ctx.extra.get("jokecheck")
    if (reader is None and editor is None) or ctx.extra.get("table_noted", 0) >= MAX_PRODUCER_NOTES:
        return None
    from concurrent.futures import ThreadPoolExecutor

    from gmbench.agent.factcheck import draft_board
    from gmbench.agent.tableread import roster_text
    speaker = ctx.state.team_label(ctx.team_id)
    board = draft_board(ctx.state, ctx.snapshot, ctx.team_id, full=True)
    recent, roster = lines_so_far(ctx.state) or ctx.extra.get("recent_lines") or [], roster_text(ctx.state)
    checker = ctx.extra.get("factcheck")
    if checker is not None and hasattr(checker, "facts_for"):  # current teams and stats, so nobody reads from memory
        ids = [int(pk["player_id"]) for pk in ctx.state.picks[-2:]]
        if ctx.current_slot is not None and ctx.extra.get("pick_id"):
            ids.append(int(ctx.extra["pick_id"]))
        roster += "\n\nPlayer facts (our data, current as of today; trust these over memory):\n" + checker.facts_for(text, ids)
    with ThreadPoolExecutor(2) as pool:
        jobs = []
        if reader is not None:
            jobs.append(pool.submit(reader.read, text, speaker, board, recent, roster))
        if editor is not None:
            jobs.append(pool.submit(editor.read, text, speaker, board, recent, roster, ctx.extra.get("kind") or "pick"))
        notes = [n for n in (j.result() for j in jobs) if n]
    if notes:
        ctx.extra["table_noted"] = ctx.extra.get("table_noted", 0) + 1
        return " Also: ".join(notes)
    return None


def _squash(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def fact_error(ctx: ToolContext, claims: list[str], text: str, extra_ids: list[int], what: str, tool: str,
               fields: dict[str, str] | None = None) -> str:
    facts = ctx.extra["factcheck"].facts_for(text, extra_ids)

    def where(claim: str) -> str:  # say which text the claim is in, so the GM fixes the right one
        hits = [name for name, body in (fields or {}).items() if _squash(claim)[:40] in _squash(body)]
        return f'"{claim}" (in your {hits[0]})' if hits else f'"{claim}"'
    return ("ERROR: fact check. These claims contradict our data: " + "; ".join(where(c) for c in claims)
            + f". Our data, current as of today:\n{facts}\nFix the {what} (keep the joke if you can) and call {tool} again.")


def fact_check(ctx: ToolContext, text: str, extra_ids: list[int], what: str = "fact") -> tuple[str, list[str]]:
    """('ok'|'unverified'|'off', problems). Problems send the text back to the GM, at most max_rejections times per
    kind of text (the spoken line and the written rationale keep separate counts)."""
    checker = ctx.extra.get("factcheck")
    if checker is None:
        return "off", []
    counter = f"{what}_rejections"
    if ctx.extra.get(counter, 0) >= checker.max_rejections:
        return "unverified", []
    leaders = ""
    if ctx.phase in ("draft", "broadcast"):  # "best left on the board" is checkable only against who's left
        from gmbench.agent.factcheck import board_leaders
        leaders = board_leaders(ctx.snapshot, ctx.state.owner)
    problems = checker.check(text, extra_ids, leaders=leaders)  # NHL facts only: table context made the checker flag honest lines
    if problems:
        ctx.extra[counter] = ctx.extra.get(counter, 0) + 1
        return "rejected", problems
    return "ok", []


HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], ToolOutcome]] = {
    "say": _say,
    "search_players": _search_players,
    "get_player": _get_player,
    "get_team_schedule": _get_team_schedule,
    "get_news": _get_news,
    "get_league_state": _get_league_state,
    "get_my_team": _get_my_team,
    "notes_read": _notes_read,
    "notes_write": _notes_write,
    "update_draft_queue": _update_draft_queue,
    "make_pick": _make_pick,
}


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


def needs_summary(groups: Counter, rules: RosterRules) -> str:
    need = {g: max(0, n - groups.get(g, 0)) for g, n in rules.minimums.items()}
    return f"F{need['F']} D{need['D']} G{need['G']} (unmet total {unmet_minimums(groups, rules)})"
