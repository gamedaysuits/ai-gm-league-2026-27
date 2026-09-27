# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""HeyGen Avatar IV (via OpenRouter /api/v1/videos) test: pixel avatar + our own ElevenLabs audio.

  uv run media/avatars/avatar_iv_test.py submit --name opus --image media/assets/avatars/opus/base.png \
      --audio media/assets/avatars/_avatar_iv_test/opus_line.mp3 --text "..." [--no-passthrough]
  uv run media/avatars/avatar_iv_test.py poll --name opus

Image and audio go in input_references as base64 data URLs (nothing is uploaded anywhere else).
Passthrough (provider.options.heygen): motion_prompt + expressiveness. Hard cap: $2.00 total.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "media" / "assets" / "avatars" / "_avatar_iv_test"
API = "https://openrouter.ai/api/v1/videos"
MODEL = "heygen/avatar-iv"
PRICE_PER_S = 0.05
CAP = 2.00
MOTION = ("animated, gesturing with wings/hands, energetic sports broadcaster, big expressive reactions, "
          "lively head and shoulder movement, keep the pixel-art style")


def key() -> str:
    k = dotenv_values(ROOT / ".env").get("OPENROUTER_API_KEY")
    if not k:
        sys.exit("OPENROUTER_API_KEY missing")
    return k


def headers() -> dict:
    return {"Authorization": f"Bearer {key()}", "Content-Type": "application/json", "X-Title": "GDS Avatar IV test"}


def spent() -> float:
    p = OUT / "jobs.jsonl"
    if not p.exists():
        return 0.0
    return sum(float(json.loads(l).get("cost") or 0) for l in p.read_text().splitlines())


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def log(rec: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "jobs.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def submit(a) -> None:
    audio = Path(a.audio) if a.audio else None
    # without our audio HeyGen speaks the prompt with its own TTS: estimate ~2.6 words/s
    secs = duration(audio) if audio else max(4.0, len(a.text.split()) / 2.6)
    est = secs * PRICE_PER_S * 1.2 + 0.05
    if spent() + est > CAP:
        sys.exit(f"REFUSED: spent ${spent():.2f} + est ${est:.2f} would exceed ${CAP:.2f}")
    body = {
        "model": MODEL,
        "prompt": a.text,
        "aspect_ratio": "1:1",
        "resolution": "720p",
        "input_references": [{"type": "image_url", "image_url": {"url": uri(Path(a.image), "image/png")}}],
    }
    if audio:  # NOTE: OpenRouter only accepts https:// audio URLs here (data: URLs are rejected)
        body["input_references"].append({"type": "audio_url", "audio_url": {"url": a.audio_url or uri(audio, "audio/mpeg")}})
    if not a.no_passthrough:
        opts = {"motion_prompt": a.motion or MOTION, "expressiveness": a.expressiveness}
        if a.voice_id:
            opts["voice_id"] = a.voice_id
        body["provider"] = {"options": {"heygen": opts}}
    t0 = time.time()
    r = httpx.post(API, headers=headers(), json=body, timeout=120)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:800]}
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "name": a.name, "event": "submit",
           "status_code": r.status_code, "audio_s": round(secs, 2), "image": a.image, "audio": a.audio,
           "passthrough": body.get("provider"), "response": data, "cost": 0.0, "submit_s": round(time.time() - t0, 1)}
    log(rec)
    print(json.dumps({k: rec[k] for k in ("name", "status_code", "audio_s", "response")}, indent=1)[:2000])
    if r.status_code in (200, 201, 202) and data.get("id"):
        (OUT / f"{a.name}.job").write_text(json.dumps({"id": data["id"], "submitted": time.time(), "est_s": secs}))


def poll(a) -> None:
    job = json.loads((OUT / f"{a.name}.job").read_text())
    jid = job["id"]
    deadline = time.time() + a.timeout
    while True:
        r = httpx.get(f"{API}/{jid}", headers=headers(), timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:500]}
        status = data.get("status")
        el = time.time() - job["submitted"]
        print(f"[{a.name}] {status} after {el:.0f}s")
        if status in ("completed", "failed", "cancelled", "expired") or time.time() > deadline:
            break
        time.sleep(a.every)
    cost = float((data.get("usage") or {}).get("cost") or 0)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "name": a.name, "event": "poll",
           "status": status, "render_s": round(el), "cost": cost, "response": data}
    if status == "completed":
        url = (data.get("unsigned_urls") or [f"{API}/{jid}/content?index=0"])[0]
        v = httpx.get(url, headers={"Authorization": f"Bearer {key()}"}, timeout=300, follow_redirects=True)
        dst = OUT / f"{a.name}.mp4"
        dst.write_bytes(v.content)
        rec.update(file=dst.name, bytes=len(v.content))
    log(rec)
    print(json.dumps({k: rec.get(k) for k in ("name", "status", "render_s", "cost", "file", "bytes")}, indent=1))
    if status == "failed":
        print(json.dumps(data, indent=1)[:1500])
    print(f"total spent: ${spent():.3f} of ${CAP:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("--name", required=True)
    s.add_argument("--image", required=True)
    s.add_argument("--audio", default="", help="local mp3 (only usable with --audio-url; data: URLs are rejected)")
    s.add_argument("--audio-url", default="", help="public https URL of the same audio")
    s.add_argument("--voice-id", default="", help="HeyGen voice id for its built-in TTS")
    s.add_argument("--text", required=True, help="transcript of the audio (used as the prompt)")
    s.add_argument("--motion", default="")
    s.add_argument("--expressiveness", default="high")
    s.add_argument("--no-passthrough", action="store_true")
    p = sub.add_parser("poll")
    p.add_argument("--name", required=True)
    p.add_argument("--every", type=int, default=20)
    p.add_argument("--timeout", type=int, default=1500)
    a = ap.parse_args()
    submit(a) if a.cmd == "submit" else poll(a)


if __name__ == "__main__":
    main()
