"""League setup: the genesis events every run starts from, and loading a run's snapshot."""
from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from gmbench.agent.prompts import prompt_hash
from gmbench.config import ROOT, LeagueConfig
from gmbench.data.models import Snapshot
from gmbench.draft.order import derive_order
from gmbench.ledger import Ledger
from gmbench.state import replay

SYNTHETIC_PREFIX = "synthetic:"


def run_dir(name: str) -> Path:
    return ROOT / "runs" / name


def ledger_for(name: str) -> Ledger:
    return Ledger(run_dir(name) / "ledger" / "league.jsonl")


def code_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "uncommitted"


def load_run_snapshot(ref: str) -> Snapshot:
    if ref.startswith(SYNTHETIC_PREFIX):
        from gmbench.data.synthetic import synthetic_snapshot

        return synthetic_snapshot(int(ref[len(SYNTHETIC_PREFIX):]))
    return Snapshot.model_validate_json((ROOT / ref).read_text())


def init_league(ledger: Ledger, cfg: LeagueConfig, snapshot_ref: str) -> None:
    if any(True for _ in ledger.events()):
        raise RuntimeError(f"{ledger.path} already has events; refusing to re-initialise")
    snap = load_run_snapshot(snapshot_ref)
    ledger.append("LEAGUE_CREATED", "league", {
        "league": cfg.raw["league"],
        "rules": cfg.rules,
        "harness": cfg.harness,
        "teams": [{"id": t.id, "lab": t.lab, "display": t.display, "model": t.model, "providers": list(t.providers),
                   "cache": t.cache, "omit": list(t.omit)} for t in cfg.teams],
        "config_sha256": hashlib.sha256((ROOT / "league.yaml").read_bytes()).hexdigest(),
        "prompts_sha256": prompt_hash(),
        "code_version": code_version(),
    })
    counts = Counter(p.group for p in snap.players.values())
    sha = (hashlib.sha256((ROOT / snapshot_ref).read_bytes()).hexdigest()
           if not snapshot_ref.startswith(SYNTHETIC_PREFIX) else "synthetic")
    ledger.append("SNAPSHOT_TAKEN", "league", {
        "kind": snap.kind, "ref": snapshot_ref, "sha256": sha, "as_of_date": snap.as_of_date,
        "counts": {"players": len(snap.players), **dict(sorted(counts.items()))},
    })


def set_draft_order(ledger: Ledger, cfg: LeagueConfig, beacon: dict[str, Any], *, method: str) -> list[str]:
    state = replay(ledger.events())
    if state.order:
        raise RuntimeError("draft order already set")
    team_ids = [t.id for t in cfg.teams]
    order = derive_order(beacon["randomness"], team_ids)
    ledger.append("DRAFT_ORDER_SET", "league", {
        "method": method, "round": beacon.get("round"), "randomness": beacon["randomness"],
        "signature": beacon.get("signature"), "rule": "teams sorted by sha256(bytes.fromhex(randomness) + team_id)",
        "order": order,
    })
    return order
