"""Render chunk compositions with the HyperFrames CLI, concatenate, and mux the show mix.

Each chunk (show/render/<cid>.html) is rendered on its own - a failed chunk can be re-rendered
without redoing the rest, and unchanged chunks are skipped (content hash of the chunk HTML +
avatar assets + render settings). Chunks are H.264 without B-frames (HyperFrames' libx264
settings), so they concatenate losslessly with ffmpeg's concat demuxer; audio is encoded once.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import audio
from .paths import SHOW, SHOW_GEN, rel


def chunk_hash(cid: str, cfg: dict) -> str:
    h = hashlib.sha256()
    h.update((SHOW / "render" / f"{cid}.html").read_bytes())
    for f in sorted((SHOW_GEN / "av").rglob("*.png")):
        h.update(f.name.encode() + str(f.stat().st_size).encode())
    r = cfg["render"]
    h.update(json.dumps([r["quality"], r["crf"], r["hyperframes"], cfg["fps"]]).encode())
    return h.hexdigest()


def render_chunk(cid: str, dur: float, cfg: dict, vdir: Path, log=print) -> dict:
    r = cfg["render"]
    out = vdir / f"{cid}.mp4"
    stamp = vdir / f"{cid}.sha"
    key = chunk_hash(cid, cfg)
    if out.exists() and stamp.exists() and stamp.read_text() == key:
        return {"cid": cid, "cached": True, "wall_s": 0.0, "path": rel(out)}
    cmd = ["npx", "--yes", r["hyperframes"], "render", str(SHOW), "-c", f"render/{cid}.html", "-o", str(out),
           "--quality", str(r["quality"]), "--crf", str(r["crf"]), "--fps", str(cfg["fps"]),
           "--workers", str(r["workers"]), "--quiet"]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=SHOW)
    wall = time.time() - t0
    (vdir / f"{cid}.log").write_text(proc.stdout[-20000:] + "\n--- stderr ---\n" + proc.stderr[-20000:])
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"hyperframes render failed for {cid} (exit {proc.returncode}); see {rel(vdir / (cid + '.log'))}")
    got = audio.duration_s(out)
    if abs(got - dur) > 0.5 / cfg["fps"]:  # even one extra frame shifts every later chunk against the audio
        log(f"render: WARNING {cid} is {round((got - dur) * cfg['fps']):+d} frame(s): {got:.4f}s != expected {dur:.4f}s")
    stamp.write_text(key)
    log(f"render: {cid} {dur:.1f}s of video in {wall:.1f}s ({dur / wall:.2f}x realtime)")
    return {"cid": cid, "cached": False, "wall_s": round(wall, 2), "path": rel(out), "duration_s": got}


def render_all(manifest: dict, cfg: dict, odir: Path, jobs: int | None = None, log=print) -> dict:
    vdir = odir / "video"
    vdir.mkdir(exist_ok=True)
    chunks = manifest["chunks"]
    jobs = jobs or int(cfg["render"]["jobs"])
    t0 = time.time()
    results: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(render_chunk, c["cid"], c["dur"], cfg, vdir, log): c["cid"] for c in chunks}
        for f in cf.as_completed(futs):
            results[futs[f]] = f.result()
    wall = time.time() - t0
    return {"chunks": [results[c["cid"]] for c in chunks], "wall_s": round(wall, 2), "jobs": jobs}


def mux(manifest: dict, cfg: dict, odir: Path, name: str, log=print) -> dict:
    vdir = odir / "video"
    lst = vdir / "concat.txt"
    lst.write_text("".join(f"file '{(vdir / (c['cid'] + '.mp4')).resolve()}'\n" for c in manifest["chunks"]))
    total = float(manifest["duration_s"])
    silent = vdir / "show_video.mp4"
    audio.run([audio.FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
               str(silent)])
    out = odir / name
    mix = odir / "mix.wav"
    audio.run([audio.FFMPEG, "-v", "error", "-y", "-i", str(silent), "-i", str(mix), "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "copy", "-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"], "-ar", "48000", "-ac", "2",
               "-t", f"{total:.4f}", "-movflags", "+faststart", str(out)])
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size,bit_rate:stream=codec_name,profile,width,height,"
         "r_frame_rate,nb_frames,pix_fmt,sample_rate,channels", "-of", "json", str(out)],
        capture_output=True, text=True).stdout)
    loud = audio.ebur128(out, true_peak=True)
    silent.unlink(missing_ok=True)
    info = {"path": rel(out), "size_mb": round(out.stat().st_size / 1e6, 2), "probe": probe, "ebur128": loud}
    log(f"mux: {rel(out)} {info['size_mb']} MB, I={loud['I']} LUFS TP={loud['TP']} dBTP")
    return info
