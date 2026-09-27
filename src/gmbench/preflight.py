"""Preflight: prove every competitor can run a pinned, multi-turn tool loop on OpenRouter.

Each model must look up two players with a research tool, then submit a valid
`make_pick` for the stronger one. We record the served model and provider,
latency, token usage (including reasoning and cached tokens), and cost, so any
model that can't route, can't call tools, or drops reasoning is caught before
the draft.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from gmbench.config import ROOT, LeagueConfig, TeamSpec
from gmbench.llm.openrouter import (
    LLMResult,
    OpenRouterClient,
    OpenRouterError,
    assistant_message_for_replay,
)

PLAYERS = {
    "connor mcdavid": {"player_id": 8478402, "name": "Connor McDavid", "team": "EDM", "pos": "C",
                       "season": "2025-26", "gp": 82, "goals": 48, "assists": 90, "points": 138},
    "nathan mackinnon": {"player_id": 8477492, "name": "Nathan MacKinnon", "team": "COL", "pos": "C",
                         "season": "2025-26", "gp": 80, "goals": 45, "assists": 82, "points": 127},
}
EXPECTED_PICK = 8478402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_player",
            "description": "Look up a player's 2025-26 NHL regular-season stats by full name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Player full name"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_pick",
            "description": "Submit your draft pick. This ends your turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NHL player ID from lookup_player"},
                    "public_rationale": {"type": "string", "description": "One or two sentences, max 280 characters."},
                    "projected_points": {"type": "integer", "description": "Your projection for his 2026-27 fantasy points."},
                },
                "required": ["player_id", "public_rationale", "projected_points"],
                "additionalProperties": False,
            },
        },
    },
]

SYSTEM = (
    "You are the general manager of a fantasy hockey team in a league of AI GMs. "
    "Today is 2026-09-26. You act only through the provided tools."
)
USER = (
    "Preflight check. Use lookup_player to get 2025-26 stats for Connor McDavid and Nathan MacKinnon, "
    "then call make_pick for whichever scored more points."
)
NUDGE = "Please continue using the tools. Call make_pick to finish your turn."


class PickArgs(BaseModel):
    player_id: int
    public_rationale: str = Field(max_length=280)
    projected_points: int = Field(ge=0, le=250)


def run_one(client: OpenRouterClient, spec: TeamSpec, *, effort: str, max_turns: int = 5) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]
    turns: list[dict[str, Any]] = []
    outcome, detail, nudged = "no_pick", "", False
    started = time.monotonic()
    session_id = f"preflight:{spec.id}:{int(time.time())}"
    for _ in range(max_turns):
        try:
            res = client.chat(spec, messages, TOOLS, effort=effort, max_tokens=16000, session_id=session_id)
        except OpenRouterError as exc:
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
            break
        turns.append(_turn_record(res))
        messages.append(assistant_message_for_replay(res.message))
        if not res.tool_calls:
            if nudged:
                outcome, detail = "no_tool_call", (res.content or res.finish_reason or "")[:200]
                break
            nudged = True
            messages.append({"role": "user", "content": NUDGE})
            continue
        picked = False
        for call in res.tool_calls:
            name = call.get("function", {}).get("name")
            raw_args = call.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args, result = {}, {"error": "arguments were not valid JSON"}
            else:
                result = _dispatch(name, args)
            if name == "make_pick" and "error" not in result:
                picked = True
                outcome = "pass" if args.get("player_id") == EXPECTED_PICK else "wrong_pick"
                detail = str(args.get("public_rationale", ""))[:160]
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(result)})
        if picked:
            break
    return {
        "team": spec.id,
        "model": spec.model,
        "providers": list(spec.providers),
        "outcome": outcome,
        "detail": detail,
        "turns": turns,
        "wall_s": round(time.monotonic() - started, 2),
        "cost_usd": round(sum(t["cost_usd"] or 0 for t in turns), 6),
    }


def _dispatch(name: str | None, args: dict[str, Any]) -> dict[str, Any]:
    if name == "lookup_player":
        hit = PLAYERS.get(str(args.get("name", "")).strip().lower())
        return hit or {"error": f"no player named {args.get('name')!r}"}
    if name == "make_pick":
        try:
            PickArgs.model_validate(args)
        except ValidationError as exc:
            return {"error": f"invalid make_pick arguments: {exc.errors()[0]['msg']}"}
        return {"ok": True}
    return {"error": f"unknown tool {name!r}"}


def _turn_record(res: LLMResult) -> dict[str, Any]:
    return {
        "served_model": res.model,
        "provider": res.provider,
        "finish_reason": res.finish_reason,
        "latency_s": res.latency_s,
        "attempts": res.attempts,
        "prompt_tokens": res.usage.get("prompt_tokens"),
        "completion_tokens": res.usage.get("completion_tokens"),
        "reasoning_tokens": res.reasoning_tokens,
        "cached_tokens": res.cached_tokens,
        "cost_usd": res.cost_usd,
        "tool_calls": [c.get("function", {}).get("name") for c in res.tool_calls],
        "has_reasoning_details": bool(res.message.get("reasoning_details")),
        "content_chars": len(res.content),
    }


def run_preflight(cfg: LeagueConfig, *, teams: list[str] | None = None, effort: str = "medium") -> Path:
    specs = [t for t in cfg.teams if not t.is_bot and (not teams or t.id in teams)]
    client = OpenRouterClient(timeout_s=600, retries=2)
    try:
        with ThreadPoolExecutor(max_workers=len(specs)) as pool:
            results = list(pool.map(lambda s: run_one(client, s, effort=effort), specs))
    finally:
        client.close()

    out_dir = ROOT / "telemetry" / "preflight"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"preflight-{stamp}.json"
    path.write_text(json.dumps({"effort": effort, "results": results}, indent=2))

    print(f"{'team':<9} {'outcome':<12} {'turns':>5} {'wall_s':>7} {'cost$':>8} {'reason_tok':>10} {'rd':>3}  served model / provider")
    for r in results:
        t = r["turns"]
        served = t[-1]["served_model"] if t else "-"
        provider = t[-1]["provider"] if t else "-"
        reasoning = sum(x["reasoning_tokens"] for x in t)
        rd = "y" if any(x["has_reasoning_details"] for x in t) else "n"
        print(f"{r['team']:<9} {r['outcome']:<12} {len(t):>5} {r['wall_s']:>7} {r['cost_usd']:>8.4f} {reasoning:>10} {rd:>3}  {served} / {provider}")
        if r["outcome"] not in ("pass",):
            print(f"          ↳ {r['detail'][:300]}")
    print(f"total cost ${sum(r['cost_usd'] for r in results):.4f} → {path.relative_to(ROOT)}")
    return path
