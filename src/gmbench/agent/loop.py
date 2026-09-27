"""The agent session loop shared by every phase (draft, weekly, Media Day, broadcast lines)."""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from gmbench.agent.prompts import NUDGE_BUDGET, NUDGE_LENGTH, NUDGE_PICK
from gmbench.agent.tools import Action, ToolContext, ToolOutcome, parse_arguments
from gmbench.agent.untrusted import looks_like_injection
from gmbench.config import TeamSpec
from gmbench.llm.openrouter import (
    BadRequest,
    InsufficientCredits,
    LLMResult,
    Transient,
    assistant_message_for_replay,
)


class ChatClient(Protocol):
    def chat(self, spec: TeamSpec, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
             effort: str | None = ..., max_tokens: int = ..., session_id: str | None = ...) -> LLMResult: ...


Dispatcher = Callable[[str, dict[str, Any], ToolContext], ToolOutcome]

# Memory upkeep never counts against the research budget: we want GMs to keep notes and queues.
HOUSEKEEPING = frozenset({"notes_read", "notes_write", "update_draft_queue"})


@dataclass
class Budget:
    max_tool_calls: int = 12
    max_nudges: int = 2
    effort: str | None = "medium"
    max_tokens: int = 32000


@dataclass
class SessionResult:
    session_id: str
    team_id: str
    outcome: str  # ok | no_action | provider_outage | bad_request | out_of_credit
    action: Action | None = None
    calls: int = 0
    tool_calls: int = 0
    invalid_tool_calls: int = 0  # bad arguments, unknown tools, illegal actions
    budget_refusals: int = 0  # research calls refused because the budget was spent
    nudges: int = 0
    cost_usd: float = 0.0
    model_latency_s: float = 0.0
    wall_s: float = 0.0
    tokens: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0})
    served_models: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    tools_by_name: dict[str, int] = field(default_factory=dict)
    violations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    transcript_path: str | None = None
    transcript_sha256: str | None = None

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k not in ("action",)}


def run_session(
    client: ChatClient,
    spec: TeamSpec,
    *,
    session_id: str,
    system: str,
    user: str,
    tools: list[dict[str, Any]],
    ctx: ToolContext,
    dispatch: Dispatcher,
    terminal_tools: set[str],
    budget: Budget,
    transcript_dir: Path,
    on_action: Callable[[Action], None] | None = None,
    nudge_text: str = NUDGE_PICK,
) -> SessionResult:
    result = SessionResult(session_id=session_id, team_id=spec.id, outcome="no_action")
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    telemetry: list[dict[str, Any]] = []
    started = time.monotonic()
    research_used = 0
    max_calls = budget.max_tool_calls + budget.max_nudges + 3

    while result.calls < max_calls:
        try:
            res = client.chat(spec, messages, tools, effort=budget.effort, max_tokens=budget.max_tokens,
                              session_id=f"{session_id}")
        except Transient as exc:
            result.outcome, result.error = "provider_outage", str(exc)
            break
        except InsufficientCredits as exc:
            result.outcome, result.error = "out_of_credit", str(exc)
            break
        except BadRequest as exc:
            result.outcome, result.error = "bad_request", str(exc)
            break

        result.calls += 1
        _account(result, res)
        telemetry.append(_telemetry(res))
        messages.append(assistant_message_for_replay(res.message))

        if not res.tool_calls:
            if result.nudges >= budget.max_nudges:
                result.outcome = "no_action"
                break
            result.nudges += 1
            messages.append({"role": "user", "content": NUDGE_LENGTH if res.finish_reason == "length" else nudge_text})
            continue

        done = False
        for call in res.tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            result.tool_calls += 1
            result.tools_by_name[name] = result.tools_by_name.get(name, 0) + 1
            if done:
                outcome = ToolOutcome("Ignored: your turn already ended.", ok=False)
            else:
                try:
                    args = parse_arguments(fn.get("arguments"))
                except (ValueError, json.JSONDecodeError):
                    outcome = ToolOutcome("ERROR: tool arguments were not valid JSON.", ok=False)
                    result.invalid_tool_calls += 1
                else:
                    counted = name not in terminal_tools and name not in HOUSEKEEPING
                    if counted and research_used >= budget.max_tool_calls:
                        outcome = ToolOutcome("ERROR: " + NUDGE_BUDGET, ok=False)
                        result.budget_refusals += 1
                    else:
                        if counted:
                            research_used += 1
                        outcome = dispatch(name, args, ctx)
                        if not outcome.ok:
                            result.invalid_tool_calls += 1
            if outcome.action is not None and outcome.ok:
                if outcome.terminal:
                    result.action, result.outcome, done = outcome.action, "ok", True
                    _check_public_text(result, outcome.action)
                elif on_action is not None:
                    on_action(outcome.action)
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": outcome.text})
        if done:
            break

    result.wall_s = round(time.monotonic() - started, 2)
    _save_transcript(result, spec, messages, telemetry, transcript_dir)
    return result


def _account(result: SessionResult, res: LLMResult) -> None:
    result.cost_usd = round(result.cost_usd + (res.cost_usd or 0.0), 6)
    result.model_latency_s = round(result.model_latency_s + res.latency_s, 3)
    result.tokens["prompt"] += int(res.usage.get("prompt_tokens") or 0)
    result.tokens["completion"] += int(res.usage.get("completion_tokens") or 0)
    result.tokens["reasoning"] += res.reasoning_tokens
    result.tokens["cached"] += res.cached_tokens
    if res.model and res.model not in result.served_models:
        result.served_models.append(res.model)
    if res.provider and res.provider not in result.providers:
        result.providers.append(res.provider)


def _telemetry(res: LLMResult) -> dict[str, Any]:
    return {"gen_id": res.gen_id, "model": res.model, "provider": res.provider, "finish_reason": res.finish_reason,
            "latency_s": res.latency_s, "attempts": res.attempts, "usage": res.usage, "cost_usd": res.cost_usd,
            "tool_calls": [c.get("function", {}).get("name") for c in res.tool_calls]}


def _check_public_text(result: SessionResult, action: Action) -> None:
    for key in ("public_rationale", "line", "message"):
        text = action.args.get(key)
        if isinstance(text, str) and looks_like_injection(text):
            result.violations.append({"kind": "suspected_injection", "field": key, "text": text[:280]})


def _save_transcript(result: SessionResult, spec: TeamSpec, messages: list[dict[str, Any]],
                     telemetry: list[dict[str, Any]], transcript_dir: Path) -> None:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    doc = {"session_id": result.session_id, "team": spec.id, "model": spec.model, "providers": list(spec.providers),
           "outcome": result.outcome, "messages": messages, "telemetry": telemetry}
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()
    safe = result.session_id.replace(":", "_").replace("/", "_")
    path = transcript_dir / f"{safe}.json.gz"
    with gzip.open(path, "wb", compresslevel=6) as fh:
        fh.write(raw)
    result.transcript_path = str(path)
    result.transcript_sha256 = hashlib.sha256(raw).hexdigest()
