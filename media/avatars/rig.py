"""Layered character rigs (schema gds-rig/1, see media/assets/avatars/RIG_SCHEMA.md).

A rig = body frames (gesture clips, head pixel-identical in every frame) + face patches (mouth visemes and
eye/brow states) in native head-crop coordinates. This module holds everything that does not call a model:
native-grid detection, head box, patch extraction, the compositor, previews, QA and the mock rig built from the
old whole-image frames. rig_build.py does the generation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SCHEMA = "gds-rig/1"
VISEMES = ("rest", "closed", "small_open", "medium_open", "wide_open", "round", "teeth", "laugh", "smirk")
EYES = ("open", "blink", "half", "happy", "angry", "surprised", "skeptical", "look_left", "look_right")
GESTURES = ("idle_breathe", "talk_hands", "point", "fist_pump", "shrug", "laugh", "thumbs_up", "open_arms", "drink_sip",
            "facepalm", "react_shock", "react_happy")
MOODS = ("neutral", "happy", "angry", "surprised")
VISEME_FALLBACK = {"round": "medium_open", "teeth": "small_open", "laugh": "wide_open", "smirk": "rest",
                   "closed": "rest", "small_open": "medium_open", "medium_open": "wide_open"}
EYES_FALLBACK = {"half": "blink", "happy": "open", "angry": "open", "surprised": "open", "skeptical": "open",
                 "look_left": "open", "look_right": "open"}
GESTURE_FALLBACK = {"fist_pump": "point", "thumbs_up": "point", "open_arms": "shrug", "drink_sip": "idle_breathe",
                    "talk_hands": "idle_breathe", "laugh": "idle_breathe", "shrug": "idle_breathe",
                    "facepalm": "shrug", "react_shock": "shrug", "react_happy": "laugh",
                    "shock": "react_shock", "celebrate": "react_happy"}
OUT = 768


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================================ native grid
def detect_scale(a: np.ndarray) -> int:
    """Exact integer nearest-neighbour factor of an upscaled pixel-art frame (1 if none)."""
    h, w = a.shape[:2]
    for k in (8, 6, 4, 3, 2):  # largest exact factor wins (a x6 image is also x3 and x2)
        if h % k or w % k:
            continue
        small = a[::k, ::k]
        if np.array_equal(np.repeat(np.repeat(small, k, 0), k, 1), a):
            return k
    return 1


def load_native(path: Path | str) -> tuple[np.ndarray, int]:
    """RGB uint8 array at the native grid + the scale it was stored at."""
    a = np.asarray(Image.open(path).convert("RGB"))
    k = detect_scale(a)
    return np.ascontiguousarray(a[::k, ::k]), k


def to_native(path: Path | str, scale: int) -> np.ndarray:
    a = np.asarray(Image.open(path).convert("RGB"))
    k = detect_scale(a)
    if k == scale:
        return np.ascontiguousarray(a[::k, ::k])
    # not an exact upscale (raw model output): box-sample to the native grid
    n = a.shape[0] // scale
    return np.asarray(Image.fromarray(a).resize((n, n), Image.BOX))


# ============================================================================ head box
def head_size(native: int) -> int:
    return native // 2


def head_box(native: int, eye_box: list, mouth_box: list | None = None) -> list[int]:
    """Square box around the head: eye line at ~45% of the box height, centred on the eyes."""
    s = head_size(native)
    cx = (eye_box[0] + eye_box[2]) / 2
    eye_cy = (eye_box[1] + eye_box[3]) / 2
    x0 = int(round(cx - s / 2))
    y0 = int(round(eye_cy - 0.45 * s))
    x0 = max(0, min(native - s, x0))
    y0 = max(0, min(native - s, y0))
    return [x0, y0, x0 + s, y0 + s]


def crop(a: np.ndarray, box: list[int], offset=(0, 0)) -> np.ndarray:
    x0, y0, x1, y1 = box
    dx, dy = offset
    return a[y0 + dy : y1 + dy, x0 + dx : x1 + dx]


# ============================================================================ patches
def diff_mask(a: np.ndarray, b: np.ndarray, thr: int = 0) -> np.ndarray:
    return np.abs(a[..., :3].astype(np.int16) - b[..., :3].astype(np.int16)).max(axis=2) > thr


def make_patch(head: np.ndarray, edited: np.ndarray, region: np.ndarray | None = None) -> np.ndarray:
    """RGBA patch (head-crop size): the edited pixels that differ from the canonical head, alpha 0/255."""
    m = diff_mask(head, edited)
    if region is not None:
        m &= region
    out = np.zeros(head.shape[:2] + (4,), np.uint8)
    out[..., :3] = edited[..., :3]
    out[..., 3] = np.where(m, 255, 0)
    out[~m, :3] = 0
    return out


def empty_patch(box: list[int]) -> np.ndarray:
    return np.zeros((box[3] - box[1], box[2] - box[0], 4), np.uint8)


def save_rgba(a: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(a.astype(np.uint8), "RGBA" if a.shape[-1] == 4 else "RGB").save(path, optimize=True)


def load_rgba(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"))


def paste(canvas: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
    """Hard-alpha paste (alpha > 0 wins) with clipping."""
    h, w = patch.shape[:2]
    H, W = canvas.shape[:2]
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    dx1, dy1 = min(W, x + w), min(H, y + h)
    if dx1 <= dx0 or dy1 <= dy0:
        return
    p = patch[sy0 : sy0 + dy1 - dy0, sx0 : sx0 + dx1 - dx0]
    m = p[..., 3] > 0
    region = canvas[dy0:dy1, dx0:dx1]
    region[m] = p[m] if canvas.shape[-1] == 4 else p[m][..., :3]


# ============================================================================ rig access + compositor
class Rig:
    def __init__(self, rig_dir: Path):
        self.dir = Path(rig_dir)
        self.data = json.loads((self.dir / "rig.json").read_text())
        self._cache: dict[str, np.ndarray] = {}

    def img(self, rel: str | None) -> np.ndarray | None:
        if not rel:
            return None
        if rel not in self._cache:
            self._cache[rel] = load_rgba(self.dir / rel)
        return self._cache[rel]

    def viseme(self, mood: str, v: str) -> str | None:
        vis = self.data.get("visemes") or {}
        table = vis.get(mood) or vis.get("neutral") or {}
        seen = set()
        while v and v not in table and v not in seen:
            seen.add(v)
            v = self.data.get("viseme_fallback", VISEME_FALLBACK).get(v)
        return table.get(v) if v else None

    def eye(self, state: str) -> str | None:
        eyes = self.data.get("eyes") or {}
        seen = set()
        while state and state not in eyes and state not in seen:
            seen.add(state)
            state = self.data.get("eyes_fallback", EYES_FALLBACK).get(state)
        return eyes.get(state) if state else None

    def gesture(self, g: str) -> dict | None:
        gs = {**(self.data.get("gestures") or {}), **(self.data.get("transitions") or {})}
        seen = set()
        while g and g not in gs and g not in seen:
            seen.add(g)
            g = self.data.get("gesture_fallback", GESTURE_FALLBACK).get(g)
        return gs.get(g) if g else None

    def compose(self, gesture: str = "idle_breathe", frame: int = 0, eyes: str = "open", mood: str = "neutral",
                viseme: str = "rest", background: bool = True) -> np.ndarray:
        d = self.data
        W, H = d["native"]["w"], d["native"]["h"]
        canvas = np.zeros((H, W, 4), np.uint8)
        if background and d.get("background"):
            canvas[:] = self.img(d["background"])
        g = self.gesture(gesture) or self.gesture("idle_breathe")
        fr = g["frames"][frame % len(g["frames"])]
        body = self.img(fr["file"])
        m = body[..., 3] > 0
        canvas[m] = body[m]
        if fr.get("face", "rig") == "rig":
            box = d["head"]["box"]
            ox, oy = fr.get("head_offset") or (0, 0)
            for rel in (self.eye(eyes), self.viseme(mood, viseme)):
                p = self.img(rel)
                if p is not None:
                    paste(canvas, p, box[0] + ox, box[1] + oy)
            occ = self.img(fr.get("occluder"))
            if occ is not None:
                mo = occ[..., 3] > 0
                canvas[mo] = occ[mo]
        return canvas


def upscale(a: np.ndarray, k: int) -> Image.Image:
    img = Image.fromarray(a.astype(np.uint8), "RGBA" if a.shape[-1] == 4 else "RGB")
    return img.resize((img.width * k, img.height * k), Image.NEAREST)


# ============================================================================ previews
def _label(img: Image.Image, text: str, h: int = 18) -> Image.Image:
    out = Image.new("RGB", (img.width, img.height + h), (14, 27, 77))
    out.paste(img.convert("RGB"), (0, h))
    ImageDraw.Draw(out).text((5, 3), text, fill=(239, 240, 245))
    return out


def preview(rig_dir: Path, gif_scale: int = 2) -> dict:
    """preview.gif (flip-book: visemes, eye states, every gesture clip) + preview.png (contact sheet)."""
    rig = Rig(rig_dir)
    d = rig.data
    box = d["head"]["box"]
    frames: list[tuple[Image.Image, int]] = []

    def add(arr: np.ndarray, label: str, ms: int) -> None:
        frames.append((_label(upscale(arr[..., :3], gif_scale), label), ms))

    for mood in [m for m in MOODS if m in (d.get("visemes") or {})]:
        for v in VISEMES:
            add(rig.compose(viseme=v, mood=mood, eyes="happy" if v == "laugh" else ("open" if mood == "neutral" else mood)
                            if mood in d.get("eyes", {}) else "open"), f"{d['team']}  mouth[{mood}]: {v}", 450)
    for e in EYES:
        add(rig.compose(eyes=e), f"{d['team']}  eyes: {e}", 450)
    talk = ["small_open", "wide_open", "closed", "medium_open", "round", "small_open", "rest", "teeth"]
    for g in ("enter_from_base",) if (d.get("transitions") or {}).get("enter_from_base") else ():
        clip = d["transitions"][g]
        ms = int(1000 / max(1, clip.get("fps", 8)))
        add(rig.compose("idle_breathe", 0), f"{d['team']}  {g} (from base)", 400)
        for fi in range(len(clip["frames"])):
            add(rig.compose(g, fi), f"{d['team']}  {g} [{fi}]", ms)
        for fi in range(3):
            add(rig.compose("talk_hands", fi, viseme=talk[fi]), f"{d['team']}  talk_hands [{fi}]", 200)
    for e in ("look_left", "look_right"):
        if e in (d.get("eyes") or {}):
            add(rig.compose(eyes=e), f"{d['team']}  eyes: {e}  (SCREEN {e.split('_')[1]})", 700)
    for g in GESTURES:
        clip = (d.get("gestures") or {}).get(g)
        if not clip:
            continue
        seq = list(range(len(clip["frames"])))
        hold = clip.get("hold")
        if clip.get("loop"):
            seq = seq * 3
        elif hold is not None:
            seq = seq[: hold + 1] + [hold] * 5 + (list(range(hold - 1, -1, -1)) if clip.get("exit") == "reverse"
                                                  else list(range(hold + 1, len(clip["frames"]))))
        ms = int(1000 / max(1, clip.get("fps", 8)))
        for i, fi in enumerate(seq):
            eyes = "happy" if g == "laugh" else "open"
            vis = "laugh" if g == "laugh" else talk[i % len(talk)]
            if g == "drink_sip" or g == "idle_breathe":
                vis = "rest"
            add(rig.compose(g, fi, eyes=eyes, viseme=vis), f"{d['team']}  {g} [{fi}]", ms)
    imgs = [f for f, _ in frames]
    durs = [ms for _, ms in frames]
    gif = rig_dir / "preview.gif"
    pal_imgs = [im.convert("P", palette=Image.Palette.ADAPTIVE, colors=255) for im in imgs]
    pal_imgs[0].save(gif, save_all=True, append_images=pal_imgs[1:], duration=durs, loop=0, disposal=1, optimize=False)

    # contact sheet: head crops (x3) for every viseme / eye state, then every gesture frame (x1)
    z = 3 if box[2] - box[0] <= 96 else 2
    cells: list[Image.Image] = []
    for mood in [m for m in MOODS if m in (d.get("visemes") or {})]:
        for v in VISEMES:
            a = rig.compose(viseme=v, mood=mood)
            cells.append(_label(upscale(crop(a, box)[..., :3], z), f"{mood[:3]}:{v}"))
    for e in EYES:
        a = rig.compose(eyes=e)
        cells.append(_label(upscale(crop(a, box)[..., :3], z), f"eyes:{e}"))
    cw = max(c.width for c in cells)
    ch = max(c.height for c in cells)
    per = 9
    rows_face = (len(cells) + per - 1) // per
    body_cells = []
    for g in list(GESTURES) + list((d.get("transitions") or {}).keys())[:1]:
        clip = (d.get("gestures") or {}).get(g) or (d.get("transitions") or {}).get(g)
        if not clip:
            continue
        for i, fr in enumerate(clip["frames"]):
            a = rig.compose(g, i)
            tag = f"{g}[{i}]{' HOLD' if clip.get('hold') == i else ''}{' occ' if fr.get('occluder') else ''}"
            body_cells.append(_label(upscale(a[..., :3], 1), tag))
    bw = max((c.width for c in body_cells), default=0)
    bh = max((c.height for c in body_cells), default=0)
    bper = max(1, (cw * per) // max(1, bw))
    rows_body = (len(body_cells) + bper - 1) // bper
    sheet = Image.new("RGB", (max(cw * per, bw * bper), ch * rows_face + bh * rows_body + 30), (8, 12, 30))
    dr = ImageDraw.Draw(sheet)
    dr.text((6, 8), f"{d['team']}  kind={d.get('kind')}  native={d['native']['w']}x{d['native']['h']} x{d['native']['scale']}"
                    f"  face={d.get('face_type')}  qa={((d.get('qa') or {}).get('status'))}", fill=(239, 240, 245))
    for i, c in enumerate(cells):
        sheet.paste(c, ((i % per) * cw, 30 + (i // per) * ch))
    y0 = 30 + rows_face * ch
    for i, c in enumerate(body_cells):
        sheet.paste(c, ((i % bper) * bw, y0 + (i // bper) * bh))
    png = rig_dir / "preview.png"
    sheet.save(png, optimize=True)
    return {"preview": "preview.gif", "contact_sheet": "preview.png", "gif_frames": len(imgs)}


# ============================================================================ QA
def _lum(a: np.ndarray) -> np.ndarray:
    a = a[..., :3].astype(np.float32)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def _iris_x(img: np.ndarray, sel: np.ndarray) -> float | None:
    """x centroid of the iris/pupil inside an eye region: pixels much darker than the eye's white, ignoring lid and
    lash rows (dark runs across most of the eye's width). None if the eye shows no iris (a plain robot lens)."""
    ys, xs = np.nonzero(sel)
    if len(xs) < 4:
        return None
    lum = _lum(img)
    bright = np.percentile(lum[ys, xs], 90)
    thr = 0.55 * bright
    dark = sel & (lum < thr)
    for y in np.unique(ys):  # a lid line runs across (nearly) the whole eye: not the iris
        row = sel[y]
        if dark[y].sum() >= 0.7 * row.sum():
            dark[y] = False
    dy, dxs = np.nonzero(dark)
    if len(dxs) < 2:
        return None
    w = thr - lum[dy, dxs]
    return float((dxs * w).sum() / w.sum())


def glance_dx(head: np.ndarray, patch: np.ndarray, eyes_box: list[int], opening: np.ndarray | None = None,
              detail: bool = False):
    """Horizontal gaze shift of an eye patch against the canonical head, one value per eye (screen-left eye first), in
    native px: negative = the eyes look toward the SCREEN's LEFT edge, positive = toward its right edge.
    Primary: the iris/pupil centroid (dark pixels inside the eye opening, lid lines ignored). An eye whose canonical
    iris is not found (a robot lens) is measured against the bright lens centre. detail=True also returns the
    white-of-the-eye shift (what a renderer that tracks the visible white would see)."""
    m = patch[..., 3] > 0
    sprite = head[..., :3].copy()
    sprite[m] = patch[m][:, :3]
    hd = head[..., :3]

    def whitish(a: np.ndarray) -> np.ndarray:
        a = a.astype(np.int16)
        return (a.min(axis=2) > 165) & (a.max(axis=2) - a.min(axis=2) < 60)

    x0, y0, x1, y1 = eyes_box
    mid = (x0 + x1) / 2
    base_l = _lum(hd)
    iris, white = [], []
    for lo, hi in ((x0 - 2, mid), (mid, x1 + 2)):
        sel = np.zeros(m.shape, bool)
        sel[max(0, y0 - 2) : y1 + 2, max(0, int(lo)) : max(0, int(hi))] = True
        if opening is not None:
            sel &= opening
        if not (sel & m).any():
            iris.append(0.0)
            white.append(0.0)
            continue
        a, b = _iris_x(hd, sel), _iris_x(sprite, sel)
        if b is None:
            iris.append(0.0)
        else:
            if a is None:  # no iris in the canonical eye (lens): compare with the bright centre
                yy, xx = np.nonzero(sel)
                bl = base_l[yy, xx]
                w = np.maximum(0, bl - np.percentile(bl, 50))
                a = float((xx * w).sum() / w.sum()) if w.sum() > 0 else (lo + hi) / 2
            iris.append(round(b - a, 2))
        wo, wl = whitish(hd) & sel, whitish(sprite) & sel
        if wo.sum() >= 2 and wl.sum() >= 2:
            white.append(round(-(float(np.nonzero(wl)[1].mean()) - float(np.nonzero(wo)[1].mean())) * 2, 2))
        else:
            white.append(0.0)
    return (iris, white) if detail else iris


def glance_dir(dx: list[float], thr: float = 0.5) -> int:
    """-1 = screen-left, +1 = screen-right, 0 = no clear glance (both eyes must agree)."""
    if len(dx) == 2 and all(v <= -thr for v in dx):
        return -1
    if len(dx) == 2 and all(v >= thr for v in dx):
        return 1
    return 0


def eye_opening(blink_patch: np.ndarray | None) -> np.ndarray | None:
    """Where the eyes are open: the pixels a blink covers, grown by one pixel."""
    if blink_patch is None or not (blink_patch[..., 3] > 0).any():
        return None
    from scipy import ndimage as ndi
    return ndi.binary_dilation(blink_patch[..., 3] > 0, iterations=1)


def renderer_look_dirs(head_rgb: np.ndarray, looks: dict[str, np.ndarray]) -> dict[str, tuple[int, float]]:
    """The show renderer's own measure (media/gmbench_media/rig.py look_directions, reproduced exactly): the white of
    the eye in the box of pixels either look sprite changes, per half; white moving right = eyes looking screen-left.
    Returns {state: (dir, white_shift)} with dir -1 screen-left, +1 screen-right, 0 none."""
    op = head_rgb[..., :3].astype(int)
    looks = {k: v[..., :3].astype(int) for k, v in looks.items()}
    changed = np.zeros(op.shape[:2], bool)
    for a in looks.values():
        changed |= np.abs(a - op).max(axis=2) > 20
    if not changed.any():
        return {k: (0, 0.0) for k in looks}
    ys, xs = np.nonzero(changed)
    box = (max(0, ys.min() - 2), ys.max() + 3, max(0, xs.min() - 2), xs.max() + 3)

    def white_x(a):
        r = a[box[0] : box[1], box[2] : box[3]]
        mn, mx = r.min(axis=2), r.max(axis=2)
        m = (mn > 165) & (mx - mn < 60)
        w = r.shape[1]
        out = []
        for half in (m[:, : w // 2], m[:, w // 2 :]):
            yy, xx = np.nonzero(half)
            out.append(xx.mean() if len(xx) else None)
        return out

    o = white_x(op)
    res = {}
    for st, a in looks.items():
        d = [b - c for c, b in zip(o, white_x(a)) if c is not None and b is not None]
        dx = float(np.mean(d)) if d else 0.0
        res[st] = (-1 if dx > 0.3 else (1 if dx < -0.3 else 0), round(dx, 2))
    return res


def glance_check(rig: "Rig") -> dict:
    """Both measures on the shipped sprites: the renderer's white-of-the-eye measure (what the show uses to pick the
    glance) and the iris centroid. ok = the renderer reads the labelled direction and the iris does not contradict."""
    d = rig.data
    head = rig.img(d["head"]["image"])
    opening = eye_opening(rig.img((d.get("eyes") or {}).get("blink")))
    comp = {}
    for st in ("look_left", "look_right"):
        rel = (d.get("eyes") or {}).get(st)
        if rel:
            p = rig.img(rel)
            a = head[..., :3].copy()
            m = p[..., 3] > 0
            a[m] = p[m][:, :3]
            comp[st] = a
    rend = renderer_look_dirs(head, comp) if comp else {}
    res = {}
    for st, want in (("look_left", -1), ("look_right", 1)):
        if st not in comp:
            res[st] = {"present": False, "ok": True}
            continue
        iris = glance_dx(head, rig.img(d["eyes"][st]), d["head"]["eyes_box"], opening)
        idir = glance_dir(iris, 0.3)
        rdir, rdx = rend[st]
        res[st] = {"present": True, "renderer_dir": rdir, "renderer_white_shift": rdx, "iris_dx": iris,
                   "iris_dir": idir, "ok": rdir == want and idir != -want}
    return res


def head_check(rig: Rig) -> dict:
    """Pixel check: in every 'rig' body frame the head box (moved by head_offset) equals head.png, except where the
    frame's occluder draws. Returns the max number of differing pixels and the offending frames."""
    d = rig.data
    head = rig.img(d["head"]["image"])
    box = d["head"]["box"]
    worst, bad = 0, []
    for g, clip in (d.get("gestures") or {}).items():
        for i, fr in enumerate(clip["frames"]):
            if fr.get("face", "rig") != "rig":
                continue
            body = rig.img(fr["file"])
            ox, oy = fr.get("head_offset") or (0, 0)
            x0, y0 = box[0] + ox, box[1] + oy
            w, h = box[2] - box[0], box[3] - box[1]
            H, W = body.shape[:2]
            cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
            region = body[cy0:cy1, cx0:cx1, :3]
            ref = head[cy0 - y0 : cy1 - y0, cx0 - x0 : cx1 - x0]
            m = diff_mask(region, ref[..., :3]) & (ref[..., 3] > 0)
            occ = rig.img(fr.get("occluder"))
            if occ is not None:
                m &= ~(occ[cy0:cy1, cx0:cx1, 3] > 0)
            n = int(m.sum())
            if n:
                bad.append(f"{g}[{i}]: {n}px")
            worst = max(worst, n)
    return {"head_identical": worst == 0, "max_head_diff_px": worst, "head_diff_frames": bad}


def validate(rig_dir: Path) -> list[str]:
    """Structural check of a rig: every referenced file exists with the right size. [] = valid."""
    errs = []
    rig_dir = Path(rig_dir)
    d = json.loads((rig_dir / "rig.json").read_text())
    W, H = d["native"]["w"], d["native"]["h"]
    x0, y0, x1, y1 = d["head"]["box"]
    hw, hh = x1 - x0, y1 - y0

    def check(rel: str | None, size: tuple[int, int], what: str) -> None:
        if not rel:
            return
        p = rig_dir / rel
        if not p.exists():
            errs.append(f"{what}: missing {rel}")
            return
        got = Image.open(p).size
        if got != size:
            errs.append(f"{what}: {rel} is {got}, expected {size}")

    check(d.get("background"), (W, H), "background")
    check(d["head"]["image"], (hw, hh), "head")
    check(d["head"].get("face_mask"), (hw, hh), "face_mask")
    for mood, table in (d.get("visemes") or {}).items():
        for v, rel in table.items():
            check(rel, (hw, hh), f"viseme {mood}.{v}")
    for e, rel in (d.get("eyes") or {}).items():
        check(rel, (hw, hh), f"eyes {e}")
    for g, clip in list((d.get("gestures") or {}).items()) + list((d.get("transitions") or {}).items()):
        n = len(clip["frames"])
        if clip.get("hold") is not None and not 0 <= clip["hold"] < n:
            errs.append(f"gesture {g}: hold {clip['hold']} out of range")
        for ph, idx in (clip.get("phases") or {}).items():
            if any(not 0 <= i < n for i in idx):
                errs.append(f"gesture {g}: phase {ph} index out of range")
        for i, fr in enumerate(clip["frames"]):
            check(fr["file"], (W, H), f"gesture {g}[{i}]")
            check(fr.get("occluder"), (W, H), f"gesture {g}[{i}] occluder")
    return errs


# ============================================================================ mock rig (old whole-image frames)
MOCK_VISEMES = {"rest": None, "closed": None, "smirk": None, "small_open": "mid", "medium_open": "mid",
                "round": "mid", "teeth": "mid", "wide_open": "wide", "laugh": "wide"}
MOCK_POSES = {"fist_pump": "hype", "point": "point", "shock": "shock", "celebrate": "celebrate"}


def build_mock(team_dir: Path, meta: dict | None = None) -> dict:
    team_dir = Path(team_dir)
    meta = meta or json.loads((team_dir / "meta.json").read_text())
    rd = team_dir / "rig"
    rd.mkdir(exist_ok=True)
    base, k = load_native(team_dir / "base.png")
    n = base.shape[0]
    info = meta.get("info") or {}
    eye_box = info.get("eye_box") or [int(n * 0.4), int(n * 0.3), int(n * 0.6), int(n * 0.34)]
    box = head_box(n, eye_box, info.get("mouth_box"))
    head = crop(base, box)
    head_rgba = np.concatenate([head, np.full(head.shape[:2] + (1,), 255, np.uint8)], axis=2)
    save_rgba(head_rgba, rd / "head.png")
    save_rgba(np.concatenate([base, np.full(base.shape[:2] + (1,), 255, np.uint8)], axis=2), rd / "bg.png")
    save_rgba(np.concatenate([base, np.full(base.shape[:2] + (1,), 255, np.uint8)], axis=2), rd / "body" / "idle_breathe_0.png")

    patches: dict[str, np.ndarray] = {}
    for src in ("mid", "wide", "blink"):
        if (team_dir / f"{src}.png").exists():
            patches[src] = make_patch(head, crop(to_native(team_dir / f"{src}.png", k), box))
    visemes = {}
    for v, src in MOCK_VISEMES.items():
        rel = f"mouth/neutral/{v}.png"
        save_rgba(patches[src] if src in patches else empty_patch(box), rd / rel)
        visemes[v] = rel
    eyes = {"open": "eyes/open.png"}
    save_rgba(empty_patch(box), rd / "eyes/open.png")
    if "blink" in patches:
        save_rgba(patches["blink"], rd / "eyes/blink.png")
        eyes["blink"] = "eyes/blink.png"
    face_union = np.zeros(head.shape[:2], bool)
    for p in patches.values():
        face_union |= p[..., 3] > 0
    Image.fromarray((face_union * 255).astype(np.uint8), "L").save(rd / "face_mask.png")

    gestures = {"idle_breathe": {"fps": 3, "loop": True, "hold": None, "exit": "cut",
                                 "frames": [{"file": "body/idle_breathe_0.png", "head_offset": [0, 0], "occluder": None,
                                             "face": "rig"}]}}
    for g, pose in MOCK_POSES.items():
        p = team_dir / "poses" / f"{pose}.png"
        if not p.exists():
            continue
        a = to_native(p, k)
        rel = f"body/{g}_0.png"
        save_rgba(np.concatenate([a, np.full(a.shape[:2] + (1,), 255, np.uint8)], axis=2), rd / rel)
        gestures[g] = {"fps": 8, "loop": False, "hold": 0, "exit": "cut",
                       "frames": [{"file": rel, "head_offset": [0, 0], "occluder": None, "face": "baked"}]}

    mb = info.get("mouth_box")
    ys, xs = np.nonzero(face_union)

    def rel_box(b):
        return [b[0] - box[0], b[1] - box[1], b[2] - box[0], b[3] - box[1]] if b else None

    eb = rel_box(eye_box)
    rig = {
        "schema": SCHEMA, "team": meta.get("team", team_dir.name), "kind": "mock", "generated_at": now(),
        "face_type": meta.get("face_type", "human"),
        "native": {"w": n, "h": n, "scale": k, "out": n * k},
        "background": "bg.png", "body_alpha": "opaque", "pointing": "screen-left",
        "head": {
            "box": box, "image": "head.png", "face_mask": "face_mask.png",
            "mouth_anchor": [int(round((mb[0] + mb[2]) / 2 - box[0])), int(round((mb[1] + mb[3]) / 2 - box[1]))] if mb else None,
            "mouth_box": rel_box(mb),
            "eye_line": int(round((eb[1] + eb[3]) / 2)) if eb else None,
            "eyes_box": eb,
            "eye_centres": [[int(eb[0] + (eb[2] - eb[0]) * 0.25), int((eb[1] + eb[3]) / 2)],
                            [int(eb[0] + (eb[2] - eb[0]) * 0.75), int((eb[1] + eb[3]) / 2)]] if eb else None,
        },
        "visemes": {"neutral": visemes},
        "viseme_fallback": VISEME_FALLBACK,
        "eyes": eyes, "eyes_fallback": EYES_FALLBACK,
        "gestures": gestures, "gesture_fallback": {**GESTURE_FALLBACK, "shock": "idle_breathe", "celebrate": "fist_pump"},
        "qa": {"status": "mock", "viseme_source": "old mid/wide/blink frames",
               "flags": ["mock rig: 3-state mouth only, poses have baked faces (no lip-sync during poses)"]
               + [f for f in (meta.get("flags") or [])],
               "cost_usd": 0.0, "pixellab_generations": 0},
    }
    (rd / "rig.json").write_text(json.dumps(rig, indent=2))
    rig["qa"].update(head_check(Rig(rd)))
    rig["qa"].update(preview(rd))
    (rd / "rig.json").write_text(json.dumps(rig, indent=2))
    return rig


def register(out_root: Path) -> dict:
    """Add teams.<id>.rig / rig_kind to manifest.json for every <team>/rig/rig.json on disk."""
    mp = Path(out_root) / "manifest.json"
    man = json.loads(mp.read_text()) if mp.exists() else {"teams": {}}
    for team, entry in man.get("teams", {}).items():
        rj = Path(out_root) / team / "rig" / "rig.json"
        if rj.exists():
            r = json.loads(rj.read_text())
            entry["rig"] = f"{team}/rig/rig.json"
            entry["rig_kind"] = r.get("kind")
            entry["rig_status"] = (r.get("qa") or {}).get("status")
    man["updated"] = now()
    mp.write_text(json.dumps(man, indent=2))
    return man


if __name__ == "__main__":  # python rig.py mock [teams...]  |  python rig.py preview <team>
    import sys

    root = Path(__file__).resolve().parents[2] / "media" / "assets" / "avatars"
    cmd, *rest = sys.argv[1:] or ["mock"]
    if cmd == "mock":
        man = json.loads((root / "manifest.json").read_text())
        for t in rest or list(man["teams"]):
            if (root / t / "rig" / "rig.json").exists() and json.loads((root / t / "rig" / "rig.json").read_text()).get("kind") == "rig":
                print(f"{t}: real rig present, mock skipped")
                continue
            r = build_mock(root / t)
            print(f"{t}: mock rig  native={r['native']}  box={r['head']['box']}  head_ok={r['qa']['head_identical']}")
        register(root)
    elif cmd == "preview":
        for t in rest:
            print(preview(root / t / "rig"))
    elif cmd == "validate":
        man = json.loads((root / "manifest.json").read_text())
        for t in rest or list(man["teams"]):
            errs = validate(root / t / "rig")
            rj = json.loads((root / t / "rig" / "rig.json").read_text())
            gl = ""
            if rj.get("kind") == "rig":
                g = glance_check(Rig(root / t / "rig"))
                if not all(v.get("ok") for v in g.values()):
                    errs.append(f"glance check failed: {g}")
                gl = "  glance " + "  ".join(
                    f"{k}: " + (f"renderer {v['renderer_dir']:+d} (white {v['renderer_white_shift']:+.2f}) iris {v['iris_dx']}"
                                if v.get("present") else "absent") for k, v in g.items())
            print(f"{t}: {'OK' if not errs else errs}{gl}")
