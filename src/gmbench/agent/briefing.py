"""Per-session system prompts and briefings, assembled from identical templates for every GM."""
from __future__ import annotations

from gmbench.agent.factcheck import notable_think
from gmbench.agent.prompts import GM_SYSTEM, persona_block, system_template_values
from gmbench.agent.tools import ToolContext, player_row
from gmbench.agent.untrusted import wrap
from gmbench.config import LeagueConfig
from gmbench.data.models import Player
from gmbench.state import LeagueState


def league_table(state: LeagueState, cfg: LeagueConfig) -> str:
    order = state.order or [t.id for t in cfg.teams]
    lines = []
    for i, tid in enumerate(order, 1):
        spec = cfg.team(tid)
        persona = state.teams[tid].persona if tid in state.teams else None
        per = persona or {}
        facts = [x for x in (per.get("franchise_name"),
                             f"Cup pick: {per['cup_pick']}" if per.get("cup_pick") else None) if x]
        lines.append(f"{i}. {spec.name}: {spec.display} ({spec.lab})" + (f", {', '.join(facts)}" if facts else ""))
    picks = cup_counts(state)
    if picks:
        lines.append("Stanley Cup picks at the table: " + picks)
    return "\n".join(lines)


def cup_counts(state: LeagueState) -> str:
    """Who picked which team to win the Cup, counted, so nobody says "half the table" about three GMs."""
    by_team: dict[str, list[str]] = {}
    for tid in state.order or list(state.teams):
        team = ((state.teams[tid].persona or {}).get("cup_pick")) if tid in state.teams else None
        if team:
            by_team.setdefault(team, []).append(state.team_label(tid))
    return "; ".join(f"{team} {len(who)} ({', '.join(who)})"
                     for team, who in sorted(by_team.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def build_system(team_id: str, state: LeagueState, cfg: LeagueConfig, *, today: str, as_of: str, task: str) -> str:
    spec = cfg.team(team_id)
    persona = state.teams[team_id].persona
    gm_label = spec.name
    body = GM_SYSTEM.substitute(
        gm_label=gm_label,
        model_display=f"{spec.display}, built by {spec.lab}",
        today=today,
        as_of=as_of,
        persona_block=persona_block(persona),
        league_table=league_table(state, cfg),
        **system_template_values(),
    )
    return body + "\n" + task


def best_available(ctx: ToolContext, *, overall: int = 30, per_d: int = 8, per_g: int = 6) -> list[Player]:
    pool = [p for p in ctx.snapshot.players.values() if p.id not in ctx.state.owner]

    def last_fp(p: Player) -> tuple[int, int]:
        return (-(p.seasons[0].fantasy_points if p.seasons else 0), p.id)

    picked: dict[int, Player] = {}
    for p in sorted(pool, key=last_fp)[:overall]:
        picked[p.id] = p
    for group, n in (("D", per_d), ("G", per_g)):
        for p in sorted((q for q in pool if q.group == group), key=last_fp)[:n]:
            picked.setdefault(p.id, p)
    return sorted(picked.values(), key=last_fp)


def gm_label(st: LeagueState, team_id: str) -> str:
    """How everyone at the table knows a GM: by its model name."""
    if st.teams[team_id].is_bot:
        return "the robot (Autodraft, the league's control bot)"
    return st.team_label(team_id)


def previous_pick(ctx: ToolContext, *, table_lines: int = 3) -> list[str]:
    """The pick right before yours and what was said about it: the set-up for your own on-air call."""
    st, me = ctx.state, ctx.team_id
    if not st.picks:
        return ["You're opening the draft: nobody has picked yet. Get the party going, then make your pick.", ""]
    last = st.picks[-1]
    p = ctx.snapshot.players.get(int(last["player_id"]))
    who = f"{p.name} ({p.team} {p.position})" if p else str(last["player_id"])
    if last["team"] == me:
        return [f"The pick right before yours was your own: #{last['pick_no']}, {who}. You pick back-to-back at the turn.", ""]
    out = [f"The pick right before yours: #{last['pick_no']}, {gm_label(st, last['team'])} took {who}."]
    notes = []
    if (last.get("auto") or {}).get("reason") != "control_bot":  # the robot is opaque: its picks show, its method doesn't
        note = notable_think(last, st.picks)
        if note:
            notes.append(f"{st.team_label(last['team'])} {note}.")
    if p and p.group in ("G", "D") and not any(
            (q := ctx.snapshot.players.get(int(x["player_id"]))) and q.group == p.group for x in st.picks[:-1]):
        notes.append("He's the first " + ("goalie" if p.group == "G" else "defenceman") + " off the board.")
    if int(last["player_id"]) in st.teams[me].queue[:3]:
        notes.append("He was near the top of YOUR queue.")
    if notes:
        out.append(" ".join(notes))
    said = last.get("on_air_call") or ""
    auto = (last.get("auto") or {}).get("reason")
    if said:
        out.append("What they said at the table: " + wrap(said, last["team"]).replace("\n", " "))
    elif auto == "control_bot":
        out.append("The robot doesn't talk.")
    elif auto:
        out.append("They couldn't be reached, so the league auto-picked for them from their own queue.")
    my_last = max((x.get("seq") or 0 for x in st.picks if x["team"] == me), default=0)
    talk = [x for x in st.says if (x.get("seq") or 0) > my_last and x["team"] != me][-table_lines:]
    if talk:
        out.append("Heard at the table since your last pick:")
        out.extend(f"- {gm_label(st, x['team'])}: " + wrap(x["line"], x["team"]).replace("\n", " ") for x in talk)
    out.append("")
    return out


def draft_briefing(ctx: ToolContext, *, recent: int = 14, notebook_chars: int = 1500) -> str:
    slot = ctx.current_slot
    assert slot is not None
    st = ctx.state
    team = st.teams[ctx.team_id]
    teams_n = len(st.order) or len(st.teams)
    in_round = (slot.pick_no - 1) % teams_n + 1
    out = [f"DRAFT — pick #{slot.pick_no} (round {slot.round}, pick {in_round} of {teams_n}). You are on the clock.", ""]
    out.extend(previous_pick(ctx))

    out.append(f"Your roster ({len(team.roster)}/{ctx.rules.roster_size}):")
    for pid in team.roster:
        p = ctx.snapshot.players.get(pid)
        if p:
            out.append("  " + player_row(p, st))
    if not team.roster:
        out.append("  (empty)")
    need = {g: max(0, n - team.groups.get(g, 0)) for g, n in ctx.rules.minimums.items()}
    mine = [s.pick_no for s in ctx.slots if s.team_id == team.id and s.pick_no > len(st.picks)]
    out.append(f"Still needed for a legal lineup: F{need['F']} D{need['D']} G{need['G']} · goalie cap {ctx.rules.max_goalies} · "
               f"your picks left including this one: {', '.join(f'#{n}' for n in mine)}")
    out.append("")

    queue = [pid for pid in team.queue if pid not in st.owner and pid in ctx.snapshot.players][:12]
    if queue:
        out.append("Your queue (still available): " + "; ".join(f"{ctx.snapshot.players[q].name} ({q})" for q in queue))
        out.append("")

    out.append("Best available by 2025-26 fantasy points (plus the top D and G):")
    out.extend(player_row(p, st) for p in best_available(ctx))
    out.append("")

    if st.picks:
        out.append(f"Last {min(recent, len(st.picks))} picks:")
        for pk in st.picks[-recent:]:
            p = ctx.snapshot.players.get(int(pk["player_id"]))
            who = f"{p.name} ({p.team} {p.position})" if p else str(pk["player_id"])
            out.append(f"#{pk['pick_no']} {st.team_label(pk['team'])} took {who}")  # what was SAID is in the section above
        out.append("")

    notes = ctx.notebook_path.read_text() if ctx.notebook_path.exists() else ""
    if notes:
        more = " …(notes_read for the rest)" if len(notes) > notebook_chars else ""
        out.append(f"Your notebook (start):\n{notes[:notebook_chars]}{more}")
    else:
        out.append("Your notebook is empty.")
    out.append("")
    out.append("Research as needed, then call make_pick.")
    return "\n".join(out)
