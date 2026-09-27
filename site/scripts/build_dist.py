#!/usr/bin/env python3
"""Package the static site into site/dist/ (the folder you deploy).

    python3 site/scripts/build_dist.py

Copies every page, assets/ and data/ into a fresh site/dist/, skipping scripts/
and dotfiles, stamps asset URLs with a content hash (?v=...) so a deploy never
mixes new pages with cached old scripts, then adds `.nojekyll` (GitHub Pages)
and `_headers` (Cloudflare Pages: data files are always revalidated so the live
draft board stays fresh). sync_data.py runs this automatically unless you pass
--no-dist.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parents[1]
SKIP = {"dist", "scripts", "__pycache__"}

HEADERS = """\
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin

/data/*
  Cache-Control: no-cache

/assets/*
  Cache-Control: public, max-age=31536000, immutable
"""


def asset_version(site: Path) -> str:
    h = hashlib.sha256()
    for p in sorted((site / "assets").rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            h.update(p.relative_to(site).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


def stamp(dist: Path, version: str) -> None:
    """Append ?v=<version> to asset URLs in pages and to relative module imports."""
    for page in dist.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        new = re.sub(r'((?:href|src)="assets/[^"?#]+\.(?:css|js))"', rf'\1?v={version}"', text)
        if new != text:
            page.write_text(new, encoding="utf-8")
    for js in (dist / "assets" / "js").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        new = re.sub(r"""((?:from|import)\s+)(['"])(\./[\w-]+\.js)\2""", rf"\1\2\3?v={version}\2", text)
        if new != text:
            js.write_text(new, encoding="utf-8")


def build(site: Path = SITE) -> Path:
    dist = site / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir()
    ignore = shutil.ignore_patterns(".*", "__pycache__", "*.pyc")
    for item in sorted(site.iterdir()):
        if item.name in SKIP or item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.copytree(item, dist / item.name, ignore=ignore)
        else:
            shutil.copy2(item, dist / item.name)
    if (dist / "assets").is_dir():
        stamp(dist, asset_version(site))
    (dist / ".nojekyll").write_text("")
    (dist / "_headers").write_text(HEADERS)
    return dist


if __name__ == "__main__":
    out = build()
    files = sum(1 for p in out.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"built {out} ({files} files, {size / 1024:.0f} KB)")
    sys.exit(0)
