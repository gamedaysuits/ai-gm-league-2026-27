"""Shared JSON-over-HTTP helper for snapshot sources (NHL, ESPN).

Uses httpx's default User-Agent on purpose: the NHL API rejects Python-urllib's.
Every payload is returned with the sha256 of its raw bytes so a snapshot can
record exactly what it was built from.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

TIMEOUT_S = 30.0
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524})
MAX_BACKOFF_S = 30.0

CLIENT = httpx.Client(timeout=TIMEOUT_S, follow_redirects=True, headers={"Accept": "application/json"})


class FetchError(RuntimeError):
    """A source could not be fetched (non-retryable status, or retries exhausted)."""


def get_json(url: str, *, retries: int = 3) -> tuple[Any, str]:
    """GET `url` and parse JSON. Returns (data, sha256 hex of the raw body).

    Retries up to `retries` times with exponential backoff on 429/5xx, timeouts,
    connection errors and unparseable bodies; honours Retry-After on 429/503.
    """
    attempt = 0
    while True:
        retry_after: float | None = None
        try:
            resp = CLIENT.get(url)
        except httpx.TransportError as exc:  # timeouts, connection resets, DNS
            err: Exception = exc
        else:
            if resp.is_success:
                raw = resp.content
                try:
                    return json.loads(raw), hashlib.sha256(raw).hexdigest()
                except ValueError as exc:  # truncated or non-JSON body
                    err = exc
            elif resp.status_code in RETRY_STATUS:
                err = FetchError(f"HTTP {resp.status_code}")
                retry_after = _retry_after(resp)
            else:
                raise FetchError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]!r}")
        if attempt >= retries:
            raise FetchError(f"GET {url} failed after {attempt + 1} attempts: {err}") from err
        delay = retry_after if retry_after is not None else min(MAX_BACKOFF_S, 2.0**attempt) + random.uniform(0, 0.25)
        log.warning("GET %s: %s; retry %d/%d in %.1fs", url, err, attempt + 1, retries, delay)
        time.sleep(delay)
        attempt += 1


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(MAX_BACKOFF_S * 2, max(0.0, float(value)))
    except ValueError:  # HTTP-date form; fall back to exponential backoff
        return None


def combined_sha256(hashes: list[str]) -> str:
    """One digest for a source fetched in several pages (order-sensitive)."""
    if len(hashes) == 1:
        return hashes[0]
    return hashlib.sha256("".join(hashes).encode()).hexdigest()
