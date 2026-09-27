"""A scripted stand-in model for rehearsals and tests: exercises every harness path at zero cost."""
from __future__ import annotations

import json
import re
import threading
from typing import Any

from gmbench.config import TeamSpec
from gmbench.llm.openrouter import LLMResult

ROW = re.compile(r"^(\d{5,8}) \| [^|]+\| \S+ (C|L|R|D|G) ", re.M)
NEEDS = re.compile(r"Still needed for a legal lineup: F(\d+) D(\d+) G(\d+).*picks left including this one: ([#\d, ]+)")
GROUP = {"C": "F", "L": "F", "R": "F", "D": "D", "G": "G"}


class FakeClient:
    """Deterministic GM: searches for its most-needed group, queues a few players, then picks the best legal one."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def chat(self, spec: TeamSpec, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
             effort: str | None = None, max_tokens: int = 0, session_id: str | None = None) -> LLMResult:
        names = {t["function"]["name"] for t in tools or []}
        if "say" in names:
            return self._result(spec, [("say", {"line": f"{spec.display} has thoughts on that pick."})])
        if "submit_persona" in names:
            return self._result(spec, [("submit_persona", _fake_persona(spec))])
        if "finish_prep" in names:
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            if not tool_msgs:
                return self._result(spec, [("search_players", {"limit": 40}), ("notes_write", {"text": "Plan: best available."})])
            ids = [int(pid) for pid in re.findall(r"^(\d{5,8}) \| ", tool_msgs[0]["content"], re.M)]
            if len(tool_msgs) < 3:
                return self._result(spec, [("update_draft_queue", {"player_ids": ids[:40]})])
            return self._result(spec, [("finish_prep", {"summary": "Best available, fill goalies late."})])
        if "submit_post_draft" in names:
            return self._result(spec, [("submit_post_draft", _fake_post_draft(spec, messages))])
        if "make_pick" not in names:
            return self._result(spec, [], content="(fake model has nothing to do)")

        briefing = next(m["content"] for m in messages if m["role"] == "user")
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if not tool_msgs:
            group = _forced_group(briefing) or "F"
            return self._result(spec, [("search_players", {"group": group, "limit": 30}),
                                       ("get_my_team", {})])
        rows = [(int(pid), GROUP[pos]) for pid, pos in ROW.findall(tool_msgs[0]["content"] + "\n" + briefing)]
        tried = {int(a["player_id"]) for a in _pick_attempts(messages)}
        forced = _forced_group(briefing)
        for pid, group in rows:
            if pid in tried or (forced and group != forced):
                continue
            if len(tried) == 0 and len(tool_msgs) == 2:
                return self._result(spec, [("update_draft_queue", {"player_ids": [r[0] for r in rows[:8]]}),
                                           ("make_pick", _pick_args(pid))])
            return self._result(spec, [("make_pick", _pick_args(pid))])
        return self._result(spec, [], content="I can't find a legal player.")

    def _result(self, spec: TeamSpec, calls: list[tuple[str, dict[str, Any]]], content: str = "") -> LLMResult:
        with self._lock:
            self._n += 1
            n = self._n
        tool_calls = [{"id": f"call_{n}_{i}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(calls)]
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return LLMResult(message=message, finish_reason="tool_calls" if tool_calls else "stop", model=spec.model,
                         provider="Fake", usage={"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.0},
                         cost_usd=0.0, latency_s=0.01, gen_id=f"fake-{n}")


def _forced_group(briefing: str) -> str | None:
    m = NEEDS.search(briefing)
    if not m:
        return None
    need = {"F": int(m.group(1)), "D": int(m.group(2)), "G": int(m.group(3))}
    left = m.group(4).count("#")
    if sum(need.values()) >= left:
        return max(need, key=lambda g: need[g])
    return None


def _pick_args(pid: int) -> dict[str, Any]:
    return {"player_id": pid, "on_air_call": "[shouting] Best player on the board, baby!",
            "public_rationale": "Best player on my board who fits the roster.",
            "joke_logic": "Scripted test line; the joke is that there is no joke.",
            "projected_points": 60, "range_low": 40, "range_high": 80}


def _pick_attempts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            if call["function"]["name"] == "make_pick":
                out.append(json.loads(call["function"]["arguments"]))
    return out


TEAM_LINE = re.compile(r"^\d+\. (\w+): ", re.M)


def _fake_post_draft(spec: TeamSpec, messages: list[dict[str, Any]]) -> dict[str, Any]:
    system = messages[0]["content"]
    briefing = next(m["content"] for m in messages if m["role"] == "user")
    teams = TEAM_LINE.findall(system)
    mine = briefing.split("All rosters")[0]
    need, active, bench = {"F": 6, "D": 4, "G": 2}, [], []
    for pid, pos in re.findall(r"(\d{5,8}) \| [^|]+\| \S+ (C|L|R|D|G) ", mine):
        g = GROUP[pos]
        (active if need[g] > 0 else bench).append(int(pid))
        need[g] -= 1
    return {"active": active, "bench": bench,
            "grades": [{"team": t, "grade": "B", "comment": f"Solid enough draft from {t}."} for t in teams if t != spec.id],
            "predicted_standings": teams, "projected_points": 900, "range_low": 750, "range_high": 1050}


def _fake_persona(spec: TeamSpec) -> dict[str, Any]:
    return {"gm_name": f"{spec.id.title()} Fakerson", "franchise_city": "Testville", "hometown": "Ponoka, AB", "favorite_nhl_team": "Oilers", "franchise_nickname": f"{spec.id.title()}s",
            "franchise_abbrev": (spec.id.upper() + "XXX")[:3].replace("UTA", "UTX"), "primary_color": "#0E1B4D",
            "secondary_color": "#E32402", "tagline": "Fake it till you make it.", "bio": "A scripted test GM.",
            "personality": ["scripted", "tidy", "cheap"], "catchphrase": "Deterministic!",
            "signature_call": "Scripted and SELECTED!", "celebration": "A stiff robotic fist pump, twice.",
            "strategy_philosophy": "Best available.",
            "trash_talk_style": "Polite.", "voice_description": "A calm mid-range test voice, medium pace, neutral tone, clear diction, " * 2,
            "avatar_description": "A friendly placeholder character with a round face, visible mouth, short hair and a calm smile. " * 2,
            "suit": {"fabric_id": "cavani-1100-21101-2", "jacket": {"lapel_style": "notch", "lapel_width": "standard",
                     "button_layout": "2-button", "venting": "double-vent", "pocket_style": "flap", "lining": "matching"},
                     "pants": {"pleats": "flat-front", "cuffs": "no-cuff", "fastening": "belt-loops"},
                     "shirt": "white", "tie": "navy", "pocket_square": "none", "rationale": "Classic."}}
