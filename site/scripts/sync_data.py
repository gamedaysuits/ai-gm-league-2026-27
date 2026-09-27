#!/usr/bin/env python3
"""Copy a league run's public exports into the static site, then rebuild site/dist/.

Usage (from the repo root):
    python3 site/scripts/sync_data.py show-dev          # development data
    python3 site/scripts/sync_data.py live --ledger     # also publish the ledger for in-browser verification
    python3 -m http.server -d site 8765                 # preview at http://localhost:8765

Sources -> destinations:
    runs/<run>/exports/*.json          -> site/data/          (draft, personas, grades, standings, scorecard, ...)
    runs/<run>/exports/avatars/*       -> site/data/avatars/  (<team>.png|.webp|.jpg, or --avatars DIR)
    exports/history/*.json             -> site/data/
    exports/history/*.md               -> site/data/fragments/<name>.html   (fallback render)
    data/fabrics.json                  -> site/data/fabrics.json
    fabric photos (--swatches DIR)     -> site/data/swatches/<fabric_id>.jpg (downscaled)
    runs/<run>/ledger/league.jsonl     -> site/data/ledger.jsonl            (only with --ledger)
    RULES.md, DRAND_COMMITMENT.md      -> rendered into site/methodology.html
    site/data/manifest.json            <- index of what is published; pages only request listed files

Files dropped into site/data/ by hand (episodes.json, avatars, ...) are kept and indexed.
Files an earlier sync copied that no longer exist at the source are removed, so an export a run
doesn't have yet (grades.json before the post-draft session) never leaks in from another run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dist import build as build_dist  # noqa: E402
from mdlite import md_to_html  # noqa: E402

SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
DATA = SITE / "data"
IMAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg")
DEFAULT_SWATCH_DIRS = [
    Path.home() / "local projects" / "Game Day Suits Website" / "game-day-suits" / "public",
]
GENESIS = "0" * 64


def log(msg: str) -> None:
    print(f"  {msg}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def same_file(a: Path, b: Path) -> bool:
    if not b.exists() or a.stat().st_size != b.stat().st_size:
        return False
    return hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()


def write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


class Sync:
    def __init__(self) -> None:
        self.written: set[str] = set()
        self.changed = 0

    def copy(self, src: Path, rel: str) -> None:
        dst = DATA / rel
        if not same_file(src, dst):
            write_atomic(dst, src.read_bytes())
            self.changed += 1
        self.written.add(rel)

    def text(self, rel: str, content: str) -> None:
        dst = DATA / rel
        data = content.encode("utf-8")
        if not dst.exists() or dst.read_bytes() != data:
            write_atomic(dst, data)
            self.changed += 1
        self.written.add(rel)

    def blob(self, rel: str, data: bytes) -> None:
        dst = DATA / rel
        if not dst.exists() or dst.stat().st_size != len(data) or dst.read_bytes() != data:
            write_atomic(dst, data)
            self.changed += 1
        self.written.add(rel)

    def keep(self, rel: str) -> None:
        if (DATA / rel).exists():
            self.written.add(rel)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- images

def _pillow():
    try:
        from PIL import Image  # type: ignore
        return Image
    except Exception:
        return None


def resize_image(src: Path, dst: Path, *, max_px: int, fmt: str, quality: int) -> bool:
    """Downscale src into dst (fmt: JPEG or WEBP). Returns False if no resizer is available."""
    Image = _pillow()
    if Image is not None:
        with Image.open(src) as im:
            im.thumbnail((max_px, max_px))
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            if fmt == "WEBP":
                im.save(tmp, fmt, quality=quality, method=6)
            else:
                im.save(tmp, fmt, quality=quality, optimize=True, progressive=True)
            os.replace(tmp, dst)
        return True
    if fmt == "JPEG" and shutil.which("sips"):
        dst.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["sips", "-Z", str(max_px), "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
                              str(src), "--out", str(dst)], capture_output=True)
        return res.returncode == 0
    return False


def sync_avatars(s: Sync, sources: list[Path]) -> None:
    for folder in sources:
        if not folder or not folder.is_dir():
            continue
        for src in sorted(folder.iterdir()):
            if src.suffix.lower() not in IMAGE_EXTS or not re.fullmatch(r"[a-z0-9_-]+", src.stem):
                continue
            rel_webp = f"avatars/{src.stem}.webp"
            dst = DATA / rel_webp
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                s.written.add(rel_webp)
                continue
            if _pillow() is not None and resize_image(src, dst, max_px=640, fmt="WEBP", quality=82):
                s.changed += 1
                s.written.add(rel_webp)
            else:
                s.copy(src, f"avatars/{src.stem}{src.suffix.lower()}")
        log(f"avatars from {folder}")


def sync_swatches(s: Sync, fabrics: dict[str, dict], used: set[str], swatch_dir: Path | None) -> None:
    for fid in sorted(used):
        rel = f"swatches/{fid}.jpg"
        fab = fabrics.get(fid)
        src = None
        if swatch_dir and fab and fab.get("swatch"):
            cand = swatch_dir / str(fab["swatch"]).lstrip("/")
            if cand.is_file():
                src = cand
        if src is None:
            s.keep(rel)  # source unavailable on this machine: keep what we already have
            continue
        dst = DATA / rel
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            s.written.add(rel)
            continue
        if resize_image(src, dst, max_px=560, fmt="JPEG", quality=74):
            s.changed += 1
            s.written.add(rel)
        else:
            s.copy(src, rel)


# --------------------------------------------------------------------------- ledger

def complete_lines(path: Path) -> bytes:
    """The ledger up to its last newline: a line the league is still appending is left for the next sync."""
    data = path.read_bytes() if path.is_file() else b""
    return data[: data.rfind(b"\n") + 1]


def ledger_head(data: bytes) -> dict | None:
    lines = [ln for ln in data.split(b"\n") if ln.strip()]
    if not lines:
        return None
    try:
        ev = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return {"seq": ev.get("seq"), "hash": ev.get("hash"), "ts": ev.get("ts")}


def verify_ledger(data: bytes) -> tuple[int, str]:
    """Same check as gmbench.ledger.Ledger.verify: sequence, links and hashes."""
    prev, count = GENESIS, 0
    for line in data.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        count += 1
        ev = json.loads(line)
        body = {k: v for k, v in ev.items() if k != "hash"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if ev.get("seq") != count or ev.get("prev_hash") != prev or digest != ev.get("hash"):
            raise ValueError(f"ledger chain broken at seq {ev.get('seq')}")
        prev = ev["hash"]
    return count, prev


# --------------------------------------------------------------------------- methodology

def inject(page: Path, marker: str, fragment: str) -> bool:
    text = page.read_text(encoding="utf-8")
    pattern = re.compile(rf"(<!-- sync:{marker}:start -->)(.*?)(<!-- sync:{marker}:end -->)", re.S)
    if not pattern.search(text):
        print(f"warning: markers for '{marker}' not found in {page.name}", file=sys.stderr)
        return False
    new = pattern.sub(lambda m: f"{m.group(1)}\n{fragment}\n{m.group(3)}", text)
    if new != text:
        page.write_text(new, encoding="utf-8")
        return True
    return False


# --------------------------------------------------------------------------- main

def draft_status(draft: dict | None) -> dict | None:
    if not isinstance(draft, dict):
        return None
    order = draft.get("order") or []
    rounds = int(draft.get("rounds") or 14)
    picks = len(draft.get("picks") or [])
    total = len(order) * rounds if order else None
    return {
        "order_set": bool(order), "picks": picks, "total": total,
        "complete": bool(total) and picks >= total,
        "ledger_head_seq": draft.get("ledger_head_seq"), "generated_at": draft.get("generated_at"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="run name under runs/ (e.g. show-dev, live)")
    ap.add_argument("--root", type=Path, default=ROOT, help="league repo root (default: parent of site/)")
    ap.add_argument("--avatars", type=Path, help="extra folder of <team>.png|.webp|.jpg portraits")
    ap.add_argument("--swatches", type=Path, help="folder holding fabric photos at the fabrics.json 'swatch' paths "
                                                  "(the Game Day Suits website's public/ dir); $GDS_SWATCH_DIR also works")
    ap.add_argument("--no-swatches", action="store_true", help="skip fabric photos (CSS swatches are used instead)")
    ap.add_argument("--ledger", action="store_true", help="publish the run's ledger for in-browser verification")
    ap.add_argument("--no-dist", action="store_true", help="don't rebuild site/dist/")
    args = ap.parse_args()

    root = args.root.resolve()
    run_dir = root / "runs" / args.run
    exports = run_dir / "exports"
    if not exports.is_dir():
        print(f"error: no exports folder at {exports}", file=sys.stderr)
        return 1

    DATA.mkdir(parents=True, exist_ok=True)
    old = read_json(DATA / "manifest.json") or {}
    s = Sync()
    print(f"syncing run '{args.run}' -> {DATA}")

    def current(name: str):
        """This run's version of an export (or a hand-placed file), never a stale copy from another run."""
        if (exports / name).is_file():
            return read_json(exports / name)
        if name not in old.get("managed", []) and (DATA / name).is_file():
            return read_json(DATA / name)
        return None

    # 1. league exports (validated: never publish a half-written file)
    for src in sorted(exports.glob("*.json")):
        if read_json(src) is None:
            print(f"warning: skipping unreadable {src}", file=sys.stderr)
            continue
        s.copy(src, src.name)
    log(f"exports: {', '.join(sorted(p.name for p in exports.glob('*.json'))) or '(none)'}")

    # 2. history pools
    hist = root / "exports" / "history"
    if hist.is_dir():
        for src in sorted(hist.glob("*.json")):
            if read_json(src) is not None:
                s.copy(src, src.name)
        for src in sorted(hist.glob("*.md")):
            s.text(f"fragments/{src.stem}.html", md_to_html(src.read_text(encoding="utf-8"), shift=1))
        log(f"history: {', '.join(sorted(p.name for p in hist.iterdir() if p.suffix in ('.json', '.md')))}")

    # 3. fabrics + swatches
    fabrics_src = root / "data" / "fabrics.json"
    fabrics_list = read_json(fabrics_src) if fabrics_src.is_file() else None
    if fabrics_list is not None:
        s.copy(fabrics_src, "fabrics.json")
    fabrics = {f["id"]: f for f in (fabrics_list or []) if isinstance(f, dict) and f.get("id")}
    personas = current("personas.json") or {}
    draft = current("draft.json")
    used: set[str] = set()
    for t in (personas.get("teams") or []) + ((draft or {}).get("teams") or []):
        fid = ((t.get("persona") or {}).get("suit") or {}).get("fabric_id")
        if fid:
            used.add(fid)
    swatch_dir = None
    if not args.no_swatches:
        for cand in [args.swatches, Path(os.environ["GDS_SWATCH_DIR"]) if os.environ.get("GDS_SWATCH_DIR") else None,
                     *DEFAULT_SWATCH_DIRS]:
            if cand and cand.is_dir():
                swatch_dir = cand
                break
    if args.no_swatches:
        log("swatches: skipped (CSS swatches)")
    else:
        sync_swatches(s, fabrics, used, swatch_dir)
        log(f"swatches: {len(used)} fabrics" + (f" from {swatch_dir}" if swatch_dir else " (no photo source; kept existing, CSS for the rest)"))

    # 4. avatars
    sync_avatars(s, [p for p in (exports / "avatars", args.avatars) if p])

    # 5. ledger (head always indexed; the file itself only with --ledger)
    ledger_bytes = complete_lines(run_dir / "ledger" / "league.jsonl")
    head = ledger_head(ledger_bytes)
    ledger_info = None
    if args.ledger and ledger_bytes:
        try:
            count, head_hash = verify_ledger(ledger_bytes)
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"error: {exc}; keeping the previously published ledger", file=sys.stderr)
            s.keep("ledger.jsonl")
        else:
            s.blob("ledger.jsonl", ledger_bytes)
            ledger_info = {"path": "data/ledger.jsonl", "events": count, "head_hash": head_hash, "bytes": len(ledger_bytes)}
            log(f"ledger: {count} events verified, head {head_hash[:12]}...")

    # 6. remove files an earlier sync managed that are gone at the source
    for rel in old.get("managed", []):
        if rel not in s.written and ".." not in rel:
            target = DATA / rel
            if target.is_file():
                target.unlink()
                log(f"removed stale {rel}")

    for folder in ("avatars", "swatches", "fragments"):
        d = DATA / folder
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    # 7. methodology page: RULES.md + DRAND_COMMITMENT.md
    page = SITE / "methodology.html"
    if page.is_file():
        for marker, md in (("rules", root / "RULES.md"), ("drand", root / "DRAND_COMMITMENT.md")):
            if md.is_file() and inject(page, marker, md_to_html(md.read_text(encoding="utf-8"), shift=1)):
                log(f"methodology: refreshed {md.name}")

    # 8. manifest (indexes everything present, including hand-placed files)
    def listing(folder: str) -> dict[str, str]:
        out: dict[str, str] = {}
        base = DATA / folder
        if base.is_dir():
            for p in sorted(base.iterdir(), key=lambda p: (IMAGE_EXTS + (".html",)).index(p.suffix.lower())
                            if p.suffix.lower() in IMAGE_EXTS + (".html",) else 99):
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS + (".html",) and p.stem not in out:
                    out[p.stem] = f"data/{folder}/{p.name}"
        return out

    manifest = {
        "schema": 1,
        "generated_at": now_iso(),
        "run": args.run,
        "files": {p.name: {"bytes": p.stat().st_size} for p in sorted(DATA.glob("*.json")) if p.name != "manifest.json"},
        "avatars": listing("avatars"),
        "swatches": listing("swatches"),
        "fragments": listing("fragments"),
        "ledger": ledger_info or ({"path": "data/ledger.jsonl"} if (DATA / "ledger.jsonl").is_file() else None),
        "ledger_head": head,
        "draft": draft_status(draft),
        "managed": sorted(s.written),
    }
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"manifest: {len(manifest['files'])} data files, {len(manifest['avatars'])} avatars, "
        f"{len(manifest['swatches'])} swatches; {s.changed} file(s) changed")

    if not args.no_dist:
        dist = build_dist()
        log(f"dist: {dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
