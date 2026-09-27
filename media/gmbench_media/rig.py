"""Character rigs (gds-rig/1, see media/assets/avatars/RIG_SCHEMA.md) -> sprite sheets the renderer can drive.

A rig is layered: body frame (gesture clips, pixel-identical head) + eye/brow patch + mouth viseme patch +
occluder. The renderer composites them in the browser from four sprite sheets per character (body, occluder,
mouth, eyes) at the rig's native size, scaled up with nearest-neighbour; the animation driver (animate.py) says
which body frame, viseme and eye state each character shows on every frame.

When a speaker has no rig.json yet, a MOCK rig is built in memory from its old whole-image frames exactly the way
the schema describes (mouth patches = pixels where mid/wide differ from base, eyes = blink diff, idle = base, the
old poses = single baked frames), so the driver runs against the same structure either way.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .avatars import FrameSet
from .paths import AVATAR_MANIFEST, SHOW_GEN, cache_dir

VISEMES = ("rest", "closed", "small_open", "medium_open", "wide_open", "round", "teeth", "laugh", "smirk")
EYE_STATES = ("open", "blink", "half", "happy", "angry", "surprised", "skeptical", "look_left", "look_right")
MOODS = ("neutral", "happy", "angry", "surprised")
DEFAULT_VISEME_FALLBACK = {"round": "medium_open", "teeth": "small_open", "laugh": "wide_open", "smirk": "rest",
                           "closed": "rest", "small_open": "medium_open", "medium_open": "wide_open"}
DEFAULT_EYES_FALLBACK = {"half": "blink", "happy": "open", "angry": "open", "surprised": "open", "skeptical": "open",
                         "look_left": "open", "look_right": "open"}
DEFAULT_GESTURE_FALLBACK = {"fist_pump": "point", "thumbs_up": "point", "open_arms": "shrug", "drink_sip": "idle_breathe",
                            "talk_hands": "idle_breathe", "laugh": "idle_breathe", "shrug": "idle_breathe",
                            "point": "idle_breathe"}


@dataclass
class Clip:
    name: str
    fps: float
    loop: bool
    hold: int | None
    exit: str
    frames: list[dict]  # {file(Path), head_offset, occluder(Path|None), face}
    phases: dict[str, list[int]] | None = None  # {"in": [...], "hold": [...], "out": [...]} (real rigs)
    hold_fps: float | None = None               # cycle phases["hold"] at this rate while holding


@dataclass
class Rig:
    speaker: str
    kind: str
    native: tuple[int, int]
    scale: float
    head_box: tuple[int, int, int, int]
    visemes: dict[str, dict[str, Path | None]]
    eyes: dict[str, Path | None]
    gestures: dict[str, Clip]
    viseme_fallback: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VISEME_FALLBACK))
    eyes_fallback: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EYES_FALLBACK))
    gesture_fallback: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GESTURE_FALLBACK))
    pointing: str = "screen-left"
    background: Path | None = None

    def clip(self, name: str) -> Clip:
        if "|" in name:  # preference list: the first clip this rig actually has
            for n in name.split("|"):
                if n in self.gestures:
                    return self.gestures[n]
            name = name.split("|")[-1]
        seen = set()
        while name not in self.gestures and name not in seen:
            seen.add(name)
            name = self.gesture_fallback.get(name, "idle_breathe")
        return self.gestures.get(name) or self.gestures["idle_breathe"]

    def viseme(self, mood: str, v: str) -> tuple[str, str]:
        table = self.visemes.get(mood) or self.visemes["neutral"]
        seen = set()
        while v not in table and v not in seen:
            seen.add(v)
            v = self.viseme_fallback.get(v, "rest")
        if v not in table:
            table, mood = self.visemes["neutral"], "neutral"
            v = v if v in table else "rest"
        return (mood if self.visemes.get(mood) is table else "neutral"), v

    def eye(self, st: str) -> str:
        seen = set()
        while st not in self.eyes and st not in seen:
            seen.add(st)
            st = self.eyes_fallback.get(st, "open")
        return st if st in self.eyes else "open"


def look_directions(rig: "Rig") -> dict[str, int]:
    """Where each look sprite actually glances: -1 screen-left, +1 screen-right, 0 no clear glance. Measured, not
    trusted from the label (the first real rigs label look_left from the character's own left, and some have no
    screen-left glance at all): with the iris off-centre the visible WHITE of the eye shifts the other way, so the
    centroid of whitish pixels in the eye area, relative to the open eyes, gives the direction."""
    import numpy as np
    from PIL import Image
    try:
        base_fr = rig.gestures["idle_breathe"].frames[0]
        body = Image.open(base_fr["file"]).convert("RGBA")
        x0, y0, x1, y1 = rig.head_box
        hx, hy = base_fr["head_offset"]

        def comp(st: str):
            im = body.copy()
            p_ = rig.eyes.get(st)
            if p_:
                im.alpha_composite(Image.open(p_).convert("RGBA"), (x0 + hx, y0 + hy))
            return np.asarray(im.crop((x0 + hx, y0 + hy, x1 + hx, y1 + hy)).convert("RGB")).astype(int)

        op = comp("open")
        looks = {st: comp(st) for st in ("look_left", "look_right") if rig.eyes.get(st)}
        if not looks:
            return {}
        changed = np.zeros(op.shape[:2], bool)
        for a in looks.values():
            changed |= np.abs(a - op).max(axis=2) > 20
        if not changed.any():
            return {st: 0 for st in looks}
        ys, xs = np.nonzero(changed)
        box = (max(0, ys.min() - 2), ys.max() + 3, max(0, xs.min() - 2), xs.max() + 3)

        def white_x(a):
            r = a[box[0]:box[1], box[2]:box[3]]
            mn, mx = r.min(axis=2), r.max(axis=2)
            m = (mn > 165) & (mx - mn < 60)
            w = r.shape[1]
            out = []
            for half in (m[:, : w // 2], m[:, w // 2:]):  # each eye on its own
                yy, xx = np.nonzero(half)
                out.append(xx.mean() if len(xx) else None)
            return out

        o = white_x(op)
        res = {}
        for st, a in looks.items():
            d = [b - c for c, b in zip(o, white_x(a)) if c is not None and b is not None]
            dx = float(np.mean(d)) if d else 0.0
            res[st] = -1 if dx > 0.3 else (1 if dx < -0.3 else 0)
        return res
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- loading

def load_rig(speaker: str, fset: FrameSet | None) -> Rig | None:
    prod = json.loads(AVATAR_MANIFEST.read_text()) if AVATAR_MANIFEST.exists() else {}
    entry = ((prod.get("teams") or {}).get(speaker) or {})
    rp = entry.get("rig")
    path = (AVATAR_MANIFEST.parent / rp) if rp else (AVATAR_MANIFEST.parent / speaker / "rig" / "rig.json")
    if path.exists():
        return read_rig(speaker, path)
    return mock_rig(speaker, fset) if fset is not None else None


def read_rig(speaker: str, path: Path) -> Rig:
    d = json.loads(path.read_text())
    base = path.parent
    nat = d.get("native") or {}
    h = d.get("head") or {}
    rel = lambda p: (base / p) if p else None  # noqa: E731
    vis = {mood: {v: rel(p) for v, p in (tab or {}).items()} for mood, tab in (d.get("visemes") or {}).items()}
    eyes = {k: rel(p) for k, p in (d.get("eyes") or {}).items()}
    gestures = {}
    for name, g in (d.get("gestures") or {}).items():
        frames = [{"file": rel(f["file"]), "head_offset": tuple(f.get("head_offset") or (0, 0)),
                   "occluder": rel(f.get("occluder")), "face": f.get("face", "rig")} for f in g.get("frames") or []]
        if frames:
            ph = g.get("phases") or None
            if ph:  # keep only valid frame indices; a phase list may be empty (e.g. no out for exit=cut)
                ph = {k: [int(i) for i in (ph.get(k) or []) if 0 <= int(i) < len(frames)] for k in ("in", "hold", "out")}
                ph = ph if ph["hold"] else None
            gestures[name] = Clip(name, float(g.get("fps") or 8), bool(g.get("loop")), g.get("hold"),
                                  str(g.get("exit") or "cut"), frames, ph,
                                  float(g["hold_fps"]) if g.get("hold_fps") else None)
    return Rig(speaker, str(d.get("kind") or "rig"), (int(nat.get("w", 256)), int(nat.get("h", 256))),
               float(nat.get("scale", 3)), tuple(h.get("box") or (0, 0, 0, 0)), vis, eyes, gestures,
               {**DEFAULT_VISEME_FALLBACK, **(d.get("viseme_fallback") or {})},
               {**DEFAULT_EYES_FALLBACK, **(d.get("eyes_fallback") or {})},
               {**DEFAULT_GESTURE_FALLBACK, **(d.get("gesture_fallback") or {})},
               str(d.get("pointing") or "screen-left"), rel(d.get("background")))


def mock_rig(speaker: str, fset: FrameSet) -> Rig:
    """Schema-shaped mock from the old whole-image frames (cached as PNG patches)."""
    paths = {st: fset.path(st) for st in ("closed", "mid", "open", "blink")}
    key = hashlib.sha1("".join(f"{k}:{p}:{Path(p).stat().st_mtime_ns}" for k, p in sorted(paths.items())).encode()
                       + json.dumps({k: {t: str(v) for t, v in d.items()} for k, d in (fset.poses or {}).items()},
                                    sort_keys=True).encode()).hexdigest()[:12]
    d = cache_dir("rig-mock", f"{speaker}-{key}")
    base = np.asarray(Image.open(paths["closed"]).convert("RGBA"))
    H, W = base.shape[:2]
    diffs = {}
    for st in ("mid", "open", "blink"):
        im = np.asarray(Image.open(paths[st]).convert("RGBA"))
        if im.shape != base.shape:
            im = np.asarray(Image.open(paths[st]).convert("RGBA").resize((W, H), Image.NEAREST))
        diffs[st] = (np.abs(im.astype(np.int16) - base.astype(np.int16)).sum(axis=2) > 24, im)
    mask = diffs["mid"][0] | diffs["open"][0] | diffs["blink"][0]
    ys, xs = np.nonzero(mask)
    if len(xs) < 10:  # no face difference found: patch the centre
        x0, y0, x1, y1 = W // 4, H // 6, 3 * W // 4, H // 2
    else:
        pad = max(4, W // 64)
        x0, y0 = max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad)
        x1, y1 = min(W, int(xs.max()) + pad + 1), min(H, int(ys.max()) + pad + 1)

    def patch(st: str, name: str) -> Path | None:
        m, im = diffs[st]
        if not m.any():
            return None
        out = d / f"{name}.png"
        if not out.exists():
            p = np.zeros_like(im)
            p[m] = im[m]
            p[m, 3] = 255
            Image.fromarray(p[y0:y1, x0:x1]).save(out)
        return out

    mid, wide, blink = patch("mid", "m_mid"), patch("open", "m_wide"), patch("blink", "e_blink")
    visemes = {"neutral": {"rest": None, "closed": None, "smirk": None, "small_open": mid, "medium_open": mid,
                           "round": mid, "teeth": mid, "wide_open": wide, "laugh": wide}}
    eyes = {"open": None, "blink": blink}
    gestures = {"idle_breathe": Clip("idle_breathe", 3, True, None, "cut",
                                     [{"file": Path(paths["closed"]), "head_offset": (0, 0), "occluder": None,
                                       "face": "rig"}])}
    pose_map = {"hype": "fist_pump", "point": "point", "shock": "shock", "celebrate": "celebrate"}
    for pose, g in pose_map.items():
        pp = (fset.poses or {}).get(pose) or {}
        f = pp.get("open") or pp.get("closed")
        if f:
            gestures[g] = Clip(g, 8, False, 0, "cut", [{"file": Path(f), "head_offset": (0, 0), "occluder": None,
                                                         "face": "baked"}])
    return Rig(speaker, "mock", (W, H), 1.0, (x0, y0, x1, y1), visemes, eyes, gestures)


# --------------------------------------------------------------------------- sprite sheets

@dataclass
class Sheets:
    speaker: str
    native: tuple[int, int]
    head_box: tuple[int, int, int, int]
    url: dict[str, str]                      # body | occ | mouth | eyes -> asset URL (relative to show/assets/)
    cols: dict[str, int]
    body_index: dict[tuple[str, int], int]   # (clip, frame) -> cell
    occ_index: dict[tuple[str, int], int]    # (clip, frame) -> cell in occ sheet (0 = empty)
    mouth_index: dict[tuple[str, str], int]  # (mood, viseme) -> cell (0 = empty patch)
    eye_index: dict[str, int]                # state -> cell (0 = empty patch)
    patch_size: tuple[int, int]


def _grid(images: list[Image.Image | None], size: tuple[int, int], out: Path, max_w: int = 4096) -> int:
    w, h = size
    cols = max(1, min(len(images), max_w // max(1, w)))
    rows = max(1, math.ceil(len(images) / cols))
    sheet = Image.new("RGBA", (cols * w, rows * h), (0, 0, 0, 0))
    for k, im in enumerate(images):
        if im is None:
            continue
        if im.size != (w, h):
            im = im.resize((w, h), Image.NEAREST)
        sheet.paste(im, ((k % cols) * w, (k // cols) * h))
    sheet.save(out, optimize=True)
    return cols


def build_sheets(rig: Rig) -> Sheets:
    files: list[Path] = []
    for c in rig.gestures.values():
        for f in c.frames:
            files += [p for p in (f["file"], f["occluder"]) if p]
    for tab in rig.visemes.values():
        files += [p for p in tab.values() if p]
    files += [p for p in rig.eyes.values() if p]
    h = hashlib.sha1(("|".join(sorted(f"{p}:{Path(p).stat().st_mtime_ns}" for p in set(files)))
                      + json.dumps(rig.head_box)).encode()).hexdigest()[:10]
    d = SHOW_GEN / "rig" / f"{rig.speaker}-{h}"
    d.mkdir(parents=True, exist_ok=True)
    W, H = rig.native
    x0, y0, x1, y1 = rig.head_box
    pw, ph = max(1, x1 - x0), max(1, y1 - y0)
    body_imgs, body_index, occ_imgs, occ_index = [], {}, [None], {}
    for name, c in rig.gestures.items():
        for i, f in enumerate(c.frames):
            body_index[(name, i)] = len(body_imgs)
            body_imgs.append(f["file"])
            if f["occluder"]:
                occ_index[(name, i)] = len(occ_imgs)
                occ_imgs.append(f["occluder"])
            else:
                occ_index[(name, i)] = 0
    mouth_imgs, mouth_index = [None], {}
    for mood, tab in rig.visemes.items():
        for v, p in tab.items():
            if p:
                mouth_index[(mood, v)] = len(mouth_imgs)
                mouth_imgs.append(p)
            else:
                mouth_index[(mood, v)] = 0
    eye_imgs, eye_index = [None], {}
    for st, p in rig.eyes.items():
        if p:
            eye_index[st] = len(eye_imgs)
            eye_imgs.append(p)
        else:
            eye_index[st] = 0
    cols = {}
    names = {"body": (body_imgs, (W, H)), "occ": (occ_imgs, (W, H)), "mouth": (mouth_imgs, (pw, ph)),
             "eyes": (eye_imgs, (pw, ph))}
    for key, (paths, size) in names.items():
        out = d / f"{key}.png"
        cols_file = d / f"{key}.cols"
        if not out.exists() or not cols_file.exists():
            ims = [Image.open(p).convert("RGBA") if p else None for p in paths]
            cols[key] = _grid(ims, size, out, max_w=max(size[0], 8192 if key == "body" else 4096))
            cols_file.write_text(str(cols[key]))
        else:
            cols[key] = int(cols_file.read_text())
    url = {k: f"gen/rig/{d.name}/{k}.png" for k in names}
    return Sheets(rig.speaker, (W, H), rig.head_box, url, cols, body_index, occ_index, mouth_index, eye_index, (pw, ph))


def cell_xy(cell: int, cols: int, size: tuple[int, int]) -> tuple[int, int]:
    return -(cell % cols) * size[0], -(cell // cols) * size[1]
