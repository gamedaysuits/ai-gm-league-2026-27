#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""Generate the show's SFX pack with the ElevenLabs Sound Effects API (POST /v1/sound-generation).

Generic, original, royalty-safe effects (no real team horns, no famous melodies, no crowd chants or words).
Each file is trimmed at the head, lightly faded at the tail (one-shots only), and gain-normalised with a single
linear gain to -18 LUFS integrated or -1 dBTP true peak, whichever is quieter (no limiting, transients intact).
manifest.json lists file, duration, kind, loopable, suggested gain for mixing under -16 LUFS dialogue, prompt, cost.

usage (repo root):  uv run media/assets/sfx/generate.py [--only name ...] [--force] [--dry-run]
Cost: 40 credits per second when duration_seconds is set (ElevenLabs docs); every call is logged.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent                 # media/assets/sfx
ROOT = HERE.parents[2]
RAW = HERE / "raw"
MANIFEST = HERE / "manifest.json"
LOG = HERE / "generation_log.jsonl"
API = "https://api.elevenlabs.io/v1/sound-generation"
MODEL = "eleven_text_to_sound_v2"
FORMAT = "mp3_44100_192"
REF_LUFS, CEIL_TP = -18.0, -1.0

# name: (kind, seconds, prompt_influence, loop, suggested_gain_db, prompt)
SFX = {
    "crowd_roar": ("crowd", 3.5, 0.5, False, -6.0,
                   "Packed ice hockey arena crowd roaring loudly after a goal, thousands of fans screaming and cheering, "
                   "big stadium reverb, no music, no announcer, no words"),
    "crowd_cheer_burst": ("crowd", 3.0, 0.5, False, -6.0,
                          "Short sudden burst of a huge arena crowd cheering, clapping and whistling, then quickly "
                          "settling down, no music, no words"),
    "crowd_ooh": ("crowd", 2.5, 0.55, False, -8.0,
                  "Large arena crowd reacting 'ooooh' in unison to a near miss: a rising collective gasp, then a "
                  "disappointed groan, no music, no words"),
    "crowd_laugh": ("crowd", 3.0, 0.55, False, -8.0,
                    "Big live audience laughing together at a joke in a large arena, warm hearty crowd laughter, "
                    "no speech, no music"),
    "goal_horn": ("stinger", 4.0, 0.6, False, -5.0,
                  "Generic ice hockey arena goal horn: one long, deep, very loud foghorn-style blast echoing through a "
                  "big arena, no crowd, no music, no melody"),
    "organ_stab": ("stinger", 2.5, 0.6, False, -6.0,
                   "Short original stadium pipe organ hype stab: a quick bright rising flourish of three chords ending "
                   "on a big held chord, punchy and triumphant, not a famous melody, no drums"),
    "air_horn": ("stinger", 1.5, 0.65, False, -9.0,
                 "Air horn blast: two short loud honks, dry, no music, no crowd"),
    "whoosh": ("transition", 1.5, 0.6, False, -6.0,
               "Fast cinematic whoosh transition ending in a deep sub-bass impact, clean sports-broadcast graphics sting, "
               "no music"),
    "record_scratch": ("comedy", 1.0, 0.7, False, -7.0,
                       "Comedic vinyl record scratch stop: one quick needle scratch, dry, no music afterwards"),
    "rimshot": ("comedy", 1.5, 0.7, False, -6.0,
                "Comedy rimshot drum sting, ba-dum-tss: two quick snare hits and a crash cymbal on a dry studio drum kit"),
    "slapshot": ("hockey", 1.5, 0.6, False, -4.0,
                 "Ice hockey slapshot: a wooden stick cracks hard against a puck on the ice, then the puck slams into the "
                 "arena boards with a loud thud and a plexiglass rattle, no crowd"),
    "crowd_bed_loop": ("ambience", 30.0, 0.45, True, -14.0,
                       "Indoor hockey arena crowd ambience before the game: thousands of people murmuring and chatting, "
                       "distant echoes in a big rink, steady and even, no cheering, no music, no announcer, "
                       "no clear words"),
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def ff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True)


def probe(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=channels,sample_rate",
                        "-of", "json", str(p)], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    return {"duration_s": round(float(d.get("format", {}).get("duration", 0)), 2),
            "channels": st.get("channels"), "sample_rate": int(st.get("sample_rate", 0) or 0)}


def loudness(p: Path) -> dict:
    r = ff("-i", str(p), "-af", "ebur128=peak=true", "-f", "null", "-")
    tail = r.stderr[r.stderr.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", tail)
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    val = lambda m: None if (not m or m.group(1) == "-inf") else float(m.group(1))
    return {"lufs": val(i), "true_peak_dbfs": val(tp)}


def finish(raw: Path, out: Path, loop: bool) -> dict:
    """Head trim + tail fade (one-shots), then one linear gain: -18 LUFS or -1 dBTP, whichever is quieter."""
    tmp = Path(tempfile.mkdtemp()) / "p.wav"
    if loop:
        ff("-y", "-i", str(raw), "-c:a", "pcm_f32le", str(tmp))  # never trim or fade a loop
    else:
        dur = probe(raw)["duration_s"]
        fade = min(0.08, max(0.02, dur * 0.05))
        ff("-y", "-i", str(raw), "-af",
           f"silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.01,"
           f"areverse,afade=t=in:d={fade:.3f},areverse", "-c:a", "pcm_f32le", str(tmp))
    m = loudness(tmp)
    gains = [g for g in ((REF_LUFS - m["lufs"]) if m["lufs"] is not None else None,
                         (CEIL_TP - m["true_peak_dbfs"]) if m["true_peak_dbfs"] is not None else None) if g is not None]
    gain = min(gains) if gains else 0.0
    ff("-y", "-i", str(tmp), "-af", f"volume={gain:.2f}dB", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k", str(out))
    return {"gain_applied_db": round(gain, 2), **loudness(out), **probe(out)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="generate only these names")
    ap.add_argument("--force", action="store_true", help="regenerate even if the file exists")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    todo = {k: v for k, v in SFX.items() if not args.only or k in args.only}
    est = sum(round(v[1] * 40) for k, v in todo.items() if args.force or not (HERE / f"{k}.mp3").exists())
    print(f"{len(todo)} effects; ~{est} credits to spend at 40 credits/s")
    if args.dry_run:
        return
    key = (dotenv_values(ROOT / ".env").get("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY missing from .env")
    RAW.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {"effects": {}}
    with httpx.Client(headers={"xi-api-key": key}, timeout=180) as c:
        for name, (kind, secs, infl, loop, gain, prompt) in todo.items():
            out = HERE / f"{name}.mp3"
            if out.exists() and not args.force:
                print(f"skip {name} (exists)")
                continue
            body = {"text": prompt, "duration_seconds": secs, "prompt_influence": infl, "loop": loop, "model_id": MODEL}
            t0 = time.time()
            r = c.post(API, params={"output_format": FORMAT}, json=body)
            dt = round(time.time() - t0, 2)
            cost_hdr = r.headers.get("character-cost") or r.headers.get("x-character-count")
            rec = {"ts": now(), "name": name, "status": r.status_code, "latency_s": dt, "credits_header": cost_hdr,
                   "credits_est": round(secs * 40), "request_id": r.headers.get("request-id"),
                   "error": None if r.status_code == 200 else r.text.replace(key, "***")[:300]}
            with LOG.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            if r.status_code != 200:
                print(f"{name}: ERROR {r.status_code} {rec['error']}")
                continue
            raw = RAW / f"{name}.mp3"
            raw.write_bytes(r.content)
            info = finish(raw, out, loop)
            manifest["effects"][name] = {
                "file": out.name, "kind": kind, "duration_s": info["duration_s"], "loopable": loop,
                "suggested_gain_db": gain, "lufs": info["lufs"], "true_peak_dbfs": info["true_peak_dbfs"],
                "channels": info["channels"], "sample_rate": info["sample_rate"], "requested_duration_s": secs,
                "prompt": prompt, "prompt_influence": infl, "model": MODEL,
                "credits": int(cost_hdr) if (cost_hdr or "").isdigit() else round(secs * 40),
                "credits_source": "response header" if (cost_hdr or "").isdigit() else "estimate (40/s)",
                "generated_at": rec["ts"],
            }
            print(f"{name:16s} {info['duration_s']:5.2f}s  {info['lufs']} LUFS  TP {info['true_peak_dbfs']}  "
                  f"{info['channels']}ch  {dt}s  credits {manifest['effects'][name]['credits']}")
            manifest.update({
                "_doc": ("SFX pack for the AI GM League show, generated with the ElevenLabs Sound Effects API "
                         f"({MODEL}). Files are gain-normalised to {REF_LUFS:.0f} LUFS integrated or {CEIL_TP:.0f} dBTP "
                         "(whichever is quieter). suggested_gain_db = gain to apply when mixing under -16 LUFS "
                         "dialogue. Generic, original sounds: no real team horns, no famous melodies, no chants."),
                "reference_lufs": REF_LUFS, "updated_at": now(),
                "total_credits": sum(e.get("credits", 0) for e in manifest["effects"].values()),
            })
            MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
