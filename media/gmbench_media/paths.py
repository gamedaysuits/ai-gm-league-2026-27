"""Filesystem layout for the show pipeline.

Everything this package writes lives under ``media/``:
  media/out/<run>/     rundown, TTS manifest, mix, cues, envelopes, tracks, renders (gitignored)
  media/cache/         content-addressed TTS + avatar caches (gitignored)
  media/show/          the HyperFrames project (index.html / compositions / render are generated)
Inputs are read-only: runs/<run>/ledger, runs/<run>/exports, league.yaml, data/fabrics.json.
"""

from __future__ import annotations

from pathlib import Path

MEDIA = Path(__file__).resolve().parents[1]
ROOT = MEDIA.parent
CACHE = MEDIA / "cache"
OUT = MEDIA / "out"
SHOW = MEDIA / "show"
SHOW_ASSETS = SHOW / "assets"
SHOW_GEN = SHOW_ASSETS / "gen"  # per-build copies of avatar frames etc. (gitignored)
VOICES_JSON = MEDIA / "voices" / "voices.json"  # production voice book (written by media/voices/design.py; read-only here)
DEV_VOICES_JSON = MEDIA / "voices_dev.json"  # zero-cost dev voices (macOS `say`), generated on first run
AVATARS_JSON = MEDIA / "avatars.json"  # optional per-speaker overrides (4 or 6 frame paths)
AVATAR_MANIFEST = MEDIA / "assets" / "avatars" / "manifest.json"  # production frame sets (media/assets/avatars/<id>/)
SHOW_YAML = MEDIA / "show.yaml"
ENV_FILE = ROOT / ".env"


FIXTURES = CACHE / "fixtures"  # dev-only synthetic runs ("fixture-<name>"): layout tests without a live ledger


def run_dir(run: str) -> Path:
    if run.startswith("fixture-"):
        return FIXTURES / run
    return ROOT / "runs" / run


def out_dir(run: str, *parts: str) -> Path:
    d = OUT.joinpath(run, *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir(*parts: str) -> Path:
    d = CACHE.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def rel(p: Path) -> str:
    """Path relative to the repo root, for logs and manifests (never absolute home paths)."""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)
