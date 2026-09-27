"""Avatar frame sets and audio-driven mouth tracks.

Frame-set interface: every speaker ("host" + team ids) resolves to images per state:
    closed | mid (slightly open) | open | blink (eyes shut, mouth closed)       <- required
    mid_blink | open_blink (eyes shut while talking)                            <- optional
Sources, in priority order:
  1. media/avatars.json (overrides): {"speakers": {"qwen": {"closed": path, "mid": ..., "open": ..., "blink": ...,
     ["mid_blink": ..., "open_blink": ...]}}} (paths relative to the repo root)
  2. media/assets/avatars/manifest.json (production pixel set): teams.<id>.files = base/mid/wide/blink/
     mid_blink/wide_blink (paths relative to media/assets/avatars/)
  3. the placeholder generator: a flat face in the franchise colours with the GM's initials (dev only).

Mouth tracks: per-frame state from the speaker's amplitude envelope (hysteresis + minimum hold +
a one-frame visual lead), plus seeded periodic blinks while not talking. Output is run-length
encoded: [[frame, state], ...] with global show frame numbers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .paths import AVATAR_MANIFEST, AVATARS_JSON, ROOT, SHOW_ASSETS, cache_dir, rel

STATES = ("closed", "mid", "open", "blink", "mid_blink", "open_blink")  # index = mouth (0-2) + 3 * eyes_shut
REQUIRED = ("closed", "mid", "open", "blink")
TALK_BLINK_FALLBACK = {"mid_blink": "mid", "open_blink": "open"}
PRODUCTION_FILES = {"closed": "base", "mid": "mid", "open": "wide", "blink": "blink", "mid_blink": "mid_blink",
                    "open_blink": "wide_blink"}
PLACEHOLDER_VERSION = 5
POSES = ("hype", "point", "shock", "celebrate")
HOST_COLORS = ("#0E1B4D", "#E32402")


@dataclass
class FrameSet:
    speaker: str
    frames: dict[str, Path]
    style: str
    poses: dict[str, dict[str, Path]] | None = None  # pose -> {"open": path, "closed": path}

    def validate(self) -> None:
        missing = [s for s in REQUIRED if s not in self.frames or not Path(self.frames[s]).exists()]
        if missing:
            raise SystemExit(f"avatar frames for {self.speaker}: missing {missing}")

    @property
    def talk_blink(self) -> bool:
        return all(s in self.frames and Path(self.frames[s]).exists() for s in TALK_BLINK_FALLBACK)

    def path(self, state: str) -> Path:
        if state in self.frames and Path(self.frames[state]).exists():
            return Path(self.frames[state])
        return Path(self.frames[TALK_BLINK_FALLBACK.get(state, "closed")])


# --------------------------------------------------------------------------- colours

def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))  # type: ignore[return-value]


def luminance(c: tuple) -> float:
    def lin(v: float) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


# --------------------------------------------------------------------------- placeholder generator

def _font(size: int, weight: int = 800, width: int = 100) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(SHOW_ASSETS / "fonts" / "archivo-latin-standard-normal.woff2"), size)
    try:
        f.set_variation_by_axes([weight, width])
    except Exception:  # noqa: BLE001 - static fallback is fine for placeholders
        pass
    return f


def draw_placeholder(state: str, initials: str, primary: str, secondary: str, bot: bool = False,
                     size: int = 512) -> Image.Image:
    S = size / 512.0
    p, s = hex_rgb(primary), hex_rgb(secondary)
    navy, off = hex_rgb("#0E1B4D"), hex_rgb("#EFF0F5")
    bg = p if luminance(p) < 0.35 else mix(p, navy, 0.55)
    im = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(im)
    # suit-fabric pinstripes (a nod to the sponsor), very subtle
    stripe = mix(bg, off, 0.07)
    for x in range(-size, size * 2, int(22 * S)):
        d.line([(x, 0), (x + size, size)], fill=stripe, width=max(1, int(2 * S)))
    # shoulders / suit
    suit = mix(bg, (8, 12, 30), 0.55)
    d.ellipse([60 * S, 380 * S, 452 * S, 700 * S], fill=suit)
    d.polygon([(256 * S, 470 * S), (205 * S, 392 * S), (307 * S, 392 * S)], fill=off)  # shirt V
    d.polygon([(256 * S, 408 * S), (242 * S, 424 * S), (256 * S, 512 * S), (270 * S, 424 * S)], fill=s)  # tie
    lapel = mix(suit, s, 0.35)
    d.line([(205 * S, 392 * S), (250 * S, 500 * S)], fill=lapel, width=int(6 * S))
    d.line([(307 * S, 392 * S), (262 * S, 500 * S)], fill=lapel, width=int(6 * S))
    # head
    face = mix(s, off, 0.62) if luminance(s) < 0.6 else mix(s, off, 0.25)
    ink = mix(navy, (0, 0, 0), 0.3)
    if bot:
        d.rounded_rectangle([150 * S, 110 * S, 362 * S, 360 * S], radius=int(38 * S), fill=face)
        d.line([(256 * S, 110 * S), (256 * S, 70 * S)], fill=face, width=int(8 * S))
        d.ellipse([242 * S, 56 * S, 270 * S, 84 * S], fill=hex_rgb("#E32402"))
    else:
        d.ellipse([146 * S, 96 * S, 366 * S, 360 * S], fill=face)
    # eyes
    ey = 205 * S
    eyes_shut = state.endswith("blink")
    state = state.replace("_blink", "") if state != "blink" else "closed"
    for ex in (212 * S, 300 * S):
        if eyes_shut:
            d.line([(ex - 18 * S, ey + 4 * S), (ex + 18 * S, ey + 4 * S)], fill=ink, width=int(7 * S))
        elif bot:
            d.rectangle([ex - 16 * S, ey - 14 * S, ex + 16 * S, ey + 14 * S], fill=ink)
        else:
            d.ellipse([ex - 13 * S, ey - 16 * S, ex + 13 * S, ey + 16 * S], fill=ink)
    # mouth
    mx, my = 256 * S, 292 * S
    mouth_in = hex_rgb("#5A1010")
    if state == "closed":
        d.rounded_rectangle([mx - 34 * S, my - 4 * S, mx + 34 * S, my + 4 * S], radius=int(4 * S), fill=ink)
    elif state == "mid":
        d.ellipse([mx - 32 * S, my - 12 * S, mx + 32 * S, my + 12 * S], fill=ink)
        d.ellipse([mx - 25 * S, my - 7 * S, mx + 25 * S, my + 8 * S], fill=mouth_in)
    else:
        d.ellipse([mx - 36 * S, my - 26 * S, mx + 36 * S, my + 30 * S], fill=ink)
        d.ellipse([mx - 29 * S, my - 20 * S, mx + 29 * S, my + 24 * S], fill=mouth_in)
        d.rectangle([mx - 22 * S, my - 20 * S, mx + 22 * S, my - 12 * S], fill=off)  # teeth
    # initials badge
    f = _font(int(62 * S), 800, 112)
    tw = d.textlength(initials, font=f)
    bx0, bx1 = 256 * S - tw / 2 - 20 * S, 256 * S + tw / 2 + 20 * S
    d.rounded_rectangle([bx0, 416 * S, bx1, 492 * S], radius=int(14 * S), fill=navy, outline=s, width=int(4 * S))
    d.text((256 * S, 455 * S), initials, font=f, fill=off, anchor="mm")
    return im


def draw_pose(pose: str, mouth_open: bool, initials: str, primary: str, secondary: str, bot: bool = False,
              size: int = 512) -> Image.Image:
    """Placeholder gesture sprite: the flat face plus arms (hype / point / shock / celebrate)."""
    S = size / 512.0
    state = "open" if mouth_open else "closed"
    if pose == "celebrate":
        state = "open_blink" if mouth_open else "blink"
    im = draw_placeholder(state, initials, primary, secondary, bot, size)
    d = ImageDraw.Draw(im)
    p, s = hex_rgb(primary), hex_rgb(secondary)
    navy, off = hex_rgb("#0E1B4D"), hex_rgb("#EFF0F5")
    bg = p if luminance(p) < 0.35 else mix(p, navy, 0.55)
    suit = mix(bg, (8, 12, 30), 0.55)
    face = mix(s, off, 0.62) if luminance(s) < 0.6 else mix(s, off, 0.25)
    w = int(40 * S)

    def arm(a, b, fist=True):
        d.line([a, b], fill=suit, width=w)
        d.ellipse([a[0] - w / 2, a[1] - w / 2, a[0] + w / 2, a[1] + w / 2], fill=suit)
        if fist:
            r = 26 * S
            d.ellipse([b[0] - r, b[1] - r, b[0] + r, b[1] + r], fill=face, outline=mix(face, navy, 0.4), width=int(3 * S))

    L, R = (150 * S, 430 * S), (362 * S, 430 * S)
    if pose == "hype":
        arm(L, (96 * S, 250 * S))
        arm(R, (416 * S, 250 * S))
    elif pose == "point":
        arm(L, (120 * S, 520 * S), fist=False)
        arm(R, (470 * S, 330 * S))
        d.line([(478 * S, 322 * S), (512 * S, 300 * S)], fill=face, width=int(16 * S))
    elif pose == "shock":
        arm(L, (160 * S, 300 * S))
        arm(R, (352 * S, 300 * S))
        for ex in (212 * S, 300 * S):  # wide eyes
            d.ellipse([ex - 22 * S, 205 * S - 26 * S, ex + 22 * S, 205 * S + 26 * S], fill=off, outline=navy, width=int(5 * S))
            d.ellipse([ex - 8 * S, 205 * S - 8 * S, ex + 8 * S, 205 * S + 8 * S], fill=navy)
    elif pose == "celebrate":
        arm(L, (70 * S, 110 * S))
        arm(R, (442 * S, 110 * S))
        star = hex_rgb("#E32402")
        for cx, cy, r in ((70, 40, 16), (256, 50, 12), (450, 44, 18), (40, 200, 10), (480, 210, 11)):
            cx, cy, r = cx * S, cy * S, r * S
            d.polygon([(cx, cy - r), (cx + r * 0.3, cy - r * 0.3), (cx + r, cy), (cx + r * 0.3, cy + r * 0.3), (cx, cy + r),
                       (cx - r * 0.3, cy + r * 0.3), (cx - r, cy), (cx - r * 0.3, cy - r * 0.3)], fill=star)
    return im


class PlaceholderSource:
    style = "placeholder"

    def __init__(self, league):
        self.league = league

    def params(self, speaker: str) -> dict:
        if speaker == "host":
            return {"initials": "C", "primary": HOST_COLORS[0], "secondary": HOST_COLORS[1], "bot": False}
        t = self.league.teams[speaker]
        prim, sec = t.colors
        return {"initials": t.initials(), "primary": prim, "secondary": sec, "bot": t.is_bot}

    def frameset(self, speaker: str) -> FrameSet:
        prm = self.params(speaker)
        key = hashlib.sha1(json.dumps({**prm, "v": PLACEHOLDER_VERSION}, sort_keys=True).encode()).hexdigest()[:12]
        d = cache_dir("avatars", "placeholder", f"{speaker}-{key}")
        frames = {}
        for st in STATES:
            f = d / f"{st}.png"
            if not f.exists():
                draw_placeholder(st, prm["initials"], prm["primary"], prm["secondary"], prm["bot"]).save(f)
            frames[st] = f
        poses: dict[str, dict[str, Path]] = {}
        for pose in POSES:
            poses[pose] = {}
            for tag, mouth in (("open", True), ("closed", False)):
                f = d / f"pose-{pose}-{tag}.png"
                if not f.exists():
                    draw_pose(pose, mouth, prm["initials"], prm["primary"], prm["secondary"], prm["bot"]).save(f)
                poses[pose][tag] = f
        return FrameSet(speaker, frames, self.style, poses)


class ManifestSource:
    """Overrides (media/avatars.json) -> production manifest -> placeholders."""

    def __init__(self, league, fallback: PlaceholderSource):
        self.fallback = fallback
        self.league = league
        self.data = json.loads(AVATARS_JSON.read_text()) if AVATARS_JSON.exists() else {}
        self.prod = json.loads(AVATAR_MANIFEST.read_text()) if AVATAR_MANIFEST.exists() else {}

    def frameset(self, speaker: str) -> FrameSet:
        entry = (self.data.get("speakers") or {}).get(speaker)
        if entry:
            fs = FrameSet(speaker, {st: (ROOT / entry[st]).resolve() for st in STATES if entry.get(st)},
                          self.data.get("style", "custom"))
            fs.validate()
            return fs
        prod = ((self.prod.get("teams") or {}).get(speaker) or (self.prod.get("speakers") or {}).get(speaker))
        team = self.league.teams.get(speaker) if self.league else None
        if prod and team is not None and team.has_persona and prod.get("gm_name") and \
                str(prod["gm_name"]).strip().lower() != team.gm_name.strip().lower():
            prod = None  # art drawn for a different persona of this model: don't put the wrong face on air
        if prod and prod.get("files"):
            base = AVATAR_MANIFEST.parent
            frames = {st: (base / prod["files"][k]).resolve() for st, k in PRODUCTION_FILES.items()
                      if prod["files"].get(k) and (base / prod["files"][k]).exists()}
            if all(st in frames for st in REQUIRED):
                return FrameSet(speaker, frames, prod.get("style", "production"), production_poses(speaker, prod))
        return self.fallback.frameset(speaker)


def production_poses(speaker: str, prod: dict) -> dict[str, dict[str, Path]]:
    """media/assets/avatars/<id>/poses/{pose}.png (talking) + {pose}_closed.png; manifest 'poses' map also honoured."""
    base = AVATAR_MANIFEST.parent
    out: dict[str, dict[str, Path]] = {}
    listed = prod.get("poses") or {}
    for pose in POSES:
        cands = {"open": [listed.get(pose), f"{speaker}/poses/{pose}.png"],
                 "closed": [listed.get(f"{pose}_closed"), f"{speaker}/poses/{pose}_closed.png"]}
        got = {}
        for tag, paths in cands.items():
            for pth in paths:
                if pth and (base / pth).exists():
                    got[tag] = (base / pth).resolve()
                    break
        if "open" in got or "closed" in got:
            got.setdefault("open", got.get("closed"))
            got.setdefault("closed", got.get("open"))
            out[pose] = got
    return out


def load_framesets(league, speakers: list[str]) -> dict[str, FrameSet]:
    src = ManifestSource(league, PlaceholderSource(league))
    return {sp: src.frameset(sp) for sp in speakers}


def all_speakers(league) -> list[str]:
    return ["host"] + [t for t in league.teams]


# --------------------------------------------------------------------------- mouth tracks

def speaker_states(segments: list[dict], total_frames: int, cfg: dict, speaker: str,
                   talk_blink: bool = True) -> np.ndarray:
    """Per-frame state index (see STATES): mouth from the envelope, plus seeded blinks."""
    a = cfg["avatars"]
    mid_th, open_th = float(a["mid_threshold"]), float(a["open_threshold"])
    hold, lead = int(a["hold_frames"]), int(a["lead_frames"])
    mouth = np.zeros(total_frames, dtype=np.int8)  # 0 closed, 1 mid, 2 open
    talking = np.zeros(total_frames, dtype=bool)
    for seg in segments:
        v = np.asarray(seg["v"], dtype=np.float32)
        f0 = int(seg["f0"]) - lead
        cur, held, env = 0, hold, 0.0
        for i, x in enumerate(v):
            f = f0 + i
            if f < 0 or f >= total_frames:
                continue
            env = x if x > env else 0.55 * env + 0.45 * x  # fast attack, softer release
            if cur == 2:
                want = 2 if env >= open_th - 0.08 else (1 if env >= mid_th else 0)
            elif cur == 1:
                want = 2 if env >= open_th else (1 if env >= mid_th - 0.06 else 0)
            else:
                want = 2 if env >= open_th + 0.05 else (1 if env >= mid_th else 0)
            if want != cur and held >= hold:
                cur, held = want, 0
            else:
                held += 1
            mouth[f] = cur
            talking[f] = True
        end = f0 + len(v)
        if 0 <= end < total_frames:
            mouth[end] = 0
    eyes = np.zeros(total_frames, dtype=np.int8)
    rng = np.random.default_rng(int(hashlib.sha256(f"blink:{speaker}".encode()).hexdigest()[:8], 16))
    fps = int(cfg["fps"])
    lo, hi = cfg["avatars"]["blink_interval_s"]
    bl = int(cfg["avatars"]["blink_frames"])
    t = rng.uniform(0.5, hi)
    while t * fps < total_frames - bl:
        f = int(t * fps)
        for g in range(f, min(total_frames - bl, f + int(1.5 * fps))):
            if talk_blink or not talking[max(0, g - 2):g + bl + 2].any():
                eyes[g:g + bl] = 1
                break
        t += rng.uniform(lo, hi)
    return (mouth + 3 * eyes).astype(np.int8)


def rle(states: np.ndarray) -> list[list]:
    out: list[list] = []
    prev = None
    for f in np.nonzero(np.diff(np.concatenate([[-1], states])))[0]:
        s = int(states[f])
        if s != prev:
            out.append([int(f), STATES[s]])
            prev = s
    return out


def build_tracks(cues: dict, env: dict, cfg: dict, speakers: list[str], odir: Path,
                 framesets: dict[str, FrameSet] | None = None) -> dict:
    total = int(cues["total_frames"])
    tracks = {}
    tdir = odir / "avatar_tracks"
    tdir.mkdir(exist_ok=True)
    for sp in speakers:
        tb = framesets[sp].talk_blink if framesets and sp in framesets else True
        states = speaker_states(env["speakers"].get(sp, []), total, cfg, sp, tb)
        tracks[sp] = rle(states)
        (tdir / f"{sp}.json").write_text(json.dumps({"speaker": sp, "fps": cues["fps"], "total_frames": total,
                                                     "states": list(STATES), "track": tracks[sp]}))
    doc = {"schema": "gmbench.avatar_tracks/1", "fps": cues["fps"], "total_frames": total, "speakers": tracks}
    (odir / "avatar_tracks.json").write_text(json.dumps(doc))
    return doc


def contact_sheet(framesets: dict[str, FrameSet], out: Path, cell: int = 160) -> Path:
    sp = list(framesets)
    sheet = Image.new("RGB", (cell * len(STATES), cell * len(sp)), (14, 27, 77))
    for r, s in enumerate(sp):
        for c, st in enumerate(STATES):
            im = Image.open(framesets[s].path(st)).convert("RGB").resize((cell, cell), Image.LANCZOS)
            sheet.paste(im, (c * cell, r * cell))
    sheet.save(out)
    return out


def describe(framesets: dict[str, FrameSet]) -> dict:
    return {sp: {"style": fs.style, "talk_blink": fs.talk_blink, **{st: rel(p) for st, p in fs.frames.items()}}
            for sp, fs in framesets.items()}


def mouth_changes_count(tracks: dict) -> int:
    return sum(len(v) for v in tracks["speakers"].values())
