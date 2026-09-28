"""Assemble normalised lines into the show mix: tight hockey-bro pacing, crosstalk on reactions, arena
crowd bed, theme/bed music (ducked under voices), event-driven SFX (roar + horn on reveals, OOOH after
roasts, record scratch on reaches, whooshes on transitions), mastered to -16 LUFS / <= -1.5 dBTP.

Outputs (media/out/<run>/):
  mix.wav          48 kHz stereo 24-bit master
  voice.wav        dry voice stem (float32)
  cues.json        per-line start/end (seconds + frames), segment windows, show events, SFX list, loudness
  envelopes.json   per-speaker amplitude envelopes at the show fps (drives mouths and body motion)
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from . import audio
from . import sfx as SFX
from .paths import ROOT, rel

GM_KINDS = {"pick_statement", "on_air_call", "reaction", "table_talk", "chirp", "montage", "gm_card", "grade_comment",
            "hook"}


def _rng(seed: int, key: str) -> np.random.Generator:
    h = int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:12], 16)
    return np.random.default_rng(h)


def _u(rng: np.random.Generator, lo_hi: list[float]) -> float:
    lo, hi = lo_hi
    return float(rng.uniform(lo, hi)) / 1000.0


LAUGH_SPACING_S = 1.55  # the comic-timing QA gate: no two punch beats within 1.5 s


def gap_for(prev: dict, ln: dict, m: dict, rng) -> float:
    pb = prev.get("beats") or {}
    end_mk = next((x for x in reversed(pb.get("marks") or []) if x.get("at_end")), None)
    # measured from the kicker's end (where the voice stops), not from the end of the line's audio
    tail = (float(prev["end"]) - float(prev["start"]) - float(end_mk["kicker_end"])) if end_mk else 0.0
    if ln.get("kind") == "comeback":  # comebacks fire fast: 150-300 ms after the reaction cut (80 ms past the kicker)
        return max(0.02, 0.08 + _u(rng, [150, 300]) - tail) if end_mk else _u(rng, [150, 300])
    if pb.get("end_beat"):  # the line ENDS on a punch / button: the laugh beat is the gap
        return max(0.02, float(pb["end_beat"]) - tail)
    if prev.get("gap_hint") == "cutaway":  # hold on the roasted GM's face (shock / celebrate) + the crowd
        return _u(rng, m.get("cutaway_ms", [1000, 1250]))
    if ln["segment"] != prev["segment"]:
        return _u(rng, m["segment_gap_ms"])
    if ln["kind"] == "montage" or prev["kind"] == "montage":
        return _u(rng, m.get("montage_gap_ms", m["gap_ms"]))
    if prev.get("gap_hint") == "beat":
        return _u(rng, m.get("beat_ms", m["gap_ms"]))
    if prev.get("gap_hint") == "cue":
        return _u(rng, m.get("cue_ms", m["gap_ms"]))
    if ln["kind"] in ("reaction", "chirp") and prev["kind"] in GM_KINDS and prev["speaker"] != ln["speaker"]:
        return -_u(rng, m.get("overlap_ms", [0, 0]))  # the next GM jumps in over the tail
    if prev.get("gap_hint") == "roast":
        return _u(rng, m.get("roast_gap_ms", m["gap_ms"]))
    if ln.get("pick_no") is not None and ln.get("pick_no") != prev.get("pick_no"):
        return _u(rng, m["pick_gap_ms"])
    return _u(rng, m["gap_ms"])


def bed_automation(placed: list[dict], n: int, sr: int, dip_db: float = -4.0, lift_db: float = 2.0) -> np.ndarray:
    """Music gain curve from the beat marks: from each punch's first word to its kicker the bed dips; on the laugh
    beat it lifts. 20 ms control rate, smoothed."""
    ctl = 0.02
    k = int(math.ceil(n / sr / ctl)) + 1
    db = np.zeros(k, dtype=np.float32)
    for row in placed:
        for mk in (row.get("beats") or {}).get("marks") or []:
            a = row["start"] + float(mk["punch_start"]) - 0.6
            b = row["start"] + float(mk["kicker_end"])
            c = b + float(mk.get("beat") or 0.4) + 0.3
            db[max(0, int(a / ctl)):max(0, int(b / ctl))] = np.minimum(db[max(0, int(a / ctl)):max(0, int(b / ctl))],
                                                                       dip_db)
            db[max(0, int(b / ctl)):max(0, int(c / ctl))] = lift_db
    w = 7
    db = np.convolve(db, np.ones(w, dtype=np.float32) / w, mode="same")
    g = (10 ** (db / 20.0)).astype(np.float32)
    return np.interp(np.arange(n) / sr, np.arange(k) * ctl, g).astype(np.float32)


def apply_tempo(manifest: dict, tempo: float, sr: int) -> dict:
    """Delivery tightening for highlight cuts: pitch-preserving time-compression of every line (ffmpeg atempo),
    cached next to the normalised WAV; alignment times are rescaled so captions and slams stay on the word."""
    if not tempo or abs(tempo - 1.0) < 1e-3:
        return manifest
    out = {**manifest, "lines": {}, "tempo": tempo}
    for lid, info in manifest["lines"].items():
        src = ROOT / info["path"]
        dst = src.with_name(src.stem + f".t{int(round(tempo * 100))}.wav")
        if not dst.exists():
            audio.run([audio.FFMPEG, "-v", "error", "-y", "-i", str(src), "-af", f"atempo={tempo:.3f}", "-ar", str(sr),
                       "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
        new = {**info, "path": rel(dst), "duration_s": round(audio.duration_s(dst), 4)}
        bt = info.get("beats")
        if bt:  # the marks move with the delivery; the beats inside a line were sized x tempo (timing.assemble),
            # so they come out at their size; the end beat is a gap the mix leaves: already on-screen seconds
            new["beats"] = {**bt,
                            "marks": [{**mk, **{k: round(mk[k] / tempo, 4) for k in ("punch_start", "kicker_start",
                                                                                      "kicker_end") if k in mk},
                                       "beat": (mk.get("beat") if mk.get("at_end") else
                                                (round(mk["beat"] / tempo, 4) if mk.get("beat") else None))}
                                      for mk in bt.get("marks") or []]}
        al = info.get("alignment")
        if al and al.get("starts"):
            new["alignment"] = {"chars": al["chars"], "starts": [round(t / tempo, 4) for t in al["starts"]],
                                "ends": [round(t / tempo, 4) for t in al["ends"]]}
        out["lines"][lid] = new
    return out


def place_lines(rundown: dict, manifest: dict, cfg: dict) -> tuple[list[dict], list[dict], float]:
    m = cfg["mix"]
    seed = int(m["seed"])
    t = float(m["preroll_s"])
    placed: list[dict] = []
    prev = None
    for ln in rundown["timeline"]:
        info = manifest["lines"].get(ln["id"])
        if info is None:
            raise SystemExit(f"line {ln['id']} has no TTS audio; run the tts step first")
        rng = _rng(seed, ln["id"])
        if prev is not None:
            t = prev["end"] + gap_for(prev, ln, m, rng)
            t = max(t, prev["start"] + 0.55 * prev["duration"])  # never talk over the first half of a line
            # two laughs need room: this line's first punch never lands within 1.5 s of the previous line's last
            pm = [x for x in ((prev.get("beats") or {}).get("marks") or []) if x.get("role") in ("PUNCH", "BUTTON")]
            nm = [x for x in ((info.get("beats") or {}).get("marks") or []) if x.get("role") in ("PUNCH", "BUTTON")]
            if pm and nm:
                need = (prev["start"] + float(pm[-1]["kicker_end"]) + LAUGH_SPACING_S - float(nm[0]["kicker_end"])
                        - float(ln.get("pre_hold_s") or 0.0))
                t = max(t, need)
        t += float(ln.get("pre_hold_s") or 0.0)
        dur = float(info["duration_s"])
        row = {k: ln.get(k) for k in ("id", "speaker", "segment", "kind", "display_text", "focus", "pick_no", "round",
                                       "card", "addressed_to", "own_words", "refs", "gap_hint", "energy")}
        row.update({"tts_text": ln.get("text"), "start": round(t, 4), "end": round(t + dur, 4),
                    "duration": round(dur, 4), "audio": info["path"], "alignment": info.get("alignment"),
                    "beats": info.get("beats"), "beatmap": ln.get("beats")})
        placed.append(row)
        prev = row
    total = (max(r["end"] for r in placed) if placed else 0.0) + float(m["postroll_s"])
    fps = int(cfg["fps"])
    total = math.ceil(total * fps) / fps
    segs = []
    by_seg: dict[str, list[dict]] = {}
    for row in placed:
        by_seg.setdefault(row["segment"], []).append(row)
    order = [s for s in rundown["segments"] if s["id"] in by_seg]
    for i, s in enumerate(order):
        first = by_seg[s["id"]][0]
        if i == 0:
            start = 0.0
        else:
            prev_last = max(by_seg[order[i - 1]["id"]], key=lambda r: r["end"])
            start = prev_last["end"] + 0.35 * max(0.0, first["start"] - prev_last["end"])
            start = min(start, first["start"])
        segs.append({**s, "start": round(start, 4)})
    for i, s in enumerate(segs):
        s["end"] = segs[i + 1]["start"] if i + 1 < len(segs) else total
    return placed, segs, total


def room_tone(n: int, sr: int, dbfs: float, seed: int) -> np.ndarray:
    """Seamless brown-ish room tone: a 20 s FFT-synthesised loop (periodic, so tiling has no seams)."""
    loop = 20 * sr
    rng = np.random.default_rng(seed)
    spec = rng.normal(size=loop // 2 + 1) + 1j * rng.normal(size=loop // 2 + 1)
    f = np.fft.rfftfreq(loop, 1 / sr)
    shape = np.where(f > 0, 1.0 / np.maximum(f, 30.0) ** 0.85, 0.0)
    shape *= 1.0 / (1.0 + (f / 7000.0) ** 4)
    shape[f < 25] = 0.0
    x = np.fft.irfft(spec * shape, n=loop).astype(np.float32)
    x *= np.float32(10 ** (dbfs / 20) / (np.sqrt(np.mean(x ** 2)) + 1e-12))
    reps = int(math.ceil(n / loop))
    return np.tile(x, reps)[:n]


def activity(voice: np.ndarray, sr: int, attack_s: float = 0.06, release_s: float = 0.6) -> np.ndarray:
    """Smoothed 0..1 voice activity (10 ms control rate) used to duck music, crowd and crowd SFX."""
    hop = sr // 100
    nb = int(math.ceil(len(voice) / hop))
    pad = np.zeros(nb * hop, dtype=np.float32)
    pad[: len(voice)] = voice
    lvl = 20 * np.log10(np.sqrt(np.mean(pad.reshape(nb, hop) ** 2, axis=1)) + 1e-9)
    target = (lvl > -45.0).astype(np.float32)
    g = np.empty_like(target)
    cur = 0.0
    a_att = math.exp(-1.0 / (attack_s * 100))
    a_rel = math.exp(-1.0 / (release_s * 100))
    for i, tv in enumerate(target):
        a = a_att if tv > cur else a_rel
        cur = a * cur + (1 - a) * tv
        g[i] = cur
    return np.repeat(g, hop)[: len(voice)]


def duck(act: np.ndarray, depth_db: float) -> np.ndarray:
    return (1.0 - act * (1.0 - 10 ** (-depth_db / 20))).astype(np.float32)


def master(pre: Path, out: Path, sr: int, lufs: float, tp: float) -> dict:
    """Gain to target integrated loudness, then a lookahead limiter under the true-peak ceiling."""
    meas = audio.ebur128(pre, sr)
    gain = lufs - (meas["I"] if meas["I"] is not None else lufs)
    ceiling_db = tp - 0.6
    result = {}
    for _ in range(3):
        lim = 10 ** (ceiling_db / 20)
        audio.run([audio.FFMPEG, "-v", "error", "-y", "-i", str(pre), "-af",
                   f"volume={gain:.3f}dB,alimiter=limit={lim:.5f}:attack=5:release=60:level=disabled,"
                   f"aresample={sr}", "-ar", str(sr), "-c:a", "pcm_s24le", str(out)])
        result = audio.ebur128(out, sr, true_peak=True)
        if result["TP"] is None or result["TP"] <= tp:
            break
        ceiling_db -= (result["TP"] - tp) + 0.2
    # the limiter shaves a little loudness on dense mixes: one corrective pass keeps us on target
    if result.get("I") is not None and abs(result["I"] - lufs) > 0.3:
        gain += lufs - result["I"]
        lim = 10 ** (ceiling_db / 20)
        audio.run([audio.FFMPEG, "-v", "error", "-y", "-i", str(pre), "-af",
                   f"volume={gain:.3f}dB,alimiter=limit={lim:.5f}:attack=5:release=60:level=disabled,"
                   f"aresample={sr}", "-ar", str(sr), "-c:a", "pcm_s24le", str(out)])
        result = audio.ebur128(out, sr, true_peak=True)
    result.update({"gain_db": round(gain, 2), "limiter_ceiling_dbfs": round(ceiling_db, 2),
                   "premaster_I": meas["I"]})
    return result


def envelopes(placed: list[dict], sr: int, fps: int) -> dict:
    spf = sr // fps
    out: dict[str, list[dict]] = {}
    for row in placed:
        x, _ = audio.read_wav(ROOT / row["audio"])
        s0 = int(round(row["start"] * sr))
        f0 = s0 // spf
        off = s0 - f0 * spf
        nf = int(math.ceil((off + len(x)) / spf))
        buf = np.zeros(nf * spf, dtype=np.float32)
        buf[off:off + len(x)] = x
        rms = np.sqrt(np.mean(buf.reshape(nf, spf) ** 2, axis=1) + 1e-12)
        db = 20 * np.log10(rms)
        v = np.clip((db + 45.0) / 33.0, 0.0, 1.0)  # -45 dBFS -> 0, -12 dBFS -> 1
        out.setdefault(row["speaker"], []).append({"line": row["id"], "f0": int(f0),
                                                   "v": [round(float(a), 3) for a in v]})
    return out


def run_mix(rundown: dict, manifest: dict, cfg: dict, odir: Path, league, log=print) -> dict:
    sr, fps = int(cfg["sample_rate"]), int(cfg["fps"])
    m = cfg["mix"]
    manifest = apply_tempo(manifest, float(m.get("tempo") or 1.0), sr)
    placed, segs, total = place_lines(rundown, manifest, cfg)
    events = SFX.plan_events(placed, segs, league)
    n = int(round(total * sr))
    voice = np.zeros(n, dtype=np.float32)
    for row in placed:
        x, xsr = audio.read_wav(ROOT / row["audio"])
        if xsr != sr:
            raise SystemExit(f"{row['audio']} is {xsr} Hz, expected {sr}")
        s0 = int(round(row["start"] * sr))
        voice[s0:s0 + len(x)] += x[: max(0, n - s0)]
    audio.write_wav(odir / "voice.wav", voice, sr, bits=32)
    act = activity(voice, sr)
    tone = room_tone(n, sr, float(m["room_tone_dbfs"]), int(m["seed"]))
    mix = np.stack([voice + tone, voice + np.roll(tone, sr // 3)], axis=1)
    lib = SFX.SfxLibrary(sr, log=log) if m.get("sfx", True) else None
    sfx_info: dict = {"enabled": bool(lib)}
    if lib:
        style = str(m.get("sfx_style") or "arena")
        mix += SFX.crowd_stem(n, sr, lib, float(m["crowd_lufs"]), style) * duck(act, 4.0)[:, None]
        ducked, dry = SFX.render_sfx(events, n, sr, lib, style)
        mix += ducked * duck(act, 5.0)[:, None] + dry
        sfx_info.update({"style": style, "sources": {k: v["source"] for k, v in lib.describe().items()},
                         "cues": [[round(t, 3), k, g] for t, k, g in SFX.sfx_cues(events, style)]})
    music, minfo = SFX.music_stem(segs, total, sr, cfg)
    music = music * bed_automation(placed, n, sr)[:, None]  # dips into the punch, lifts on the laugh
    mix += music * duck(act, float(m["music_duck_db"]))[:, None]
    mpath = (m.get("music") or {}).get("path")
    if mpath:  # optional extra bed (legacy --music)
        mfile = Path(mpath) if Path(mpath).is_absolute() else ROOT / mpath
        if mfile.exists():
            mus = audio.decode(mfile, sr=sr, channels=2)
            mus = SFX.loop_to(mus, n, int(0.5 * sr)) * np.float32(10 ** (float(m["music"]["gain_db"]) / 20))
            mix += mus * duck(act, 11.0)[:, None]
            minfo["extra"] = rel(mfile)
    pre = odir / "premaster.wav"
    audio.write_wav(pre, mix, sr, bits=32)
    log(f"mix: {len(placed)} lines over {total:.1f}s; {len(events)} events; mastering")
    loud = master(pre, odir / "mix.wav", sr, float(m["master_lufs"]), float(m["master_tp"]))
    pre.unlink(missing_ok=True)
    env = envelopes(placed, sr, fps)
    for row in placed:
        row["start_frame"] = int(math.floor(row["start"] * fps))
        row["end_frame"] = int(math.ceil(row["end"] * fps))
    cues = {"schema": "gmbench.cues/2", "run": rundown["run"], "profile": rundown.get("profile"), "fps": fps,
            "sample_rate": sr, "duration_s": round(total, 4), "total_frames": int(round(total * fps)),
            "segments": segs, "lines": placed, "events": events, "sfx": sfx_info, "music": minfo,
            "loudness": loud, "speech_s": round(sum(r["duration"] for r in placed), 2), "mix": rel(odir / "mix.wav")}
    (odir / "cues.json").write_text(json.dumps(cues, indent=1, ensure_ascii=False))
    (odir / "envelopes.json").write_text(json.dumps({"fps": fps, "speakers": env}))
    log(f"mix: master I={loud.get('I')} LUFS, TP={loud.get('TP')} dBTP, LRA={loud.get('LRA')} LU")
    return cues
