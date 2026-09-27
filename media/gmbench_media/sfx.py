"""Sound design: SFX library, show events, and the SFX/music/crowd stems.

Events (reveals, clocks, shouts, roasts, snipes, celebrations, segment changes, board ticks) are planned
once from the placed lines and shared by the mixer (SFX) and the composer (motion), so the horn, the
name slam, the zoom punch and the confetti all land on the same frame.

SFX sources: media/assets/sfx/manifest.json (the voice agent's pack; matched to our kinds by id/name/tags
keywords) with procedural placeholders for any kind the pack does not provide yet.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np

from . import audio
from . import tags as T
from .paths import MEDIA, ROOT, cache_dir, rel

SFX_MANIFEST = MEDIA / "assets" / "sfx" / "manifest.json"
PLACEHOLDER_VERSION = 2
KINDS = ["crowd_bed", "roar", "horn", "organ", "ooh", "laugh", "scratch", "whoosh", "impact", "tick", "murmur", "cheer",
         "rimshot", "can"]
KEYWORDS = {
    "crowd_bed": [("crowd", "bed"), ("crowd", "ambience"), ("crowd", "ambient"), ("arena", "bed"),
                  ("arena", "ambience"), ("crowd", "loop"), ("arena", "loop")],
    "roar": [("roar",), ("crowd", "cheer"), ("goal", "crowd"), ("crowd", "erupt")],
    "horn": [("horn",)],
    "organ": [("organ",)],
    "ooh": [("ooh",), ("oooh",), ("gasp",), ("ohh",)],
    "laugh": [("laugh",)],
    "scratch": [("scratch",), ("record",)],
    "whoosh": [("whoosh",), ("swoosh",), ("swish",), ("transition",)],
    "rimshot": [("rimshot",), ("ba-dum",), ("badum",)],
    "impact": [("slapshot",), ("slap", "shot"), ("impact",), ("boom",), ("slam",), ("hit",)],
    "tick": [("tick",), ("click",), ("blip",)],
    "can": [("can", "open"), ("beer",), ("pop", "tab")],
    "murmur": [("murmur",), ("anticipation",), ("tension",), ("buzz",)],
    "cheer": [("cheer",), ("applause",)],
}
# level relative to a -20 LUFS reference (voices sit at -19 LUFS before mastering)
GAIN_DB = {"roar": -3.0, "horn": -5.0, "organ": -7.0, "ooh": -4.0, "laugh": -5.0, "scratch": -4.0, "whoosh": -9.0,
           "impact": -4.0, "tick": -12.0, "murmur": -11.0, "cheer": -5.0, "rimshot": -6.0, "can": -8.0}
DUCKED = {"roar", "ooh", "laugh", "murmur", "cheer"}
PEAK_NORMALISED = {"impact", "tick", "whoosh", "scratch", "rimshot", "can"}


# --------------------------------------------------------------------------- DSP helpers (placeholders)

def _rng(key: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def _band_noise(n: int, sr: int, rng, lo: float, hi: float, tilt: float = -0.6) -> np.ndarray:
    spec = rng.normal(size=n // 2 + 1) + 1j * rng.normal(size=n // 2 + 1)
    f = np.fft.rfftfreq(n, 1 / sr)
    band = np.clip((f - lo * 0.7) / (lo * 0.3 + 1e-9), 0, 1) * np.clip((hi * 1.3 - f) / (hi * 0.3 + 1e-9), 0, 1)
    shape = band * np.where(f > 1, (np.maximum(f, 1) / 1000.0) ** (tilt / 2), 0)
    x = np.fft.irfft(spec * shape, n=n)
    return (x / (np.max(np.abs(x)) + 1e-9)).astype(np.float32)


def _env(n: int, sr: int, pts: list[tuple[float, float]]) -> np.ndarray:
    t = np.arange(n) / sr
    xs, ys = zip(*pts)
    return np.interp(t, xs, ys).astype(np.float32)


def _saw(freq: np.ndarray, sr: int, phase0: float = 0.0) -> np.ndarray:
    ph = phase0 + np.cumsum(freq) / sr
    return (2.0 * (ph % 1.0) - 1.0).astype(np.float32)


def _formants(x: np.ndarray, sr: int, formants: list[tuple[float, float, float]]) -> np.ndarray:
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    g = np.zeros_like(f)
    for fc, bw, amp in formants:
        g += amp * np.exp(-0.5 * ((f - fc) / bw) ** 2)
    y = np.fft.irfft(X * g, n=len(x))
    return (y / (np.max(np.abs(y)) + 1e-9)).astype(np.float32)


def _stereo(l: np.ndarray, r: np.ndarray | None = None) -> np.ndarray:
    return np.stack([l, l if r is None else r], axis=1).astype(np.float32)


def _crowd(n: int, sr: int, key: str, lo=220, hi=4200, tilt=-0.7) -> np.ndarray:
    rng = _rng(key)
    l = _band_noise(n, sr, rng, lo, hi, tilt)
    r = _band_noise(n, sr, rng, lo, hi, tilt)
    t = np.arange(n) / sr
    tex = 1.0 + 0.18 * np.sin(2 * np.pi * 3.1 * t + rng.uniform(0, 6)) * np.sin(2 * np.pi * 0.7 * t)
    return _stereo(l * tex, r * tex)


def synth(kind: str, sr: int) -> np.ndarray:
    """Procedural placeholder for one SFX kind (stereo float32)."""
    if kind == "crowd_bed":
        n = 20 * sr  # periodic -> seamless loop
        rng = _rng("crowd_bed")
        l = _band_noise(n, sr, rng, 200, 3500, -0.9)
        r = _band_noise(n, sr, rng, 200, 3500, -0.9)
        t = np.arange(n) / sr
        am = 1 + 0.16 * np.sin(2 * np.pi * t / 20 * 3) + 0.1 * np.sin(2 * np.pi * t / 20 * 7 + 1.3)
        return _stereo(l * am, r * am)
    if kind in ("roar", "cheer", "murmur"):
        dur = {"roar": 3.4, "cheer": 2.0, "murmur": 1.8}[kind]
        n = int(dur * sr)
        x = _crowd(n, sr, kind, 260 if kind != "murmur" else 180, 5200 if kind != "murmur" else 2500)
        pts = {"roar": [(0, 0), (0.12, 1), (0.9, 0.85), (2.0, 0.5), (dur, 0)],
               "cheer": [(0, 0), (0.08, 1), (0.8, 0.7), (dur, 0)],
               "murmur": [(0, 0), (1.3, 0.9), (dur, 0.2)]}[kind]
        return x * _env(n, sr, pts)[:, None]
    if kind == "horn":
        dur, n = 2.1, int(2.1 * sr)
        t = np.arange(n) / sr
        vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t)
        x = sum(_saw(np.full(n, f) * vib, sr, ph) for f, ph in ((185.0, 0.0), (233.1, 0.3), (277.2, 0.6), (92.5, 0.1)))
        x = _formants(x, sr, [(300, 250, 1.0), (900, 400, 0.8), (1800, 600, 0.35)])
        return _stereo(x * _env(n, sr, [(0, 0), (0.05, 1), (1.7, 0.95), (dur, 0)]))
    if kind == "organ":
        notes = [(392.0, 0.15), (523.3, 0.15), (659.3, 0.15), (784.0, 0.5)]
        parts = []
        for f0, d in notes:
            n = int(d * sr)
            t = np.arange(n) / sr
            y = sum(a * np.sin(2 * np.pi * f0 * k * t) for k, a in ((1, 1), (2, .6), (3, .35), (4, .25), (6, .12), (8, .08)))
            parts.append(y * _env(n, sr, [(0, 0), (0.008, 1), (d * 0.7, 0.8), (d, 0)]))
        x = np.concatenate(parts).astype(np.float32)
        return _stereo(x / (np.max(np.abs(x)) + 1e-9))
    if kind == "whoosh":
        dur, hop = 0.55, 256
        n = int(dur * sr)
        rng = _rng("whoosh")
        noise = rng.normal(size=n + 2048).astype(np.float32)
        out = np.zeros(n + 2048, dtype=np.float32)
        win = np.hanning(1024).astype(np.float32)
        freqs = np.fft.rfftfreq(1024, 1 / sr)
        for i in range(0, n, hop):
            u = i / n
            fc = 400 * (8 ** math.sin(math.pi * u)) if u < 0.7 else 3200 - (u - 0.7) / 0.3 * 2300
            mask = np.exp(-0.5 * (np.log2(np.maximum(freqs, 20) / fc) / 0.55) ** 2)
            seg = np.fft.irfft(np.fft.rfft(noise[i:i + 1024] * win) * mask, n=1024)
            out[i:i + 1024] += seg.astype(np.float32) * win
        x = out[:n] * _env(n, sr, [(0, 0), (0.3, 1), (dur, 0)])
        x /= np.max(np.abs(x)) + 1e-9
        pan = np.linspace(0.2, 0.8, n, dtype=np.float32)
        return _stereo(x * (1 - pan) * 1.4, x * pan * 1.4)
    if kind == "impact":
        dur, n = 1.1, int(1.1 * sr)
        t = np.arange(n) / sr
        f = 38 + 57 * np.exp(-t / 0.12)
        boom = np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t / 0.33)
        rng = _rng("impact")
        click = _band_noise(n, sr, rng, 80, 6000, -1.0) * np.exp(-t / 0.02)
        x = np.tanh(2.2 * (boom + 0.5 * click)).astype(np.float32)
        return _stereo(x / (np.max(np.abs(x)) + 1e-9))
    if kind == "scratch":
        dur, n = 0.5, int(0.5 * sr)
        t = np.arange(n) / sr
        speed = np.interp(t, [0, 0.12, 0.22, 0.34, dur], [0.2, 1.6, -1.4, 1.1, 0.1])
        tone = _saw(np.abs(speed) * 520 + 40, sr)
        rng = _rng("scratch")
        grit = _band_noise(n, sr, rng, 300, 5000, -0.3) * np.abs(speed)
        x = np.tanh(1.8 * (0.8 * tone + 0.6 * grit)) * _env(n, sr, [(0, 0), (0.02, 1), (0.44, 0.8), (dur, 0)])
        return _stereo(x.astype(np.float32) / (np.max(np.abs(x)) + 1e-9))
    if kind in ("ooh", "laugh"):
        dur = 1.9 if kind == "ooh" else 2.1
        n = int(dur * sr)
        t = np.arange(n) / sr
        rng = _rng(kind)
        voices = []
        for _ in range(28 if kind == "ooh" else 18):
            f0 = rng.uniform(110, 290)
            if kind == "ooh":
                glide = np.interp(t, [0, 0.35, dur], [1.12, 1.18, 0.9])
                f = f0 * glide * (1 + 0.012 * np.sin(2 * np.pi * rng.uniform(4, 6.5) * t + rng.uniform(0, 6)))
                amp = 1.0
            else:
                f = f0 * (1 + 0.03 * np.sin(2 * np.pi * rng.uniform(4.5, 6.5) * t + rng.uniform(0, 6)))
                rate = rng.uniform(4.2, 6.2)
                amp = np.clip(np.sin(2 * np.pi * rate * t + rng.uniform(0, 6)), 0, 1) ** 1.5
            voices.append(_saw(f, sr, rng.uniform(0, 1)) * amp * rng.uniform(0.5, 1.0))
        x = np.sum(voices, axis=0)
        fm = [(360, 90, 1.0), (840, 130, 0.55), (2400, 300, 0.08)] if kind == "ooh" else \
             [(760, 140, 1.0), (1220, 180, 0.7), (2600, 300, 0.12)]
        x = _formants(x, sr, fm)
        x += 0.04 * _band_noise(n, sr, rng, 300, 6000, -0.5)
        pts = [(0, 0), (0.28, 1), (1.2, 0.8), (dur, 0)] if kind == "ooh" else [(0, 0), (0.1, 1), (1.3, 0.6), (dur, 0)]
        x = x * _env(n, sr, pts)
        d = int(0.013 * sr)
        return _stereo(x, np.concatenate([np.zeros(d, np.float32), x[:-d]]))
    if kind == "rimshot":
        n = int(0.9 * sr)
        t = np.arange(n) / sr
        rng = _rng("rimshot")
        hit = lambda t0, dec: _band_noise(n, sr, rng, 900, 9000, -0.2) * np.exp(-np.maximum(t - t0, 0) / dec) * (t >= t0)
        x = hit(0.0, 0.05) + hit(0.14, 0.05) + 0.8 * _band_noise(n, sr, rng, 3000, 16000, 0.0) * np.exp(-np.maximum(t - 0.28, 0) / 0.35) * (t >= 0.28)
        return _stereo((x / (np.max(np.abs(x)) + 1e-9)).astype(np.float32))
    if kind == "can":  # a beer can cracked open: a sharp pop, then the fizz
        n = int(0.9 * sr)
        t = np.arange(n) / sr
        rng = _rng("can")
        pop = _band_noise(n, sr, rng, 1500, 9000, 0.2) * np.exp(-t / 0.012)
        fizz = _band_noise(n, sr, rng, 4000, 14000, 0.4) * np.exp(-np.maximum(t - 0.03, 0) / 0.28) * (t >= 0.03) * 0.35
        x = (pop + fizz).astype(np.float32)
        return _stereo(x / (np.max(np.abs(x)) + 1e-9))
    if kind == "tick":
        n = int(0.08 * sr)
        t = np.arange(n) / sr
        x = (np.sin(2 * np.pi * 2200 * t) * np.exp(-t / 0.012) + 0.4 * np.sin(2 * np.pi * 4400 * t) * np.exp(-t / 0.006))
        return _stereo(x.astype(np.float32))
    raise KeyError(kind)


# --------------------------------------------------------------------------- library

class SfxLibrary:
    def __init__(self, sr: int = 48000, log=print):
        self.sr = sr
        self.log = log
        self.files: dict[str, list[Path]] = {k: [] for k in KINDS}
        self.source: dict[str, str] = {}
        self._cache: dict[tuple[str, int], np.ndarray] = {}
        if SFX_MANIFEST.exists():
            self._read_manifest(json.loads(SFX_MANIFEST.read_text()))
        for k in KINDS:
            self.source[k] = "pack" if self.files[k] else "placeholder"

    def _read_manifest(self, data) -> None:
        entries: list[tuple[str, dict]] = []

        def walk(obj, key=""):
            if isinstance(obj, dict):
                if any(k in obj for k in ("path", "file", "src", "files")):
                    entries.append((key, obj))
                    return
                for k, v in obj.items():
                    walk(v, f"{key} {k}".strip())
            elif isinstance(obj, list):
                for v in obj:
                    walk(v, key)

        walk(data)
        base = SFX_MANIFEST.parent
        for key, e in entries:
            words = " ".join(str(x) for x in [key, e.get("id"), e.get("name"), e.get("kind"), e.get("category"),
                                              e.get("description"), " ".join(e.get("tags") or [])] if x).lower()
            paths = e.get("files") or [e.get("path") or e.get("file") or e.get("src")]
            for kind, rules in KEYWORDS.items():
                if any(all(w in words for w in rule) for rule in rules):
                    for p in paths:
                        if not p:
                            continue
                        for cand in (base / p, ROOT / p, MEDIA / p):
                            if cand.exists():
                                self.files[kind].append(cand)
                                break
                    break

    def get(self, kind: str, variant: int = 0) -> np.ndarray:
        """Stereo float32, normalised to a -20 LUFS reference (peak -3 dBFS for transients)."""
        paths = self.files.get(kind) or []
        vi = variant % len(paths) if paths else 0
        key = (kind, vi)
        if key in self._cache:
            return self._cache[key]
        if paths:
            x = audio.decode(paths[vi], sr=self.sr, channels=2)
        else:
            f = cache_dir("sfx", "placeholder") / f"{kind}-v{PLACEHOLDER_VERSION}.wav"
            if f.exists():
                x, _ = audio.read_wav(f)
            else:
                x = synth(kind, self.sr)
                audio.write_wav(f, x, self.sr, bits=32)
        x = x.astype(np.float32)
        if kind in PEAK_NORMALISED or len(x) < int(0.5 * self.sr):
            x = x * np.float32(10 ** (-3 / 20) / (np.max(np.abs(x)) + 1e-9))
        else:
            meas = audio.ebur128(x, self.sr)["I"]
            if meas is not None:
                x = x * np.float32(10 ** ((-20.0 - meas) / 20))
        self._cache[key] = x
        return x

    def describe(self) -> dict:
        return {k: {"source": self.source[k], "files": [rel(p) for p in self.files[k]]} for k in KINDS}


# --------------------------------------------------------------------------- events

def phrase_time(line: dict, phrase: str, default_frac: float = 0.55) -> float:
    """When the speaker reaches `phrase` in this line: TTS alignment when present, else character share."""
    return name_time(line, phrase, default_frac, surname=False)


def name_time(line: dict, name: str, default_frac: float = 0.55, surname: bool = True) -> float:
    """When the speaker reaches `name` (full name, else surname) in this line: TTS alignment when present,
    else character share of the line."""
    start, end = float(line["start"]), float(line["end"])
    al = line.get("alignment") or {}
    keys = [k for k in (name, (name or "").split()[-1] if (name and surname) else "") if k]
    if al.get("chars"):
        s = "".join(al["chars"]).lower()
        for k in keys:
            i = s.find(k.lower())
            if i >= 0:
                return round(start + float(al["starts"][i]), 4)
    txt = (line.get("display_text") or "").lower()
    for k in keys:
        i = txt.find(k.lower())
        if i >= 0:
            return round(max(start, start + i / max(1, len(txt)) * (end - start) - 0.05), 4)
    return round(start + default_frac * (end - start), 4)


def beat2_time(line: dict) -> float | None:
    """Start of beat 2 (the GM's own pick) in a two-beat call, from the caption word timing (TTS alignment)."""
    c = line.get("card") or {}
    k = c.get("beat2_word")
    if not k:
        return None
    from .captions import word_times
    ws = word_times(line)
    if not ws:
        return None
    return round(ws[min(int(k), len(ws) - 1)]["start"], 4) if int(k) < len(ws) else round(float(line["end"]), 4)


def call_name_time(line: dict, player: str) -> float | None:
    """When the GM says the player's name in their call (beat 2): fuzzy match of the display words (timed by the TTS
    alignment) against player_name; lands on the first name when it is said, else on the surname."""
    import difflib

    from .captions import word_times
    ws = word_times(line)
    if not ws or not player:
        return None
    core = lambda w: re.sub(r"[^a-z0-9]", "", w.lower().replace("’", "'").removesuffix("'s"))  # noqa: E731
    target = [core(w) for w in player.split() if core(w)]
    if not target:
        return None
    sur, first = target[-1], target[0]
    start = int((line.get("card") or {}).get("beat2_word") or 0)
    for i in range(min(start, len(ws) - 1), len(ws)):
        w = core(ws[i]["w"])
        if w == sur or (len(sur) >= 5 and difflib.SequenceMatcher(None, sur, w).ratio() >= 0.8):
            if len(target) >= 2 and i > 0 and difflib.SequenceMatcher(None, first, core(ws[i - 1]["w"])).ratio() >= 0.8:
                return round(ws[i - 1]["start"], 4)
            return round(ws[i]["start"], 4)
    return None


def is_reach(p, league) -> bool:
    return league.value_flags().get(p.pick_no) == "reach"


def slam_time(ln: dict, t: float) -> float:
    """The name slam never collides with a punchline: a name within 400 ms of a kicker slams after the laugh beat."""
    for mk in (ln.get("beats") or {}).get("marks") or []:
        ke = ln["start"] + float(mk["kicker_end"])
        if abs(t - ke) < 0.4 or (ln["start"] + float(mk["kicker_start"]) - 0.05 <= t <= ke):
            return round(ke + float(mk.get("beat") or 0.4) + 0.05, 4)
    return t


def _reveal(ev: list[dict], league, p, ln: dict, t: float) -> None:
    flag = league.value_flags().get(p.pick_no)
    ev.append({"t": t, "type": "reveal", "pick_no": p.pick_no, "team": p.team, "line": ln["id"],
               "player": p.player_name, "round": p.round, "big": p.round == 1 or ln.get("round") == 1,
               "reach": flag == "reach", "flag": flag, "slam_t": slam_time(ln, t)})
    ev.append({"t": round(t + 0.25, 4), "type": "celebrate", "team": p.team, "pick_no": p.pick_no,
               "until": round(t + 2.6, 4)})
    for s in league.reactions_for(p.pick_no):
        if "top of your own draft queue" in (s.cue or "") and s.team != p.team:
            ev.append({"t": round(t + 0.1, 4), "type": "snipe", "team": s.team, "pick_no": p.pick_no,
                       "until": round(t + 1.9, 4)})


def punch_events(placed: list[dict]) -> list[dict]:
    """A room reaction on every punch / button (the beat marks): sized to the spice, praise gets a cheer."""
    from .party import tone
    out = []
    for row in placed:
        bm = row.get("beatmap") or {}
        for mk in (row.get("beats") or {}).get("marks") or []:
            t = row["start"] + float(mk["kicker_end"])
            praise = tone(row.get("display_text") or "") == "praise" and mk.get("role") == "PUNCH" and \
                row.get("kind") != "comeback"
            out.append({"t": round(t, 4), "type": "punch_beat", "speaker": row["speaker"], "line": row["id"],
                        "role": mk.get("role"), "spice": int(mk.get("spice") or 1), "beat": mk.get("beat"),
                        "praise": praise, "tight": bool(bm)})
    return out


def plan_events(placed: list[dict], segs: list[dict], league) -> list[dict]:
    picks = {p.pick_no: p for p in league.picks}
    ev: list[dict] = []
    host_called = {ln.get("pick_no") for ln in placed if ln["kind"] == "host_pick"}
    for s in segs[1:]:
        ev.append({"t": s["start"], "type": "segment", "segment": s["id"], "kind": s["kind"]})
    for i, ln in enumerate(placed):
        kind, sp = ln["kind"], ln["speaker"]
        text = ln.get("tts_text") or ""
        if kind == "montage":
            ev.append({"t": ln["start"], "type": "montage", "speaker": sp, "line": ln["id"]})
        if kind == "host_round" and "cold one" in (ln.get("display_text") or "").lower():
            ev.append({"t": phrase_time(ln, "crack", 0.3), "type": "crack", "line": ln["id"]})
        if kind == "host_open" and ln["id"] == "open-01":
            ev.append({"t": ln["start"], "type": "title", "line": ln["id"]})
        if kind == "host_clock":
            nxt = placed[i + 1] if i + 1 < len(placed) else None
            ev.append({"t": ln["start"], "type": "clock", "team": ln.get("focus"), "pick_no": ln.get("pick_no"),
                       "beat_end": nxt["start"] if nxt else ln["end"]})
        if kind == "host_pick" and ln.get("pick_no") in picks:
            p = picks[ln["pick_no"]]
            _reveal(ev, league, p, ln, name_time(ln, p.player_name))
        if kind in ("pick_statement", "on_air_call") and ln.get("pick_no") in picks \
                and ln.get("pick_no") not in host_called:
            p = picks[ln["pick_no"]]  # no host call: the GM's own call is the reveal (the slam lands on the name)
            t_name = call_name_time(ln, p.player_name)
            _reveal(ev, league, p, ln, t_name if t_name is not None else name_time(ln, p.player_name))
            host_called.add(p.pick_no)
        c = ln.get("card") or {}
        if kind in ("pick_statement", "on_air_call") and c.get("prev_pick") in picks and c.get("beat2_word"):
            b2 = beat2_time(ln) or ln["end"]
            prev = picks[c["prev_pick"]]
            if prev.team != sp and b2 > ln["start"] + 0.3:
                ev.append({"t": ln["start"], "type": "react", "team": prev.team, "speaker": sp, "line": ln["id"],
                           "until": round(b2, 4), "tone": c.get("tone") or "roast", "pick_no": prev.pick_no,
                           "pose": "celebrate" if c.get("tone") == "praise" else "shock",
                           "tight": bool(ln.get("beats"))})
        if T.has(text, "shouting") or (sp == "host" and T.has(text, "shouting")):
            ev.append({"t": ln["start"], "type": "shout", "speaker": sp, "line": ln["id"], "until": ln["end"]})
        if kind in ("reaction", "table_talk", "chirp", "grade_comment") or \
                (kind in ("montage", "hook") and ln.get("addressed_to")):
            ev.append({"t": ln["start"], "type": "roast", "speaker": sp, "target": ln.get("addressed_to"),
                       "line": ln["id"], "until": ln["end"], "funny": T.has(text, *T.FUNNY),
                       "praise": bool((ln.get("card") or {}).get("praise")),
                       "cutaway": ln.get("gap_hint") == "cutaway", "tight": bool(ln.get("beats"))})
        if ln.get("gap_hint") == "cutaway" and ln.get("addressed_to") in league.teams:
            nxt = placed[i + 1]["start"] if i + 1 < len(placed) else ln["end"] + 1.2
            praise = bool((ln.get("card") or {}).get("praise"))
            ev.append({"t": round(ln["end"] + 0.03, 4), "type": "cutaway", "team": ln["addressed_to"],
                       "until": round(max(ln["end"] + 0.5, nxt - 0.02), 4), "line": ln["id"],
                       "pose": "celebrate" if praise else "shock", "praise": praise})
        if kind == "host_grade" and (ln.get("card") or {}).get("letter"):
            c = ln["card"]
            ev.append({"t": phrase_time(ln, c.get("say_letter") or c["letter"], 0.8), "type": "stamp",
                       "team": c.get("team"), "letter": c["letter"], "line": ln["id"], "top": bool(c.get("top")),
                       "bottom": bool(c.get("bottom")),
                       "good": c["letter"][0] == "A", "bad": c["letter"][0] in "CDF"})
        if kind in ("pick_statement", "on_air_call"):
            ev.append({"t": ln["start"], "type": "call", "speaker": sp, "line": ln["id"], "until": ln["end"],
                       "hype": T.has(text, *T.HYPE) or kind == "on_air_call"})
        if kind == "bot_statement":
            ev.append({"t": ln["start"], "type": "bot", "line": ln["id"], "until": ln["end"]})
        if sp == "host" and kind in ("host_seg", "host_close") and T.has(text, "laughs", "chuckles"):
            ev.append({"t": ln["start"], "type": "joke", "line": ln["id"], "until": ln["end"]})
        if kind == "host_speed":
            dur = ln["end"] - ln["start"]
            for r in (ln.get("card") or {}).get("reveal") or []:
                ev.append({"t": round(ln["start"] + float(r["at"]) * dur, 4), "type": "tick",
                           "pick_no": r.get("pick_no")})
        if ln["id"] == "close-02":
            ev.append({"t": ln["end"], "type": "finale", "line": ln["id"]})
    ev += punch_events(placed)
    ev.sort(key=lambda e: e["t"])
    return ev


# --------------------------------------------------------------------------- stems

def _place(buf: np.ndarray, x: np.ndarray, t: float, sr: int, gain_db: float) -> None:
    s0 = int(round(t * sr))
    if s0 >= len(buf):
        return
    if s0 < 0:
        x = x[-s0:]
        s0 = 0
    n = min(len(x), len(buf) - s0)
    buf[s0:s0 + n] += x[:n] * np.float32(10 ** (gain_db / 20))


def sfx_cues(events: list[dict], style: str = "arena") -> list[tuple[float, str, float]]:
    """(time, kind, extra gain dB) for every sound effect. style 'party': a basement full of buddies -- the room
    cheers, groans and laughs; the arena horn is saved for the show's open and close."""
    out: list[tuple[float, str, float]] = []
    party = style == "party"
    for e in events:
        t, ty = e["t"], e["type"]
        if ty == "punch_beat":  # the room on the laugh beat, sized to the spice
            sp = int(e.get("spice") or 1)
            if e.get("praise"):
                out.append((t + 0.05, "cheer", -5.0))
            elif sp >= 3:
                out += [(t + 0.03, "ooh", -1.0), (t + 0.35, "laugh", -6.0)]
            else:
                out.append((t + 0.05, "laugh", -4.0 if sp == 2 else -9.0))
            continue
        if e.get("tight") and ty in ("react", "roast"):
            continue  # the punch beats carry the room on tight lines
        if ty == "reveal" and e.get("slam_t") is not None:
            t = float(e["slam_t"])
        if party and ty == "reveal":
            if e.get("flag"):
                out += [(t + 0.05, "scratch", -2.0), (t + 0.45, "ooh", 0.0)]
            else:
                out += [(t - 0.04, "impact", -8.0), (t, "cheer", 0.0 if e.get("big") else -3.0)]
            continue
        if party and ty == "react":
            out.append((e["until"] - 0.05, "laugh" if e.get("tone") != "praise" else "cheer", -4.0))
            continue
        if party and ty == "clock":
            continue
        if party and ty == "crack":
            out.append((t, "can", 0.0))
            continue
        if ty == "segment":
            out.append((t - 0.2, "whoosh", 0.0))
        elif ty == "montage":
            out.append((t - 0.18, "whoosh", 0.0))
        elif ty == "title":
            out += [(t - 0.35, "impact", 0.0), (t - 0.3, "roar", -2.0)]
        elif ty == "clock":
            out.append((t + 0.4, "murmur", 0.0))
        elif ty == "reveal":
            if e.get("flag"):  # reach / steal: the record scratch cuts the celebration short
                out += [(t - 0.04, "impact", 0.0), (t + 0.05, "scratch", 0.0), (t + 0.55, "ooh", 0.0)]
                continue
            out += [(t - 0.04, "impact", 0.0), (t, "roar", 0.0 if e.get("big") else -3.0)]
            out.append((t + 0.08, "horn" if e.get("big") else "organ", 0.0))
        elif ty == "roast":
            if e.get("praise"):
                out.append((e["until"] + 0.04, "cheer", -2.0))
            else:
                out.append((e["until"] + 0.04, "laugh" if e.get("funny") else "ooh", 0.0))
        elif ty == "cutaway":
            out.append((t - 0.02, "whoosh", -4.0))
        elif ty == "stamp":
            out.append((t - 0.03, "impact", 0.0))
            if e.get("top"):
                out += [(t, "roar", 0.0), (t + 0.08, "horn", 0.0)]
            elif e.get("good"):
                out += [(t, "cheer", 0.0), (t + 0.08, "organ", 0.0)]
            elif e.get("bad"):
                out += [(t + 0.05, "scratch", -2.0), (t + 0.3, "ooh", 0.0)]
            else:
                out.append((t + 0.1, "organ", -2.0))
        elif ty in ("bot", "joke"):
            out.append((e["until"] + 0.05, "rimshot", 0.0))
        elif ty == "tick":
            out.append((t, "tick", 0.0))
        elif ty == "finale":
            out += [(t - 0.1, "horn", 0.0), (t - 0.15, "roar", 0.0)]
    return out


def render_sfx(events: list[dict], n: int, sr: int, lib: SfxLibrary, style: str = "arena") -> tuple[np.ndarray, np.ndarray]:
    """-> (ducked_stem, dry_stem) stereo; the ducked stem is attenuated under voices by the mixer."""
    ducked = np.zeros((n, 2), dtype=np.float32)
    dry = np.zeros((n, 2), dtype=np.float32)
    counts: dict[str, int] = {}
    roomed: dict[tuple[str, int], np.ndarray] = {}
    for t, kind, extra in sfx_cues(events, style):
        k = counts.get(kind, 0)
        counts[kind] = k + 1
        x = lib.get(kind, k)
        if style == "party" and kind in ("cheer", "laugh", "ooh", "roar"):  # a basement of buddies, not an arena
            key = (kind, k % max(1, len(lib.files.get(kind) or [1])))
            if key not in roomed:
                f = np.fft.rfftfreq(len(x), 1 / sr)
                g = (1.0 / np.sqrt(1.0 + (f / 2600.0) ** 4)).astype(np.float32)[:, None]
                roomed[key] = np.fft.irfft(np.fft.rfft(x, axis=0) * g, n=len(x), axis=0).astype(np.float32) * np.float32(0.8)
            x = roomed[key]
        _place(ducked if kind in DUCKED else dry, x, t, sr, GAIN_DB.get(kind, -8.0) + extra)
    return ducked, dry


def loop_to(x: np.ndarray, n: int, xfade: int) -> np.ndarray:
    if len(x) >= n:
        return x[:n].copy()
    out = np.zeros((n, x.shape[1]), dtype=np.float32)
    pos, step = 0, len(x) - xfade
    ramp = np.linspace(0, 1, xfade, dtype=np.float32)[:, None]
    while pos < n:
        seg = x.copy()
        if pos > 0:
            seg[:xfade] *= ramp
        seg[-xfade:] *= ramp[::-1]
        m = min(len(seg), n - pos)
        out[pos:pos + m] += seg[:m]
        pos += step
    return out


def music_stem(segs: list[dict], total: float, sr: int, cfg: dict) -> tuple[np.ndarray, dict]:
    """Theme under the cold open and the close, bed looped under everything else (not yet ducked)."""
    m = cfg["mix"]
    n = int(round(total * sr))
    out = np.zeros((n, 2), dtype=np.float32)
    info: dict = {"theme": None, "bed": None}
    theme_p = ROOT / m["theme"] if m.get("theme") else None
    bed_p = ROOT / m["bed"] if m.get("bed") else None

    def load(p: Path, lufs: float) -> np.ndarray:
        x = audio.decode(p, sr=sr, channels=2)
        meas = audio.ebur128(x, sr)["I"]
        return x * np.float32(10 ** ((lufs - (meas if meas is not None else lufs)) / 20))

    kinds = {s["kind"] for s in segs}
    theme_win = []
    if theme_p and theme_p.exists():
        theme = load(theme_p, float(m["theme_lufs"]))
        fade = int(1.2 * sr)
        opener = segs[0] if segs and segs[0]["kind"] in ("cold_open", "hook", "highlights") else None
        if opener:
            end = min(opener["end"] + 1.5, len(theme) / sr)
            if m.get("theme_open_s"):
                end = min(end, float(m["theme_open_s"]))
            k = min(int(end * sr), n)
            seg = theme[:k].copy()
            f = min(fade, k // 2)
            seg[-f:] *= np.linspace(1, 0, f, dtype=np.float32)[:, None]
            out[:len(seg)] += seg
            theme_win.append((0.0, end))
        if "close" in kinds:
            cl = next(s for s in segs if s["kind"] == "close")
            start = max(cl["start"] - 0.5, total - len(theme) / sr)
            s0 = int(start * sr)
            seg = theme[: n - s0].copy()
            seg[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)[:, None]
            out[s0:s0 + len(seg)] += seg
            theme_win.append((start, total))
        elif m.get("theme_end_s"):  # shorts / highlight cuts: the theme's final hit under the end card
            k = min(int(float(m["theme_end_s"]) * sr), len(theme), n)
            seg = theme[-k:].copy()
            f = min(int(0.4 * sr), k // 2)
            seg[:f] *= np.linspace(0, 1, f, dtype=np.float32)[:, None]
            out[n - k:] += seg
            theme_win.append(((n - k) / sr, total))
        info["theme"] = rel(theme_p)
    if bed_p and bed_p.exists():
        bed = loop_to(load(bed_p, float(m["bed_lufs"])), n, int(0.8 * sr))
        gate = np.ones(n, dtype=np.float32)
        for a, b in theme_win:  # the bed steps aside while the theme plays
            i0, i1 = int(a * sr), int(b * sr)
            gate[i0:i1] = 0.0
        k = int(1.0 * sr)
        gate = np.convolve(gate, np.ones(k, dtype=np.float32) / k, mode="same")
        out += bed * gate[:, None]
        info["bed"] = rel(bed_p)
    return out, info


def crowd_stem(n: int, sr: int, lib: SfxLibrary, lufs: float, style: str = "arena") -> np.ndarray:
    bed = lib.get("crowd_bed")
    if style == "party":  # a room, not an arena: darker (gentle FFT low-pass on the loop) and quieter
        f = np.fft.rfftfreq(len(bed), 1 / sr)
        g = (1.0 / np.sqrt(1.0 + (f / 1400.0) ** 4)).astype(np.float32)[:, None]
        bed = np.fft.irfft(np.fft.rfft(bed, axis=0) * g, n=len(bed), axis=0).astype(np.float32)
        lufs -= 3.0
    x = loop_to(bed, n, int(1.0 * sr)) if len(bed) >= 2 * sr else np.tile(bed, (int(math.ceil(n / len(bed))), 1))[:n]
    return x * np.float32(10 ** ((lufs + 20.0) / 20))  # the library normalises to -20 LUFS

