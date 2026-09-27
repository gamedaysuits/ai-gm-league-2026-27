"""The suits are real: every GM wears a Game Day Suit built from a real fabric. Suit facts for the on-screen suit
card (the first time a GM speaks) and the end-card CTA, plus fabric swatch thumbnails from the GDS site's public
folder (same lookup as site/scripts/sync_data.py: $GDS_SWATCH_DIR, then the site repo's public/)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from PIL import Image

from .paths import SHOW_GEN

SWATCH_DIRS = [Path.home() / "local projects" / "Game Day Suits Website" / "game-day-suits" / "public"]


def _cut(suit: dict) -> str:
    jk = suit.get("jacket") or {}
    parts = []
    if suit.get("vest") or suit.get("waistcoat"):
        parts.append("three-piece")
    bl = str(jk.get("button_layout") or "")
    if bl.startswith("double"):
        parts.append("double-breasted")
    elif bl:
        m = re.match(r"(\d)-button", bl)
        roll = re.match(r"(\d)-roll-(\d)", bl)
        if roll:
            parts.append(f"{roll.group(1)}-roll-{roll.group(2)}")
        elif m:
            parts.append({"1": "one-button", "2": "two-button", "3": "three-button"}.get(m.group(1), bl))
    if jk.get("lapel_style"):
        parts.append(f"{jk['lapel_style']} lapels")
    return ", ".join(parts)


def suit_info(league, tid: str) -> dict | None:
    t = league.teams.get(tid)
    if not t or not t.has_persona:
        return None
    suit = t.p("suit") or {}
    fid = suit.get("fabric_id") or ""
    fab = league.fabrics.get(fid, {})
    if not fab:
        return None
    nick = re.findall(r"[\"'“‘]([^\"'“”‘’]+)[\"'”’]", t.gm_name)
    who = nick[0] if nick else (t.short_gm.split()[0] if t.short_gm else t.gm_name)
    who = re.sub(r"^The\s+", "", who)
    summary = str(fab.get("summary") or fab.get("pattern_desc") or "").strip()
    return {"team": tid, "who": who, "possessive": f"{who}'s", "fabric_id": fid, "fabric": fab.get("name") or fid,
            "summary": summary[:1].upper() + summary[1:] if summary else "", "cut": _cut(suit),
            "color": fab.get("color_hex") or "#2a2f45", "swatch": fab.get("swatch")}


def swatch_url(info: dict | None, size: int = 160) -> str | None:
    """Copy (downscaled) the fabric photo into show/assets/gen/swatches/ and return its asset URL."""
    if not info or not info.get("swatch"):
        return None
    out = SHOW_GEN / "swatches" / f"{info['fabric_id']}.jpg"
    if out.exists():
        return f"gen/swatches/{out.name}"
    dirs = ([Path(os.environ["GDS_SWATCH_DIR"])] if os.environ.get("GDS_SWATCH_DIR") else []) + SWATCH_DIRS
    for d in dirs:
        src = d / str(info["swatch"]).lstrip("/")
        if src.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            im = Image.open(src).convert("RGB")
            side = min(im.size)
            im = im.crop(((im.width - side) // 2, (im.height - side) // 2, (im.width + side) // 2,
                          (im.height + side) // 2)).resize((size, size), Image.LANCZOS)
            im.save(out, quality=88)
            return f"gen/swatches/{out.name}"
    return None


def cta_lines(info: dict) -> tuple[str, str, str]:
    """The end-card CTA: what the suit is, plainly, and where to build one."""
    what = " · ".join(x for x in (info["fabric"], info["summary"].lower() if info["summary"] else "", info["cut"]) if x)
    return ("Every suit in the league is a real Game Day Suit.", f"{info['possessive']}: {what}",
            "Build yours at gamedaysuits.ca")
