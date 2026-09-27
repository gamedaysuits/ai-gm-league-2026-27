#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "httpx>=0.27",
#   "python-dotenv>=1.0",
#   "numpy>=1.26",
#   "praat-parselmouth>=0.4.3",
#   "resemblyzer>=0.1.4",
#   "setuptools<70",
#   "faster-whisper>=1.0",
# ]
# ///
"""Energy test: can each cast voice go BIG on a tag while its base stays natural?

Renders a tagged line per voice (default: "[shouting] <catchphrase>!" -- the GM's own words; host: its template
line) at the voice's recorded settings, and compares it with the plain part of the voice's anchor:
  pitch lift (semitones, shout vs plain median F0), effort lift (alpha ratio dB: 1-5 kHz vs 50 Hz-1 kHz energy),
  plus the shout's pitch variance. Renders are idempotent (never re-billed).

usage: uv run media/voices/energy_test.py [--teams ...] [--out media/voices/energy_test/shout] [--budget 1500]
       uv run media/voices/energy_test.py --spec spec.json --out <dir>   # custom {"renders": [{id, team, text}]}
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import design as D  # noqa: E402

HOST_SHOUT = "[shouting] AND WE ARE LIVE! LET'S GET TO WORK!"


def default_renders(book: dict, personas: Path | None, teams: list[str] | None) -> list[dict]:
    cards = {}
    if personas and personas.is_file():
        for t in json.loads(personas.read_text()).get("teams", []):
            if t.get("persona"):
                cards[t["team"]] = t["persona"]
    out = []
    for team, e in book.items():
        if teams and team not in teams:
            continue
        if team == "host":
            text = HOST_SHOUT
        else:
            cp = D.spoken((cards.get(team) or {}).get("catchphrase") or "") or e["gm_name"]
            text = f"[shouting] {D.exclaim(cp)}"
        out.append({"id": f"{team}_shout", "team": team, "text": text})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", type=Path, help="custom renders JSON (default: one shout line per cast voice)")
    ap.add_argument("--personas", type=Path, help="personas export for the catchphrases (default: from voices.json)")
    ap.add_argument("--teams", nargs="*")
    ap.add_argument("--out", type=Path, default=D.HERE / "energy_test" / "shout")
    ap.add_argument("--budget", type=int, default=1500)
    args = ap.parse_args()
    book = D.load_book()[D.BOOK_KEY]
    if args.spec:
        renders = json.loads(args.spec.read_text())["renders"]
    else:
        src = args.personas or next((D.ROOT / e["source_card"]["personas"] for e in book.values()
                                     if (e.get("source_card") or {}).get("personas")), None)
        renders = default_renders(book, src, args.teams)
    out = args.out if args.out.is_absolute() else Path.cwd() / args.out
    out.mkdir(parents=True, exist_ok=True)
    need = sum(len(r["text"]) for r in renders if not (out / f"{r['id']}.mp3").exists())
    print(f"{len(renders)} renders, {need} characters to spend")
    an = D.Analyzer("base")
    el = D.ElevenLabs(D.load_key(), args.budget) if need else None
    rows = []
    for r in renders:
        e = book[r["team"]]
        f = out / f"{r['id']}.mp3"
        if not f.exists():
            f.write_bytes(el.tts(r["team"], e["voice_id"], r["text"], e["settings"], D.ANCHOR_SEED, note=f"energy test {r['id']}"))
        m = an.plain_f0(f)
        base = e.get("measured") or {}
        f0p, ap_ = base.get("plain_f0_hz"), base.get("plain_alpha_db")
        row = {"id": r["id"], "team": r["team"], "voice_id": e["voice_id"], "source": e.get("source"),
               "stability": e["settings"]["stability"], "plain_f0_hz": f0p, "shout_f0_hz": m["f0_median_hz"],
               "pitch_lift_st": round(12 * math.log2(m["f0_median_hz"] / f0p), 2) if f0p and m["f0_median_hz"] else None,
               "plain_alpha_db": ap_, "shout_alpha_db": m["alpha_db"],
               "effort_lift_db": round(m["alpha_db"] - ap_, 2) if ap_ is not None and m["alpha_db"] is not None else None,
               "shout_f0_st_std": m["f0_st_std"], "file": D.rel(f), "text": r["text"]}
        rows.append(row)
    (out / "metrics.json").write_text(json.dumps({"chars_spent": el.spent if el else 0, "rows": rows}, indent=2) + "\n")
    with (out / "metrics.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{'team':9s} {'src':7s} {'stab':>4s} {'plainF0':>7s} {'shoutF0':>7s} {'+st':>5s} {'plainA':>6s} {'shoutA':>6s} {'+dB':>5s}")
    for x in sorted(rows, key=lambda x: x["plain_f0_hz"] or 0):
        print(f"{x['team']:9s} {x['source']:7s} {x['stability']:4.1f} {x['plain_f0_hz'] or 0:7.1f} {x['shout_f0_hz'] or 0:7.1f} "
              f"{x['pitch_lift_st'] or 0:5.1f} {x['plain_alpha_db'] or 0:6.1f} {x['shout_alpha_db'] or 0:6.1f} {x['effort_lift_db'] or 0:5.1f}")
    print(f"characters spent: {el.spent if el else 0}")


if __name__ == "__main__":
    main()
