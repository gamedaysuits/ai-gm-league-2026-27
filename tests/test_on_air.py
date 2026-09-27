"""On-air rules: delivery-tag allowlist, spoken length, and the required podium call."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from gmbench.agent.prompts import spoken_length, tag_problem
from gmbench.agent.tools import ToolContext, dispatch
from gmbench.data.synthetic import synthetic_snapshot
from gmbench.rules import RosterRules, Slot
from gmbench.state import LeagueState, TeamState

RULES = RosterRules(minimums={"F": 6, "D": 4, "G": 2}, bench=2, max_goalies=3)


def test_tag_allowlist_and_limits() -> None:
    assert tag_problem("[shouting] KNIFE'S OUT! [laughs] Poison, baby!") is None
    assert "unknown" in tag_problem("[dances] Let's go")
    assert "at most" in tag_problem("[shouting] a [laughs] b [excited] c [gasps] d")
    assert spoken_length("[shouting] Hi   there [laughs]") == len("Hi there")


def _ctx(tmp: Path) -> ToolContext:
    snap = synthetic_snapshot(3)
    state = LeagueState(teams={"a": TeamState(id="a", lab="x", model="m/a")}, order=["a"])
    return ToolContext(team_id="a", state=state, snapshot=snap, rules=RULES, slots=[Slot(1, 1, "a")],
                       notebook_path=tmp / "a.md", current_slot=Slot(1, 1, "a"))


def test_make_pick_requires_a_valid_on_air_call(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    pid = next(p.id for p in ctx.snapshot.players.values() if p.group == "F")
    base = {"player_id": pid, "public_rationale": "Best forward left.", "joke_logic": "Calls the pick obvious.", "projected_points": 80, "range_low": 60, "range_high": 100}
    assert "on_air_call is required" in dispatch("make_pick", base, ctx).text
    assert "unknown delivery tag" in dispatch("make_pick", {**base, "on_air_call": "[moonwalks] Mine!"}, ctx).text
    ok = dispatch("make_pick", {**base, "on_air_call": "[shouting] POCKETED! Thanks for the donation!"}, ctx)
    assert ok.ok and ok.terminal and ok.action.args["on_air_call"].startswith("[shouting]")


def test_say_rejects_unknown_tags(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert not dispatch("say", {"line": "[twirls] nice pick"}, ctx).ok
    assert dispatch("say", {"line": "[laughs] The spreadsheet bought a security system!"}, ctx).ok


def _party(tmp: Path) -> tuple[ToolContext, list[int]]:
    snap = synthetic_snapshot(3)
    teams = {"a": TeamState(id="a", lab="x", model="m/a"), "b": TeamState(id="b", lab="y", model="m/b"),
             "bot": TeamState(id="bot", lab="league", model=None)}
    teams["a"].persona = {"gm_name": "Gully 'Snowplow' Lindqvist", "franchise_name": "Ponoka Plowboys"}
    state = LeagueState(teams=teams, order=["a", "b", "bot"])
    fwd = [p.id for p in snap.players.values() if p.group == "F"][:3]
    ctx = ToolContext(team_id="b", state=state, snapshot=snap, rules=RULES, slots=[], notebook_path=tmp / "b.md",
                      current_slot=Slot(2, 1, "b"))
    return ctx, fwd


def test_previous_pick_sets_up_the_reaction(tmp_path: Path) -> None:
    from gmbench.agent.briefing import previous_pick

    ctx, fwd = _party(tmp_path)
    assert "opening the draft" in " ".join(previous_pick(ctx))
    ctx.state.teams["b"].queue = [fwd[0]]
    ctx.state.picks.append({"pick_no": 1, "team": "a", "player_id": fwd[0], "on_air_call": "Beauty. Mine now, boys.",
                            "seq": 5})
    ctx.state.says.append({"team": "a", "line": "You're next, bud.", "seq": 6, "pick_no": 1})
    text = "\n".join(previous_pick(ctx))
    assert "Gully 'Snowplow' Lindqvist of the Ponoka Plowboys (a) took" in text
    assert "near the top of YOUR queue" in text and "Beauty. Mine now, boys." in text and "untrusted" in text
    assert "You're next, bud." in text
    ctx.state.picks.append({"pick_no": 2, "team": "bot", "player_id": fwd[1], "auto": {"reason": "control_bot"}})
    assert "robot doesn't talk" in " ".join(previous_pick(ctx))
    ctx.state.picks.append({"pick_no": 3, "team": "b", "player_id": fwd[2], "on_air_call": "Mine.", "seq": 9})
    assert "your own" in " ".join(previous_pick(ctx)) and "turn" in " ".join(previous_pick(ctx))


def test_comeback_goes_to_the_gm_who_just_got_ribbed() -> None:
    from gmbench.draft.floor import FloorManager

    snap = synthetic_snapshot(3)
    teams = {t: TeamState(id=t, lab="x", model=f"m/{t}") for t in ("a", "b", "c")}
    teams["bot"] = TeamState(id="bot", lab="league", model=None)
    state = LeagueState(teams=teams, order=["a", "b", "c", "bot"])
    slots = [Slot(i + 1, 1, t) for i, t in enumerate(["a", "b", "c", "bot"])]
    fwd = [p.id for p in snap.players.values() if p.group == "F"][:4]
    floor = FloorManager(snap, slots)
    first = {"pick_no": 1, "round": 1, "team": "a", "player_id": fwd[0], "on_air_call": "Opening the party."}
    state.picks.append(first)
    assert floor.after_pick(state, first, {}) == []  # nobody to fire back at yet
    second = {"pick_no": 2, "round": 1, "team": "b", "player_id": fwd[1], "on_air_call": "Bold pick, a. Mine's better."}
    state.picks.append(second)
    cues = [c for c in floor.after_pick(state, second, {}) if c.kind == "comeback"]
    assert len(cues) == 1 and cues[0].team_id == "a" and cues[0].addressed_to == "b"
    # the next picker answers in their own call, so they get no separate comeback
    third = {"pick_no": 3, "round": 1, "team": "c", "player_id": fwd[2], "on_air_call": "b, really?"}
    state.picks.append(third)
    floor.slots.append(Slot(4, 1, "b"))  # pretend b picks next
    floor.slots[:] = [Slot(1, 1, "a"), Slot(2, 1, "b"), Slot(3, 1, "c"), Slot(4, 1, "b")]
    assert not [c for c in floor.after_pick(state, third, {}) if c.kind == "comeback"]


def test_pick_call_cannot_open_with_a_laugh(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    pid = next(p.id for p in ctx.snapshot.players.values() if p.group == "F")
    base = {"player_id": pid, "public_rationale": "Best forward left.", "joke_logic": "Calls the pick obvious.", "projected_points": 80, "range_low": 60, "range_high": 100}
    bad = dispatch("make_pick", {**base, "on_air_call": "[chuckles] Bold pick, buddy. I'll take this guy."}, ctx)
    assert not bad.ok and "laugh" in bad.text
    ok = dispatch("make_pick", {**base, "on_air_call": "Bold pick, buddy. [pause] I'll take the guy who shows up. [laughs]"}, ctx)
    assert ok.ok


def test_pick_requires_joke_logic(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    pid = next(p.id for p in ctx.snapshot.players.values() if p.group == "F")
    args = {"player_id": pid, "public_rationale": "Best forward left.", "projected_points": 80, "range_low": 60,
            "range_high": 100, "on_air_call": "Bold pick, buddy. I'll take the guy who shows up."}
    out = dispatch("make_pick", args, ctx)
    assert not out.ok and "joke_logic" in out.text
    assert dispatch("make_pick", {**args, "joke_logic": "Mocks the last pick as a no-show."}, ctx).ok


class _StubChecker:
    max_rejections = 2

    def __init__(self) -> None:
        self.calls = 0

    def check(self, text: str, extra: list[int]) -> list[str]:
        self.calls += 1
        return ["Hughes drives Vancouver"] if "Vancouver" in text else []

    def facts_for(self, text: str, extra: list[int]) -> str:
        return "Quinn Hughes: D, current team MIN."


def test_fact_check_bounces_false_claims_then_marks_unverified(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.extra["factcheck"] = _StubChecker()
    pid = next(p.id for p in ctx.snapshot.players.values() if p.group == "F")
    args = {"player_id": pid, "public_rationale": "Best forward left.", "joke_logic": "Mocks a golf-course team.",
            "projected_points": 80, "range_low": 60, "range_high": 100,
            "on_air_call": "Hughes drives Vancouver straight to the golf course. I'll take this guy."}
    first = dispatch("make_pick", args, ctx)
    assert not first.ok and "fact check" in first.text and "MIN" in first.text
    fixed = dispatch("make_pick", {**args, "on_air_call": "Hughes drives Minnesota to the golf course. Mine."}, ctx)
    assert fixed.ok and fixed.action.args["fact_check"] == "ok"
    ctx.extra["fact_rejections"] = 2  # out of chances: accepted, but never aired
    last = dispatch("make_pick", args, ctx)
    assert last.ok and last.action.args["fact_check"] == "unverified"
