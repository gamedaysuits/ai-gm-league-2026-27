"""Publicly verifiable draft order from a pre-committed drand randomness round.

We announce a future drand "quicknet" round before it exists. Once published,
anyone can fetch that round's randomness and recompute the order:
teams sorted by sha256(randomness_bytes || team_id).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import httpx

QUICKNET_CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
QUICKNET_GENESIS = 1692803367
QUICKNET_PERIOD_S = 3
RELAYS = ("https://api.drand.sh", "https://drand.cloudflare.com")


def round_at(when: datetime) -> int:
    ts = int(when.astimezone(timezone.utc).timestamp())
    return (ts - QUICKNET_GENESIS) // QUICKNET_PERIOD_S + 1


def round_time(round_no: int) -> datetime:
    return datetime.fromtimestamp(QUICKNET_GENESIS + (round_no - 1) * QUICKNET_PERIOD_S, tz=timezone.utc)


def fetch_beacon(round_no: int) -> dict:
    """Fetch the round from two independent relays and require they agree."""
    seen = []
    for relay in RELAYS:
        resp = httpx.get(f"{relay}/{QUICKNET_CHAIN}/public/{round_no}", timeout=20)
        resp.raise_for_status()
        seen.append(resp.json())
    if len({(b["round"], b["randomness"], b["signature"]) for b in seen}) != 1:
        raise RuntimeError(f"drand relays disagree for round {round_no}")
    return seen[0]


def derive_order(randomness_hex: str, team_ids: list[str]) -> list[str]:
    seed = bytes.fromhex(randomness_hex)
    return sorted(team_ids, key=lambda t: hashlib.sha256(seed + t.encode()).hexdigest())
