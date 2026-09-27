"""OpenRouter chat client for GM sessions.

Every request is pinned to the team's first-party providers (no fallbacks, no
`openrouter/auto`), sends no sampling parameters (several flagship hosts reject
`temperature`), and returns usage, cost, latency, and the served model/provider
so each call can be audited. Assistant messages are passed back unmodified,
including `reasoning_details`, so multi-turn tool loops keep their reasoning.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from gmbench.config import TeamSpec, require_env

BASE_URL = "https://openrouter.ai/api/v1"
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class OpenRouterError(Exception):
    """Base class for client errors."""


class Transient(OpenRouterError):
    """Retryable failure (rate limit, provider outage, timeout). Never charged to a GM."""


class BadRequest(OpenRouterError):
    """Non-retryable request error (invalid params, unroutable pin, auth)."""


class InsufficientCredits(OpenRouterError):
    """The league key is out of credit; stop the round for everyone."""


@dataclass
class LLMResult:
    message: dict[str, Any]
    finish_reason: str | None
    model: str | None
    provider: str | None
    usage: dict[str, Any]
    cost_usd: float | None
    latency_s: float
    gen_id: str | None
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []

    @property
    def content(self) -> str:
        return self.message.get("content") or ""

    @property
    def reasoning_tokens(self) -> int:
        details = self.usage.get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)

    @property
    def cached_tokens(self) -> int:
        details = self.usage.get("prompt_tokens_details") or {}
        return int(details.get("cached_tokens") or 0)


def build_body(
    spec: TeamSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    effort: str | None,
    max_tokens: int,
    session_id: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": spec.model,
        "messages": _apply_block_cache(messages) if spec.cache == "block" else messages,
        "max_tokens": max_tokens,
        "provider": {
            "order": list(spec.providers),
            "only": list(spec.providers),
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "usage": {"include": True},
    }
    if tools:
        body["tools"] = tools
    if effort:
        body["reasoning"] = {"effort": effort}
    if session_id:
        body["session_id"] = session_id
    if spec.cache == "top":
        body["cache_control"] = {"type": "ephemeral"}
    for param in spec.omit:
        body.pop(param, None)
    return body


def _apply_block_cache(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the system prompt as a cache breakpoint for vendors that need per-block cache_control."""
    out = []
    for i, m in enumerate(messages):
        if i == 0 and m.get("role") == "system" and isinstance(m.get("content"), str):
            m = {
                "role": "system",
                "content": [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}],
            }
        out.append(m)
    return out


def assistant_message_for_replay(message: dict[str, Any]) -> dict[str, Any]:
    """The assistant turn as it must be sent back: content, tool calls, and untouched reasoning_details."""
    replay: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        replay["tool_calls"] = message["tool_calls"]
    if message.get("reasoning_details"):
        replay["reasoning_details"] = message["reasoning_details"]
    return replay


class OpenRouterClient:
    def __init__(self, api_key: str | None = None, *, timeout_s: float = 600, retries: int = 3) -> None:
        self._key = api_key or require_env("OPENROUTER_API_KEY")
        self._timeout_s = timeout_s
        self._retries = retries
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(timeout_s, connect=30),
            headers={
                "Authorization": f"Bearer {self._key}",
                "HTTP-Referer": "https://gamedaysuits.ca",
                "X-Title": "GDS AI GM League (GM-Bench)",
            },
        )

    def close(self) -> None:
        self._http.close()

    def chat(
        self,
        spec: TeamSpec,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = "medium",
        max_tokens: int = 32000,
        session_id: str | None = None,
    ) -> LLMResult:
        if spec.is_bot:
            raise BadRequest(f"team {spec.id} is the control bot and has no model")
        body = build_body(spec, messages, tools, effort=effort, max_tokens=max_tokens, session_id=session_id)
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 2):
            started = time.monotonic()
            try:
                resp = self._http.post("/chat/completions", json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = Transient(f"{type(exc).__name__}: {exc}")
            else:
                latency = time.monotonic() - started
                try:
                    return self._parse(resp, latency, attempt)
                except Transient as exc:
                    last_error = exc
            if attempt <= self._retries:
                time.sleep(min(60, 4 * 2 ** (attempt - 1)))
        assert last_error is not None
        raise last_error

    def _parse(self, resp: httpx.Response, latency: float, attempt: int) -> LLMResult:
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {"error": {"code": resp.status_code, "message": resp.text[:500]}}

        error = data.get("error") if isinstance(data, dict) else None
        status = resp.status_code
        if error and status == 200:
            status = int(error.get("code") or 502)
        if status == 402:
            raise InsufficientCredits(_error_text(error))
        if status in TRANSIENT_STATUS:
            raise Transient(f"HTTP {status}: {_error_text(error)}")
        if status >= 400:
            raise BadRequest(f"HTTP {status}: {_error_text(error)}")

        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        choice_error = choice.get("error")
        if choice_error:
            raise Transient(f"choice error: {_error_text(choice_error)}")
        return LLMResult(
            message=choice.get("message") or {},
            finish_reason=choice.get("finish_reason"),
            model=data.get("model"),
            provider=data.get("provider"),
            usage=usage,
            cost_usd=usage.get("cost"),
            latency_s=round(latency, 3),
            gen_id=data.get("id"),
            attempts=attempt,
            raw=data,
        )


def _error_text(error: Any) -> str:
    if not error:
        return "unknown error"
    if isinstance(error, dict):
        msg = str(error.get("message") or "")
        meta = error.get("metadata") or {}
        raw = meta.get("raw") if isinstance(meta, dict) else None
        provider = meta.get("provider_name") if isinstance(meta, dict) else None
        return " | ".join(p for p in (msg, provider, str(raw)[:300] if raw else None) if p)
    return str(error)[:500]
