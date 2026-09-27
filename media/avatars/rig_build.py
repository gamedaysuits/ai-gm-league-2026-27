"""Rig generation (the `--rig` stage of generate.py). Schema: media/assets/avatars/RIG_SCHEMA.md.

Consistent by construction:
  * FACE: the canonical head crop is edited by the image model as a 2x2 grid of identical copies (four mouth shapes
    or four eye states per call). Each cell is sampled back to the native grid, aligned to the canonical head on the
    untouched part of the face, snapped to the character's palette, and ONLY the changed pixels inside the mouth
    zone (or the eye/brow zone) are kept as a patch. Everything outside the zone is the canonical head, always.
  * BODY: gesture frames are full-frame edits of the base portrait (key pose first, then in-betweens drawn between
    two frames). Each frame is aligned to the base, snapped to the palette, and only the moving parts (arms, hands,
    props, revealed torso) are kept: head, background and the rest of the body are the base pixels. Hands that
    cross the face become an occluder layer, drawn over the face patches.
  * idle_breathe / laugh bob are programmatic 1-px moves of the character silhouette (from one chroma-key edit).
Every call is budgeted and logged to media/assets/avatars/costs.jsonl (variant "rig_*"); results are cached in
<team>/rig/raw/state.json, so a rerun only makes what is missing.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

import facelock
import generate as G
import prompts as P
import rig as R

HOLD_FLASH = 0.09
IMAGE_MODELS = {"pro": "google/gemini-3-pro-image", "flash": "google/gemini-3.1-flash-image"}
G.HOLD.setdefault(IMAGE_MODELS["flash"], HOLD_FLASH)
LOCK = threading.Lock()
RIG_VERSION = 7  # bump when frame processing changes: re-runs the (cheap) vision review on the next build


# ============================================================================ state (resume)
class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        with LOCK:
            self.data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2))


# ============================================================================ palette + grid sampling
def palette_of(*arrays: np.ndarray) -> np.ndarray:
    return np.unique(np.concatenate([a.reshape(-1, 3) for a in arrays]), axis=0)


def snap(a: np.ndarray, pal: np.ndarray) -> np.ndarray:
    """Nearest palette colour for every pixel (exact matches stay exact)."""
    flat = a.reshape(-1, 3).astype(np.int32)
    p = pal.astype(np.int32)
    out = np.empty_like(flat)
    for i in range(0, len(flat), 16384):
        chunk = flat[i : i + 16384]
        d = ((chunk[:, None, :] - p[None, :, :]) ** 2).sum(-1)
        out[i : i + 16384] = p[d.argmin(1)]
    return out.reshape(a.shape).astype(np.uint8)


def sample_native(img: np.ndarray, expect: float) -> np.ndarray:
    """Sample a model image back to its pixel grid. `expect` = expected pixel period (image px per native px)."""
    g = facelock.detect_grid(img)
    if g["ok"] and abs(g["px"] - expect) / expect < 0.25:
        return facelock.sample_cells(img, g)
    n = max(1, int(round(img.shape[0] / expect)))
    return np.asarray(Image.fromarray(img).resize((n, n), Image.BOX))


def best_shift(ref: np.ndarray, cand: np.ndarray, weight: np.ndarray, r: int = 6) -> tuple[int, int, float]:
    """Translation (dx, dy) of cand that best matches ref where weight is True. Returns (dx, dy, match_fraction)."""
    H, W = ref.shape[:2]
    best = (0, 0, -1.0)
    ch, cw = cand.shape[:2]
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            # cand pixel (y - dy, x - dx) lands on ref (y, x)
            y0, y1 = max(0, dy), min(H, ch + dy)
            x0, x1 = max(0, dx), min(W, cw + dx)
            if y1 - y0 < H * 0.6 or x1 - x0 < W * 0.6:
                continue
            a = ref[y0:y1, x0:x1].astype(np.int16)
            b = cand[y0 - dy : y1 - dy, x0 - dx : x1 - dx].astype(np.int16)
            w = weight[y0:y1, x0:x1]
            if w.sum() == 0:
                continue
            close = np.abs(a - b).max(axis=2) <= 28
            frac = float(close[w].mean())
            if frac > best[2]:
                best = (dx, dy, frac)
    return best


def place(cand: np.ndarray, shape: tuple, dx: int, dy: int, fill: np.ndarray) -> np.ndarray:
    """cand shifted by (dx, dy) into an array of `shape`, holes filled from `fill`."""
    out = fill.copy()
    H, W = shape[:2]
    ch, cw = cand.shape[:2]
    y0, y1 = max(0, dy), min(H, ch + dy)
    x0, x1 = max(0, dx), min(W, cw + dx)
    out[y0:y1, x0:x1] = cand[y0 - dy : y1 - dy, x0 - dx : x1 - dx]
    return out


# ============================================================================ face zones
def zones(rigd: dict, S: int) -> dict[str, np.ndarray]:
    """Boolean masks (head-crop coords): where mouth patches and eye/brow patches may paint. Disjoint."""
    hb = rigd["head"]
    eb, mb = hb["eyes_box"], hb["mouth_box"]
    u = S / 128.0
    eye = np.zeros((S, S), bool)
    mouth = np.zeros((S, S), bool)
    ex0, ey0, ex1, ey1 = eb
    ew = ex1 - ex0
    eye[max(0, int(ey0 - 14 * u)) : min(S, int(ey1 + 6 * u)), max(0, int(ex0 - 0.12 * ew)) : min(S, int(ex1 + 0.12 * ew))] = True
    mx0, my0, mx1, my1 = mb
    mw = mx1 - mx0
    top = max(int(ey1 + 7 * u), int(my0 - 2 * u))
    mouth[top : min(S, int(my1 + 3 * u)), max(0, int(mx0 - 0.12 * mw)) : min(S, int(mx1 + 0.12 * mw))] = True
    mouth &= ~eye
    return {"eye": eye, "mouth": mouth}


# ============================================================================ 2x2 face grids
CELL = 512


def grid_input(head: np.ndarray) -> tuple[Image.Image, int, int]:
    S = head.shape[0]
    f = CELL // S
    pad = (CELL - S * f) // 2
    up = np.repeat(np.repeat(head, f, 0), f, 1)
    cell = np.pad(up, ((pad, CELL - S * f - pad), (pad, CELL - S * f - pad), (0, 0)), mode="edge")
    grid = np.concatenate([np.concatenate([cell, cell], 1), np.concatenate([cell, cell], 1)], 0)
    return Image.fromarray(grid), f, pad


def grid_cells(out_img: np.ndarray, S: int, f: int, pad: int) -> list[np.ndarray]:
    """Split a 2x2 model output into 4 cell images (content area only), each ~S*f px."""
    h, w = out_img.shape[:2]
    k = w / (2 * CELL)
    cells = []
    for r in range(2):
        for c in range(2):
            y0 = int(round((r * CELL + pad) * k))
            x0 = int(round((c * CELL + pad) * k))
            size = int(round(S * f * k))
            m = int(round(3 * f * k))  # margin: lets the aligner find a slightly shifted face
            cy0, cx0 = max(0, y0 - m), max(0, x0 - m)
            cells.append((out_img[cy0 : min(h, y0 + size + m), cx0 : min(w, x0 + size + m)], (y0 - cy0, x0 - cx0)))
    return cells


MOUTH_SHAPES = {
    "closed": "lips pressed firmly together in a flat line, as when saying \"M\" or \"B\" - no gap, no teeth",
    "small_open": "lips slightly parted: a narrow dark gap with a hint of the upper teeth, as in \"eh\" or \"t\"",
    "medium_open": "mouth half open: upper teeth and the dark inside of the mouth visible, jaw dropped a little, as in \"ay\"",
    "wide_open": "mouth wide open, jaw dropped: upper teeth, tongue and the dark inside of the mouth clearly visible, as in a big \"AH!\"",
    "round": "lips pushed forward into a small round \"O\" (a small dark round opening), as in \"oo\" or \"w\"",
    "teeth": "upper front teeth pressed onto the lower lip, as when saying \"F\" or \"V\"",
    "laugh": "a big open-mouthed laugh: wide grin, upper teeth showing, mouth corners pulled up high",
    "smirk": "a closed-mouth smirk: lips together, ONE corner of the mouth (viewer's right) pulled up in a cocky half-smile",
}
ROBOT_MOUTH = {
    "closed": "jaw plate clamped shut, drawn as one thin dark horizontal seam line across the jaw plate where the mouth "
              "is (like lips pressed together for an M)", "small_open": "jaw plate open a crack: a thin dark gap",
    "medium_open": "jaw plate half open: a clear dark gap", "wide_open": "jaw plate dropped wide open: a big dark opening",
    "round": "mouth slot narrowed into a small rounded opening", "teeth": "jaw plate open a crack with the mouth-grille teeth showing",
    "laugh": "jaw plate wide open with the corners turned up into a happy grin shape",
    "smirk": "jaw plate shut with the mouth line tilted up on the viewer's right into a cocky smirk",
}
EYE_STATES = {
    "blink": "both eyes fully closed, as in the middle of a blink (upper eyelids all the way down)",
    "half": "both upper eyelids half closed (heavy-lidded, sleepy), eyes still looking at the viewer",
    "happy": "happy smiling eyes: lower lids pushed up into a cheerful squint, eyebrows relaxed and slightly raised",
    "angry": "angry glare: eyebrows pulled down hard and angled in toward the nose, eyes narrowed",
    "surprised": "surprised: eyebrows raised high, eyes opened wide with white showing around the irises",
    "skeptical": "skeptical: the eyebrow on the viewer's LEFT raised high and arched, the other eyebrow lowered, eyes a bit narrowed",
    "look_left": "both eyes glancing sideways toward the LEFT EDGE OF THE IMAGE: in each eye the iris and pupil sit in "
                 "the corner nearest the left edge of the picture, the white of the eye showing on the right side of "
                 "each iris (he looks at someone off-picture on the left); head, lids and brows unchanged",
    "look_left#2": "both eyes glancing toward the LEFT EDGE OF THE IMAGE, irises moved about halfway toward the corner "
                   "of each eye nearest the left edge of the picture; head, lids and brows unchanged",
    "look_right": "both eyes glancing sideways toward the RIGHT EDGE OF THE IMAGE: in each eye the iris and pupil sit in "
                  "the corner nearest the right edge of the picture, the white of the eye showing on the left side of "
                  "each iris (he looks at someone off-picture on the right); head, lids and brows unchanged",
    "look_right#2": "both eyes glancing toward the RIGHT EDGE OF THE IMAGE, irises moved about halfway toward the corner "
                    "of each eye nearest the right edge of the picture; head, lids and brows unchanged",
}
ROBOT_EYES = {
    "blink": "both eye shutters fully closed over the lenses", "half": "both eye shutters half closed",
    "happy": "eye lenses shaped into happy upward arcs (shutters pushed up from below)",
    "angry": "eye shutters slanted down toward the centre in an angry scowl",
    "surprised": "eye lenses opened extra wide and bright, shutters raised high",
    "skeptical": "the shutter on the viewer's LEFT raised high, the other lowered halfway",
    "look_left": "a small black pupil in each lens, placed at the side of the lens nearest the LEFT EDGE OF THE IMAGE",
    "look_left#2": "a small black pupil in each lens, halfway between the lens centre and the side nearest the LEFT EDGE "
                   "OF THE IMAGE",
    "look_right": "a small black pupil in each lens, placed at the side of the lens nearest the RIGHT EDGE OF THE IMAGE",
    "look_right#2": "a small black pupil in each lens, halfway between the lens centre and the side nearest the RIGHT "
                    "EDGE OF THE IMAGE",
}
FACE_SETS = {
    "mouth_a": ("mouth", ["closed", "small_open", "medium_open", "wide_open"]),
    "mouth_b": ("mouth", ["round", "teeth", "laugh", "smirk"]),
    "eyes_a": ("eye", ["blink", "half", "happy", "angry"]),
    "eyes_b": ("eye", ["surprised", "skeptical", "look_left", "look_right"]),
    "mouth_happy": ("mouth", ["happy:rest", "happy:small_open", "happy:medium_open", "happy:wide_open"]),
    "mouth_angry": ("mouth", ["angry:rest", "angry:small_open", "angry:medium_open", "angry:wide_open"]),
    "eyes_glance": ("eye", ["look_left", "look_left#2", "look_right", "look_right#2"]),  # SCREEN directions, measured
    # on demand only (not part of the first pass):
    "mouth_fix_closed": ("mouth", ["closed#1", "closed#2", "closed#3", "closed#4"]),
}
ON_DEMAND = {"mouth_fix_closed"}
GLANCES = ("look_left", "look_right")
MOOD_MOUTH = {  # talking while grinning / while angry: same openings, different lips
    "happy": {"rest": "a closed-mouth grin: lips together, both mouth corners pulled up high in a big friendly smile",
              "small_open": "grinning while talking: mouth corners pulled up, lips slightly parted showing the upper teeth",
              "medium_open": "grinning while talking: a big smile, mouth half open, upper teeth and the dark inside of the "
                             "mouth visible",
              "wide_open": "a huge open grin while shouting: mouth wide open and smiling, upper teeth and tongue visible"},
    "angry": {"rest": "a tight angry frown: lips pressed together, mouth corners pulled down",
              "small_open": "an angry snarl while talking: lips slightly parted, teeth clenched and showing, corners down",
              "medium_open": "angry talking: mouth half open in a scowl, teeth showing, corners pulled down",
              "wide_open": "angry yelling: mouth wide open and square, teeth and the dark inside of the mouth visible"},
}
POS = ["TOP-LEFT", "TOP-RIGHT", "BOTTOM-LEFT", "BOTTOM-RIGHT"]


def face_grid_prompt(kind: str, zone: str, names: list[str], retry: bool = False) -> str:
    robot = kind == "robot"
    if zone == "mouth":
        table = dict(ROBOT_MOUTH if robot else MOUTH_SHAPES)
        for mood, shapes in MOOD_MOUTH.items():
            for v, txt in shapes.items():
                table[f"{mood}:{v}"] = (f"{'jaw plate ' if robot else ''}{txt}" if not robot else
                                        {"rest": "jaw plate shut", "small_open": "jaw plate open a crack",
                                         "medium_open": "jaw plate half open", "wide_open": "jaw plate wide open"}[v]
                                        + (" with the mouth line curved up in a happy grin" if mood == "happy" else
                                           " with the mouth line slanted down in an angry scowl"))
        part = P.FACE[kind]["mouth_region"]
        keep = ("the eyes, eyebrows, nose, hair, facial hair, glasses, ears, skin and everything above the mouth"
                if not robot else "the eye lenses, shutters, antenna, faceplate and everything above the mouth")
        what = f"Edit ONLY the {part} in each copy (the jaw and chin may move a little for the open shapes)"
    else:
        table = ROBOT_EYES if robot else EYE_STATES
        part = "eyes and eyebrows" if not robot else "eye lenses and shutters"
        keep = ("the mouth, moustache or beard, nose, hair, glasses, ears, skin and everything below the eyes"
                if not robot else "the mouth, faceplate, antenna and everything below the eyes")
        what = f"Edit ONLY the {part} in each copy"
    lines = "; ".join(f"{POS[i]} copy: {table.get(n) or table[n.split('#')[0]]}" for i, n in enumerate(names))
    text = (
        "This image is a 2x2 grid of four IDENTICAL copies of the same pixel-art character's face (a 16-bit "
        "video-game portrait drawn with big square pixels). It is an expression sheet for animation. "
        f"{what}: {lines}. "
        f"Keep {keep} exactly as they are in every copy - pixel for pixel, same position, same size, same "
        "pixel grid, same palette, same outlines and shading. Keep the 2x2 layout, the four copies exactly where they "
        "are and the same size, and nothing between them. Hard-edged square pixels only: no anti-aliasing, no blur, "
        "no new colours if avoidable. No text, labels, numbers, borders or watermarks."
    )
    if retry:
        text += (" IMPORTANT: the previous attempt moved or redrew other parts of the faces. Only the " + part +
                 " may change; every other pixel must stay exactly as in the input.")
    return text


def sheet_changes(ctx, set_names: list[str]) -> tuple[np.ndarray, int]:
    """How many aligned cells changed each head-crop pixel, over every cached attempt of these face sheets."""
    S = ctx.head.shape[0]
    acc = np.zeros((S, S), np.float32)
    n = 0
    for set_name in set_names:
        for f in raw_files(ctx, f"face_{set_name}"):
            out_img = np.asarray(Image.open(f).convert("RGB"))
            k = out_img.shape[1] / (2 * CELL)
            period = ctx.f * k
            for cimg, (oy, ox) in grid_cells(out_img, S, ctx.f, ctx.pad):
                nat = sample_native(cimg, period)
                dy0, dx0 = int(round(oy / period)), int(round(ox / period))
                dx, dy, frac = best_shift(ctx.head, nat, np.ones((S, S), bool), r=4 + max(dx0, dy0))
                if frac < 0.85:
                    continue
                q = snap(place(nat, ctx.head.shape, dx, dy, ctx.head), ctx.palette)
                acc += R.diff_mask(q, ctx.head, 40)
                n += 1
    return acc, n


def _blob_box(mask: np.ndarray, pair: bool = False, pad: int = 1) -> list[int] | None:
    """Bounding box of the biggest blob (or the two biggest, e.g. two eyes) of a boolean mask."""
    lab, k = ndi.label(ndi.binary_closing(mask, iterations=1), structure=np.ones((3, 3), bool))
    if not k:
        return None
    sizes = ndi.sum(mask, lab, np.arange(1, k + 1))
    order = np.argsort(-sizes)
    keep = [order[0] + 1]
    if pair and len(order) > 1 and sizes[order[1]] >= 0.3 * sizes[order[0]]:
        keep.append(order[1] + 1)
    boxes = ndi.find_objects(lab)
    y0 = min(boxes[j - 1][0].start for j in keep) - pad
    y1 = max(boxes[j - 1][0].stop for j in keep) + pad
    x0 = min(boxes[j - 1][1].start for j in keep) - pad
    x1 = max(boxes[j - 1][1].stop for j in keep) + pad
    S = mask.shape[0]
    return [max(0, x0), max(0, y0), min(S, x1), min(S, y1)]


def calibrate_zones(ctx) -> dict:
    """Place the eye and mouth zones where the face sheets consistently changed the face (the base stage's boxes can
    be off: the host's old mouth edits mostly moved his chin). Falls back to the base-stage boxes."""
    S = ctx.head.shape[0]
    u = S / 128.0
    info = {}
    eyes_acc, ne = sheet_changes(ctx, ["eyes_a", "eyes_b", "eyes_glance"])
    mouth_acc, nm = sheet_changes(ctx, ["mouth_a", "mouth_b", "mouth_happy", "mouth_angry"])
    hb = ctx.rigd["head"]
    eb, mb = list(hb["eyes_box"]), list(hb["mouth_box"])
    if ne >= 4:
        m = eyes_acc >= max(2, 0.3 * ne)
        m[: int(0.12 * S)] = False
        m[int(0.7 * S) :] = False
        box = _blob_box(m, pair=True)
        if box and (box[2] - box[0]) <= 0.8 * S and (box[3] - box[1]) <= 0.35 * S:
            eb = box
            info["eyes_from_sheets"] = box
    if nm >= 4:
        m = mouth_acc >= max(2, 0.3 * nm)
        m[: min(S - 1, eb[3] + int(2 * u))] = False
        box = _blob_box(m)
        if box and (box[2] - box[0]) <= 0.6 * S and (box[3] - box[1]) <= 0.35 * S:
            mb = box
            info["mouth_from_sheets"] = box
    ctx.rigd["head"].update(eyes_box=eb, mouth_box=mb)
    eye = np.zeros((S, S), bool)
    mouth = np.zeros((S, S), bool)
    eye[max(0, int(eb[1] - 5 * u)) : min(S, int(eb[3] + 2 * u)), max(0, int(eb[0] - 3 * u)) : min(S, int(eb[2] + 3 * u))] = True
    mouth[max(0, int(mb[1] - 2 * u)) : min(S, int(mb[3] + 5 * u)), max(0, int(mb[0] - 4 * u)) : min(S, int(mb[2] + 4 * u))] = True
    mouth &= ~eye
    ctx.zones = {"eye": eye, "mouth": mouth}
    ctx.face_zone_canvas = np.zeros(ctx.base.shape[:2], bool)
    ctx.face_zone_canvas[ctx.box[1]:ctx.box[3], ctx.box[0]:ctx.box[2]] = eye | mouth
    return info


def extract_patches(ctx, set_name: str, raw_file: Path) -> dict:
    """Turn one 2x2 grid output into patches for its four states. Returns {state: {"patch", "metrics"}}."""
    zone_name, names = FACE_SETS[set_name]
    head = ctx.head
    S = head.shape[0]
    zmask = ctx.zones[zone_name]
    other = ctx.zones["mouth" if zone_name == "eye" else "eye"]
    out_img = np.asarray(Image.open(raw_file).convert("RGB"))
    k = out_img.shape[1] / (2 * CELL)
    res = {}
    # reference region for alignment: the whole head crop minus a grown edit zone
    ref_w = ~ndi.binary_dilation(zmask, iterations=3)
    for i, ((cimg, (oy, ox)), name) in enumerate(zip(grid_cells(out_img, S, ctx.f, ctx.pad), names)):
        period = ctx.f * k
        nat = sample_native(cimg, period)
        # the content starts at (oy, ox) image px inside the cell crop -> native offset
        dy0, dx0 = int(round(oy / period)), int(round(ox / period))
        dx, dy, frac = best_shift(head, nat, ref_w, r=4 + max(dx0, dy0))
        aligned = place(nat, head.shape, dx, dy, head)
        q = snap(aligned, ctx.palette)
        strong = R.diff_mask(q, head, 40) & zmask
        lab, n = ndi.label(strong, structure=np.ones((3, 3), bool))
        region = np.zeros_like(strong)
        if n:
            sizes = ndi.sum(strong, lab, np.arange(1, n + 1))
            keep = [j + 1 for j, s in enumerate(sizes) if s >= max(2, 0.08 * sizes.max())]
            region = np.isin(lab, keep)
            region = ndi.binary_fill_holes(ndi.binary_dilation(region, structure=np.ones((3, 3), bool), iterations=1))
            region &= zmask
        patch = R.make_patch(head, q, region)
        # drift: how much of the NON-edit face changed (after alignment + palette snap)
        face_ref = ref_w & ~other
        drift = float((R.diff_mask(q, head, 40) & face_ref).mean())
        changed = int((patch[..., 3] > 0).sum())
        res[name] = {"patch": patch, "metrics": {"align_match": round(frac, 3), "shift": [dx + dx0, dy + dy0],
                                                  "drift_pct": round(drift * 100, 2), "changed_px": changed,
                                                  "grid_period": round(period, 2)}}
    return res


# ============================================================================ context
class Ctx:
    def __init__(self, args, team: str, budget: "G.Budget"):
        self.args, self.team, self.budget = args, team, budget
        self.tdir = args.out / team
        self.rd = self.tdir / "rig"
        self.raw = self.rd / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        (self.tdir / "raw").mkdir(exist_ok=True)
        self.meta = json.loads((self.tdir / "meta.json").read_text())
        self.kind = self.meta.get("face_type") or "human"
        self.state = State(self.raw / "state.json")
        self.base, self.scale = R.load_native(self.tdir / "base.png")
        n = self.base.shape[0]
        info = self.meta.get("info") or {}
        if not info.get("eye_box"):  # no face-lock boxes (e.g. an older base): a centred guess, the sheets refine it
            info["eye_box"] = [int(n * 0.40), int(n * 0.28), int(n * 0.60), int(n * 0.32)]
        self.box = R.head_box(n, info["eye_box"], info.get("mouth_box"))
        self.head = R.crop(self.base, self.box).copy()
        S = self.head.shape[0]
        self.f, self.pad = CELL // S, (CELL - S * (CELL // S)) // 2
        olds = [R.to_native(self.tdir / f"{v}.png", self.scale) for v in ("mid", "wide", "blink") if (self.tdir / f"{v}.png").exists()]
        self.palette = palette_of(self.base, *olds)
        eb = info["eye_box"]
        mb = info.get("mouth_box") or [eb[0], eb[3] + 10, eb[2], eb[3] + 30]
        x0, y0 = self.box[0], self.box[1]
        self.rigd = {"head": {"eyes_box": [eb[0] - x0, eb[1] - y0, eb[2] - x0, eb[3] - y0],
                              "mouth_box": [mb[0] - x0, mb[1] - y0, mb[2] - x0, mb[3] - y0]}}
        self.zones = zones(self.rigd, S)
        self.face_zone_canvas = np.zeros(self.base.shape[:2], bool)
        self.face_zone_canvas[self.box[1]:self.box[3], self.box[0]:self.box[2]] = self.zones["eye"] | self.zones["mouth"]
        sp = self.raw / "silhouette.png"
        self.sil = (np.asarray(Image.open(sp).convert("L")) > 127) if sp.exists() else None

    def model(self, which: str) -> str:
        return IMAGE_MODELS.get(which, which)

    def call_image(self, variant: str, images: list[Image.Image | Path], text: str, model: str,
                   retry: str | None = None, image_size: str | None = None) -> Path | None:
        content = []
        for im in images:
            if isinstance(im, Path):
                content.append({"type": "image_url", "image_url": {"url": G.data_url(im)}})
            else:
                p = self.raw / f"_in_{variant}.png"
                im.save(p)
                content.append({"type": "image_url", "image_url": {"url": G.data_url(p)}})
        content.append({"type": "text", "text": text})
        res = G.call_image(model, content, self.budget, image_size=image_size)
        file = None
        if res["ok"]:
            n = 1
            while any(self.raw.glob(f"{variant}_{n}.*")):
                n += 1
            path = self.raw / f"{variant}_{n}.{res['ext']}"
            path.write_bytes(res["bytes"])
            file = path
        G.record_call(self.args, self.team, f"rig_{variant}", res, text, retry,
                      str(file.relative_to(self.tdir)) if file else None)
        G.log(f"  [{self.team}] rig {variant}{' retry:' + retry if retry else ''} {model.split('/')[-1]} "
              f"${res.get('cost', 0):.3f} {res.get('elapsed_s', 0):.0f}s -> {file.name if file else 'FAILED ' + str(res.get('error'))}"
              f"   (run total ${self.budget.spent:.2f})")
        return file


# ============================================================================ body sheets (2x2 at 2K: four full-res frames per call)
BCELL = 1024


def body_grid_input(base: np.ndarray) -> tuple[Image.Image, int, int]:
    n = base.shape[0]
    f = BCELL // n
    pad = (BCELL - n * f) // 2
    up = np.repeat(np.repeat(base, f, 0), f, 1)
    cell = np.pad(up, ((pad, BCELL - n * f - pad), (pad, BCELL - n * f - pad), (0, 0)), mode="edge")
    grid = np.concatenate([np.concatenate([cell, cell], 1), np.concatenate([cell, cell], 1)], 0)
    return Image.fromarray(grid), f, pad


def body_cells(out_img: np.ndarray, n: int, f: int, pad: int) -> list[tuple[np.ndarray, tuple[int, int]]]:
    h, w = out_img.shape[:2]
    k = w / (2 * BCELL)
    cells = []
    for r in range(2):
        for c in range(2):
            y0 = int(round((r * BCELL + pad) * k))
            x0 = int(round((c * BCELL + pad) * k))
            size = int(round(n * f * k))
            m = int(round(4 * f * k))
            cy0, cx0 = max(0, y0 - m), max(0, x0 - m)
            cells.append((out_img[cy0 : min(h, y0 + size + m), cx0 : min(w, x0 + size + m)], (y0 - cy0, x0 - cx0)))
    return cells


def side(kind: str) -> dict:
    L = P.LIMBS.get(kind, P.LIMBS["human"])
    return {"hand": L["hand"], "hands": L["hands"], "arm": P._one(L["arms"]), "arms": L["arms"],
            "fist": P._one(L["fists"]), "fists": L["fists"], "finger": L["finger"]}


POSE_TEXT = {  # screen sides only ("the LEFT side of the picture"): models mix up "his right" vs "viewer's left"
    "point_0": "halfway into a point: the {arm} on the LEFT side of the picture bent, its {hand} rising to chest height "
               "with the {finger} starting to point forward (the in-between frame toward the TOP-RIGHT copy)",
    "point_1": "the {arm} on the LEFT side of the picture reaching forward toward the viewer, {finger} pointing straight "
               "at the viewer (foreshortened) in a cocky \"YOU!\" point; the other {arm} relaxed",
    "fist_pump_0": "the {fist} on the LEFT side of the picture raised beside the shoulder, elbow bent, winding up for a "
                   "fist pump",
    "fist_pump_1": "the same {fist} (LEFT side of the picture) yanked down hard to waist height in a big \"YES!\" fist "
                   "pump, elbow bent tight against the body",
    "shrug_0": "halfway into a shrug: both {hands} in front of the hips turning palm-up, elbows starting to lift, "
               "shoulders starting to rise (the in-between frame toward the TOP-RIGHT copy)",
    "shrug_1": "a big shrug: shoulders pulled up, both forearms out to the sides at waist height, palms open and facing "
               "up - both {hands} fully inside the picture, well clear of the edges",
    "thumbs_up_0": "the {hand} on the LEFT side of the picture rising to chest height, closed in a loose fist (the "
                   "in-between frame toward the BOTTOM-RIGHT copy)",
    "thumbs_up_1": "the same {hand} (LEFT side of the picture) held at chest height giving a big, clear THUMBS-UP to the "
                   "viewer",
    "open_arms_0": "halfway into a welcome: both {hands} rising in front of the stomach, starting to open outward, palms "
                   "turning up (the in-between frame toward the TOP-RIGHT copy)",
    "open_arms_1": "a big \"BOYS!\" welcome: both {hands} raised to chest height in front of the body and spread apart, "
                   "palms up and open, elbows out - both {hands} fully inside the picture, well clear of the edges",
    "drink_sip_0": "the {hand} on the LEFT side of the picture holding a plain unbranded drink can ({can}, no text, no "
                   "logo, no label) at chest height",
    "drink_sip_1": "the SAME {hand} (LEFT side of the picture) lifting the same plain can to the mouth and tilting it to "
                   "take a sip - the can touches the lips; the other {hand} stays down",
    "talk_hands_0": "both {hands} loosely together in front of the stomach, relaxed, as if about to explain something",
    "talk_hands_1": "the same pose as the TOP-LEFT copy but the {hands} a little apart, the one on the RIGHT side of the "
                    "picture turned palm-up",
    "talk_hands_2": "the same pose as the TOP-LEFT copy but the {hand} on the LEFT side of the picture raised a few "
                    "pixels higher, palm open",
    "talk_hands_3": "the same pose as the TOP-LEFT copy but both {hands} slightly further apart, palms up, making a point",
    "laugh_0": "one {hand} pressed on the belly and the other on the chest, shoulders hunched up, shaking with laughter",
    "laugh_1": "the same laugh as the TOP-LEFT copy, but the {hand} on the belly pressed flatter and the shoulders "
               "pulled up even higher (no impact marks)",
    "facepalm_0": "the {hand} on the LEFT side of the picture rising toward the face, open, about to facepalm (the "
                  "in-between frame toward the BOTTOM-RIGHT copy)",
    "facepalm_1": "a facepalm: the same open {hand} covering the eyes and forehead, head unchanged under it - the mouth "
                  "stays visible below the {hand}",
    "react_shock_0": "startled: both {hands} flying up toward the cheeks, almost there, fingers spread (the in-between "
                     "frame toward the TOP-RIGHT copy)",
    "react_shock_1": "SHOCKED, the classic \"oh no!\" face-clutch: both {hands} pressed flat against the cheeks, fingers "
                     "up along the sides of the face beside the eyes, elbows in - the eyes, nose and mouth stay "
                     "uncovered, and the {hands} stay tight against the face",
    "clap_0": "delighted applause right beside the face: both {hands} at cheek height, tight against the side of the head "
              "on the LEFT of the picture, palms a little apart, about to clap - the face stays uncovered",
    "clap_1": "the same two {hands} clapped together at cheek height, tight against the side of the head on the LEFT of "
              "the picture - the face stays uncovered",
    "cheer_0": "delighted: both {hands} coming up toward the face, closed in loose fists (the in-between frame toward "
               "the TOP-RIGHT copy)",
    "cheer_1": "delighted: two big THUMBS-UP held right beside the cheeks, one {hand} on each side of the face at eye "
               "height, touching the sides of the face - the eyes, nose and mouth uncovered",
    "cheer_2": "the same two thumbs-up as the TOP-RIGHT copy, pumped a little higher (level with the eyebrows)",
    "cheer_3": "a delighted double fist-pump: both {fists} right beside the cheeks at eye height, touching the sides of "
               "the face - the eyes, nose and mouth uncovered",
    "enter_0": "25% of the way from the start pose to the end pose",
    "enter_1": "50% of the way from the start pose to the end pose",
    "enter_2": "75% of the way from the start pose to the end pose",
    "enter_3": "90% of the way: almost exactly the end pose",
}
BODY_SETS = {
    "body_a": ["point_0", "point_1", "fist_pump_0", "fist_pump_1"],
    "body_b": ["shrug_0", "shrug_1", "thumbs_up_0", "thumbs_up_1"],
    "body_c": ["open_arms_0", "open_arms_1", "drink_sip_0", "drink_sip_1"],
    "body_d": ["talk_hands_0", "talk_hands_1", "talk_hands_2", "talk_hands_3"],
    "body_e": ["laugh_0", "laugh_1", "facepalm_0", "facepalm_1"],
    "body_f": ["react_shock_0", "react_shock_1", "clap_0", "clap_1"],
}
BODY_ROWS = {"body_d": [["talk_hands_0", "talk_hands_1", "talk_hands_2", "talk_hands_3"]],  # one clip: keep together
             "enter": [["enter_0", "enter_1", "enter_2", "enter_3"]],
             "happy_fix": [["cheer_0", "cheer_1", "cheer_2", "cheer_3"]]}
ENTER = ["enter_0", "enter_1", "enter_2", "enter_3"]
CHEER = ["cheer_0", "cheer_1", "cheer_2", "cheer_3"]  # on demand: react_happy when no clap fits the reaction cam  # base pose -> talk_hands_0 (drawn against both, second pass)
REACH_CLIPS = ("react_shock_1", "clap_0", "clap_1", "cheer_1", "cheer_2", "cheer_3")  # key frames in the reaction cam
REACH_MIN = 70  # % of hand pixels inside the bust crop
CHEER_MIN = 55  # thumbs-up / fists beside the face: the fist reads even when the wrist is cut by the crop
BODY_F_VERSION = 4  # body_f prompt generation: hands near the face (reaction-cam bust crop)


def body_grid_prompt(kind: str, names: list[str], can: str, retry: bool = False) -> str:
    s = side(kind)
    lines = "; ".join(f"{POS[i]} copy: {POSE_TEXT[n].format(can=can, **s)}" for i, n in enumerate(names))
    robot = kind == "robot"
    text = (
        "This image is a 2x2 grid of four IDENTICAL copies of the same pixel-art character portrait (a 16-bit "
        "video-game cut-scene portrait drawn with big square pixels). It is an animation sheet: each copy becomes one "
        f"frame of a gesture animation. Change ONLY the {s['arms']} and {s['hands']} (and what they hold) in each copy: "
        f"{lines}. In every copy keep the head, face, expression, {'antenna, ' if robot else 'hair, '}suit, shirt, tie, "
        "pocket square, body position and the background EXACTLY as in the input - pixel for pixel, same position, "
        "same size. Keep the face uncovered unless a pose says otherwise. Same art style, same pixel grid and "
        "palette, hard-edged square pixels, no blur. Keep the 2x2 layout: each copy stays in its quarter, the same "
        "size, with nothing drawn between them. No motion lines, speed lines, sweat drops, sparkles or other effects. "
        "No text, labels, numbers, logos or weapons anywhere."
    )
    if retry:
        text += (" IMPORTANT: the previous attempt redrew the head or body. Only the arms and hands may move; the head, "
                 "face and torso must be pixel-identical to the input in all four copies.")
    return text


def enter_prompt(kind: str, retry: bool = False) -> str:
    s = side(kind)
    lines = "; ".join(f"{POS[i]} copy: {POSE_TEXT[n]}" for i, n in enumerate(ENTER))
    text = (
        "Image 1 is a 2x2 grid of four IDENTICAL copies of the same pixel-art character portrait (a 16-bit video-game "
        "cut-scene portrait drawn with big square pixels): the START pose of a short animation. Image 2 is the same "
        "character in the END pose. Turn the four copies of image 1 into the in-between frames of the motion from the "
        f"start pose to the end pose: {lines}. Only the {s['arms']} and {s['hands']} move (anything held in the start "
        "pose that is gone in the end pose is lowered down out of the picture). In every copy keep the head, face, "
        "expression, suit, shirt, tie, pocket square, body position and the background EXACTLY as in image 1 - pixel "
        "for pixel. Same art style, pixel grid and palette, hard-edged square pixels. Keep the 2x2 layout: each copy "
        "stays in its quarter, the same size, nothing drawn between them. No motion lines or effects. No text, logos "
        "or weapons."
    )
    if retry:
        text += " IMPORTANT: only the arms and hands may change; the head, face and torso stay pixel-identical."
    return text


def body_cell_image(frame: np.ndarray) -> Image.Image:
    """One native frame upscaled the way a body-sheet cell is (whole-number factor, edge padded to 1024)."""
    n = frame.shape[0]
    f = BCELL // n
    pad = (BCELL - n * f) // 2
    up = np.repeat(np.repeat(frame, f, 0), f, 1)
    return Image.fromarray(np.pad(up, ((pad, BCELL - n * f - pad), (pad, BCELL - n * f - pad), (0, 0)), mode="edge"))


BG_TEXT_CHECK = (
    "Look at the BACKGROUND of this pixel-art portrait only (ignore the person, their clothes and anything they hold). "
    "Reply with ONLY a JSON object: \"background_text\" (true if any readable letters, words or numbers appear on signs, "
    "fridges, walls or props behind the person), \"where\" (at most 10 words), \"notes\" (at most 10 words)."
)
BG_CLEAN_PROMPT = (
    "Edit this image: remove every letter, word and number from the signs, labels, fridge and walls in the BACKGROUND - "
    "repaint those spots as plain surface in the same colours as around them (a blank sign stays a blank sign). Change "
    "nothing else: the character, their clothes, the lights, props and every other background pixel stay exactly as "
    "they are. Same pixel grid and palette, hard-edged square pixels. No new text anywhere."
)


SIL_PROMPT = (
    "Edit this image: replace the ENTIRE background with one flat, uniform, pure green colour (#00FF00) - every "
    "background pixel: the room, walls, lights, furniture, fridge and floor. Keep the character exactly as is: the same "
    "pixels, outline, position and size, including the hair, shoulders and the edges of the suit. No shadow, glow, "
    "outline or gradient on the green. No text."
)


def silhouette_from(ctx, raw_file: Path) -> tuple[np.ndarray, dict]:
    img = np.asarray(Image.open(raw_file).convert("RGB"))
    n = ctx.base.shape[0]
    period = img.shape[0] / n
    nat = sample_native(img, period)
    dx, dy, frac = best_shift(ctx.base, nat, np.ones(ctx.base.shape[:2], bool), r=4)
    al = place(nat, ctx.base.shape, dx, dy, np.zeros_like(ctx.base))
    a = al.astype(np.int16)
    green = (a[..., 1] > 170) & (a[..., 0] < 110) & (a[..., 2] < 110)
    fg = ~green
    lab, k = ndi.label(fg)
    if k:
        sizes = ndi.sum(fg, lab, np.arange(1, k + 1))
        fg = lab == (int(np.argmax(sizes)) + 1)
    fg = ndi.binary_fill_holes(fg)
    fg = ndi.binary_opening(fg, structure=np.ones((2, 2), bool)) | (fg & ndi.binary_erosion(fg))
    fg = ndi.binary_fill_holes(fg)
    # the silhouette must contain the whole head box face zones
    return fg, {"shift": [dx, dy], "match": round(frac, 3), "fg_pct": round(float(fg.mean()) * 100, 1)}


def motion_lock(ctx, cell_img: np.ndarray, off: tuple[int, int], period: float) -> tuple[np.ndarray, np.ndarray, dict]:
    """Keep only the moving parts of a model frame. Returns (frame RGB native, motion mask, metrics).

    Outside the head box the motion region is the dense part of the change map (smoothed, holes filled), so its
    border runs where the model agrees with the base and no seam shows. Inside the head box only change blobs that
    reach in from outside (a hand, a can) are kept: expression changes are dropped (the face patches do those)."""
    base = ctx.base
    nat = sample_native(cell_img, period)
    dy0, dx0 = int(round(off[0] / period)), int(round(off[1] / period))
    dx, dy, frac = best_shift(base, nat, np.ones(base.shape[:2], bool), r=4 + max(dx0, dy0))
    al = place(nat, base.shape, dx, dy, base)
    q = snap(al, ctx.palette)
    strong = R.diff_mask(q, base, 40)
    x0, y0, x1, y1 = ctx.box
    inhead = np.zeros(base.shape[:2], bool)
    inhead[0:y1, x0:x1] = True  # the head box and everything above it (hair tufts, hats, antennas)
    sil = ctx.sil if ctx.sil is not None else np.ones(base.shape[:2], bool)
    near_char = ndi.binary_dilation(sil, iterations=3)
    # --- outside the head box: dense change regions
    dens = ndi.uniform_filter(strong.astype(np.float32), size=5)
    cand = ((dens > 0.14) | ndi.binary_opening(strong, structure=np.ones((2, 2), bool))) & ~inhead
    cand = ndi.binary_closing(cand, structure=np.ones((3, 3), bool), iterations=2) & ~inhead
    lab, k = ndi.label(cand, structure=np.ones((3, 3), bool))
    keep = []
    for j in range(1, k + 1):
        comp = lab == j
        if comp.sum() < 24 or not (comp & near_char).any():
            continue
        keep.append(j)
    out_m = ndi.binary_fill_holes(np.isin(lab, keep))
    out_m |= strong & ndi.binary_dilation(out_m, iterations=2) & ~inhead
    out_m = ndi.binary_fill_holes(out_m)
    # --- inside the head box: strong blobs connected to the outside motion
    slab, sk = ndi.label(strong, structure=np.ones((3, 3), bool))
    ids = np.unique(slab[strong & ndi.binary_dilation(out_m, iterations=1) & inhead])
    in_m = np.isin(slab, ids[ids > 0]) & inhead
    if in_m.any():
        in_m = ndi.binary_fill_holes(ndi.binary_dilation(in_m, iterations=1) & inhead) & (strong | ndi.binary_dilation(in_m))
    m = out_m | in_m
    frame = base.copy()
    frame[m] = q[m]
    faces = ctx.face_zone_canvas
    metrics = {"align_match": round(frac, 3), "shift": [dx + dx0, dy + dy0], "motion_pct": round(float(m.mean()) * 100, 2),
               "dropped_face_px": int((strong & inhead & ~m).sum()), "over_head_px": int(in_m.sum()),
               "hand_over_face": bool((in_m & faces).any())}
    return frame, m, metrics


# ============================================================================ programmatic motion
def shift_vert(frame: np.ndarray, sil: np.ndarray, dy: int, y_cut: int | None = None) -> np.ndarray:
    """Move the character (silhouette pixels above y_cut, or all of it) up (dy=-1) or down (dy=+1) one native pixel.
    Up: the cut row is duplicated (1-px stretch). Down: the cut row is absorbed and the pixel uncovered at the top
    edge (hair, shoulders) takes the room colour right above it. Nothing is left as a hole."""
    region = sil.copy()
    if y_cut is not None:
        region[y_cut + 1 :] = False
    out = frame.copy()
    if dy < 0:
        src = region[1:]  # (y, x) -> (y-1, x)
        out[:-1][src] = frame[1:][src]
        return out
    src = region[:-1].copy()  # (y, x) -> (y+1, x)
    if y_cut is not None:
        src[y_cut:] = False
    out[1:][src] = frame[:-1][src]
    moved_into = np.zeros_like(region)
    moved_into[1:] = src
    vac = region & ~moved_into
    ys, xs = np.nonzero(vac)
    out[ys, xs] = frame[np.maximum(ys - 1, 0), xs]
    return out


def shift_up(frame: np.ndarray, sil: np.ndarray, y_cut: int | None = None) -> np.ndarray:
    return shift_vert(frame, sil, -1, y_cut)


def chest_line(ctx) -> int:
    """Row below which breathing does not move anything: a bit under the head box (mid-chest)."""
    return min(ctx.base.shape[0] - 2, int(ctx.box[3] + 0.25 * (ctx.box[3] - ctx.box[1])))


# ============================================================================ QA prompts
FACE_CHECK = (
    "Look at this pixel-art character portrait. Reply with ONLY a JSON object: \"species\" (\"human\", \"bird\", "
    "\"animal\", \"fish\" or \"robot\"), \"mouth_kind\" (\"lips\", \"beak\", \"snout\", \"jaw_plate\" or \"other\"), "
    "\"facial_hair\" (short description or \"none\"), \"notes\" (at most 12 words)."
)


def sheet_check_prompt(n: int) -> str:
    return (
        "You are QA for an animated sports-show character rig. Image 1 is the character's reference portrait. Image 2 "
        f"is a grid of {n} animation frames (read left to right, top to bottom, numbered from 0) of the same character: "
        "gestures with the arms and hands, and close-ups of mouth shapes and eye expressions. Reply with ONLY a JSON "
        "object: \"same_character\" (true if every frame is clearly the same character as image 1), \"same_outfit\" "
        "(true if the suit, shirt and tie match image 1 in every frame), \"face_consistent\" (true if the head and face "
        "look identical across the body frames apart from mouth/eye expression), \"text_or_logo\" (true if readable "
        "letters, numbers, words or a brand logo appear on the character or anything they hold - a plain lapel pin, "
        "button or badge without writing is fine; ignore the room background), "
        f"\"weapon_visible\" ({P.WEAPON_CHECK}), \"broken_frames\" (list of frame numbers with a glitch: extra or missing "
        "fingers or limbs, a hand merged into the body, a doubled face, a seam or smear), \"notes\" (at most 25 words)."
    )


def vision_json(ctx, variant: str, images: list[Path], prompt: str) -> dict | None:
    content = [{"type": "image_url", "image_url": {"url": G.data_url(p)}} for p in images]
    content.append({"type": "text", "text": prompt})
    for model in G.QA_MODELS:
        res = G.call_model(model, content, ctx.budget, image=False)
        parsed = G._parse_json(res.get("text")) if res.get("ok") else None
        G.record_call(ctx.args, ctx.team, f"rig_{variant}", {**res, "text": (res.get("text") or "")[:300]}, prompt, None,
                      images[-1].name)
        if parsed is not None:
            parsed.update(model=model, cost=round(res.get("cost", 0.0), 6))
            return parsed
    return None


# ============================================================================ build one team
GESTURE_CLIPS = {  # name: (frames, fps, loop, hold, exit, phases)   "^" = upper body 1 px up, "v" = 1 px down
    "talk_hands": (["talk_hands_0", "talk_hands_1^", "talk_hands_0", "talk_hands_2v", "talk_hands_0^", "talk_hands_3"],
                   5, True, None, "cut", None),
    "point": (["point_0", "point_1", "point_1^"], 9, False, 1, "reverse", {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "fist_pump": (["fist_pump_0", "fist_pump_1", "fist_pump_1^"], 10, False, 1, "reverse",
                  {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "shrug": (["shrug_0", "shrug_1", "shrug_1^"], 8, False, 1, "reverse", {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "laugh": (["laugh_0", "laugh_0^", "laugh_1", "laugh_1^"], 9, True, None, "cut", None),
    "thumbs_up": (["thumbs_up_0", "thumbs_up_1", "thumbs_up_1^"], 8, False, 1, "reverse",
                  {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "open_arms": (["open_arms_0", "open_arms_1", "open_arms_1^"], 8, False, 1, "reverse",
                  {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "drink_sip": (["drink_sip_0", "drink_sip_1", "drink_sip_1^"], 5, False, 1, "reverse",
                  {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "facepalm": (["facepalm_0", "facepalm_1", "facepalm_1^"], 8, False, 1, "reverse",
                 {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "react_shock": (["react_shock_0", "react_shock_1", "react_shock_1^"], 12, False, 1, "reverse",
                    {"in": [0, 1], "hold": [1, 2], "out": [0]}),
    "react_happy": (["clap_0", "clap_1", "clap_0^", "clap_1"], 7, True, None, "cut", None),
}
HOLD_FPS = 3  # hold-phase bob (key frame <-> key frame 1 px up) while the character keeps talking


CHAIN = {  # frame -> reference: keep everything that did not move from the reference frame (no texture "boil")
    # loops: every frame shares the first frame's torso
    "talk_hands_1": "talk_hands_0", "talk_hands_2": "talk_hands_0", "talk_hands_3": "talk_hands_0",
    "laugh_1": "laugh_0", "clap_1": "clap_0",
    # in/hold clips: the brief in-between borrows the key pose's torso (the key pose stays untouched)
    "point_0": "point_1", "fist_pump_0": "fist_pump_1", "shrug_0": "shrug_1", "thumbs_up_0": "thumbs_up_1",
    "open_arms_0": "open_arms_1", "drink_sip_0": "drink_sip_1", "facepalm_0": "facepalm_1",
    "react_shock_0": "react_shock_1",
}


def chain_lock(ref: np.ndarray, frame: np.ndarray) -> np.ndarray:
    """frame, but with every pixel that did not really change vs ref taken from ref (dense-change regions only)."""
    strong = R.diff_mask(frame, ref, 24)
    dens = ndi.uniform_filter(strong.astype(np.float32), size=5)
    cand = (dens > 0.14) | ndi.binary_opening(strong, structure=np.ones((2, 2), bool))
    cand = ndi.binary_closing(cand, structure=np.ones((3, 3), bool), iterations=2)
    lab, k = ndi.label(cand, structure=np.ones((3, 3), bool))
    if not k:
        return ref.copy()
    sizes = ndi.sum(cand, lab, np.arange(1, k + 1))
    keep = np.isin(lab, [j + 1 for j, sz in enumerate(sizes) if sz >= 12])
    keep = ndi.binary_fill_holes(keep)
    keep |= strong & ndi.binary_dilation(keep, iterations=2)
    keep = ndi.binary_fill_holes(keep)
    out = ref.copy()
    out[keep] = frame[keep]
    return out


def raw_files(ctx, variant: str) -> list[Path]:
    return sorted(ctx.raw.glob(f"{variant}_[0-9]*.*"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))


def face_ok(name: str, m: dict, S: int) -> list[str]:
    bad = []
    if m["align_match"] < 0.85:
        bad.append("misaligned")
    if m["drift_pct"] > 4.0:
        bad.append("face redrawn")
    subtle = name in ("closed", "smirk", "skeptical", "look_left", "look_right") or name.endswith(":rest")
    if m["changed_px"] < (6 if subtle else max(12, 0.0015 * S * S)):  # closed ~ rest by nature: only flag no change
        bad.append("weak")
    return bad


def body_ok(m: dict, name: str = "") -> list[str]:
    bad = []
    if m["align_match"] < 0.6:
        bad.append("misaligned")
    if m["motion_pct"] > 48:
        bad.append("body redrawn")
    # an in-between may legitimately equal the base (fable already holds a can at chest height)
    if m["motion_pct"] < 0.8 and not (name.endswith("_0") and not name.startswith(("talk", "laugh", "clap"))):
        bad.append("no motion")
    return bad


def base_sha(tdir: Path) -> str:
    import hashlib
    return hashlib.sha1((tdir / "base.png").read_bytes()).hexdigest()[:16]


def build_team(args, team: str, budget: "G.Budget", model: str = "pro") -> dict:
    tdir = args.out / team
    st0 = State(tdir / "rig" / "raw" / "state.json")
    sha = base_sha(tdir)
    if st0.get("base_sha") and st0.get("base_sha") != sha:  # base portrait regenerated: every cached sheet is stale
        for f in (tdir / "rig" / "raw").glob("*_[0-9]*.*"):
            if not f.name.startswith("discarded_"):
                f.rename(f.with_name(f"discarded_{f.name}"))
        (tdir / "rig" / "raw" / "state.json").unlink(missing_ok=True)
        G.log(f"  [{team}] base portrait changed since the last rig: cached sheets discarded")
    ctx = Ctx(args, team, budget)
    ctx.state.set("base_sha", sha)
    mdl = ctx.model(model)
    st = ctx.state
    spent0 = budget.spent
    log = G.log
    n = ctx.base.shape[0]
    S = ctx.head.shape[0]
    flags: list[str] = []
    persona_colour = ctx.meta.get("primary_color") or "#0E1B4D"
    can = f"solid {P.colour_name(persona_colour)}"

    # ---- 0. face type sanity check (vision) before any mouth asset
    if not st.get("face_check") and not args.no_check:
        hp = ctx.raw / "head_x4.png"
        Image.fromarray(np.repeat(np.repeat(ctx.head, 4, 0), 4, 1)).save(hp)
        chk = vision_json(ctx, "face_check", [hp], FACE_CHECK)
        if chk:
            st.set("face_check", chk)
    fc = st.get("face_check") or {}
    kind = ctx.kind
    if fc.get("species") == "human" and fc.get("mouth_kind") == "lips" and kind != "human":
        flags.append(f"face_type {kind} -> human (vision check: {fc.get('notes', '')})")
        kind = "human"
    ctx.kind = kind

    # ---- 1. requests (parallel): silhouette, 4 face sheets, 4 body sheets
    jobs = []
    if not raw_files(ctx, "silhouette"):
        k = max(1, 1024 // n)
        jobs.append(("silhouette", lambda: ctx.call_image("silhouette", [Image.fromarray(np.repeat(np.repeat(ctx.base, k, 0), k, 1))],
                                                          SIL_PROMPT, mdl)))
    for name, (zone, names) in FACE_SETS.items():
        if name not in ON_DEMAND and not raw_files(ctx, f"face_{name}"):
            img = grid_input(ctx.head)[0]
            jobs.append((f"face_{name}", lambda img=img, zone=zone, names=names, name=name: ctx.call_image(
                f"face_{name}", [img], face_grid_prompt(kind, zone, names), mdl)))
    for name, names in BODY_SETS.items():
        if not raw_files(ctx, name):
            img = body_grid_input(ctx.base)[0]
            jobs.append((name, lambda img=img, names=names, name=name: ctx.call_image(
                name, [img], body_grid_prompt(kind, names, can), mdl, image_size="2K")))
            if name == "body_f":
                st.set("body_f_version", BODY_F_VERSION)
    bg_chk = st.get("bg_text_check")
    if bg_chk is None and not args.no_check:
        bg_chk = vision_json(ctx, "bg_text_check", [ctx.tdir / "base.png"], BG_TEXT_CHECK) or {}
        st.set("bg_text_check", bg_chk)
    if (bg_chk or {}).get("background_text") and not raw_files(ctx, "bgclean"):
        k2 = max(1, 1024 // n)
        jobs.append(("bgclean", lambda: ctx.call_image("bgclean", [Image.fromarray(np.repeat(np.repeat(ctx.base, k2, 0), k2, 1))],
                                                       BG_CLEAN_PROMPT, mdl)))
    need = len(jobs) * G.HOLD.get(mdl, 0.3)
    if jobs and budget.remaining() < need - 1e-9:
        return {"team": team, "status": f"skipped (budget: need ${need:.2f}, have ${budget.remaining():.2f})", "cost": 0.0}
    if jobs:
        log(f"  [{team}] rig: {len(jobs)} image calls ({', '.join(j[0] for j in jobs)})")
        with ThreadPoolExecutor(min(6, len(jobs))) as ex:
            list(ex.map(lambda j: j[1](), jobs))

    # ---- 1b. zones from what the face sheets actually changed
    zinfo = calibrate_zones(ctx)

    # ---- 2. silhouette
    sils = raw_files(ctx, "silhouette")
    if sils:
        sil, sm = silhouette_from(ctx, sils[-1])
        if 15 <= sm["fg_pct"] <= 85:
            Image.fromarray((sil * 255).astype(np.uint8)).save(ctx.raw / "silhouette.png")
            ctx.sil = sil
        else:
            flags.append(f"silhouette rejected ({sm})")
    if ctx.sil is None:
        flags.append("no silhouette: breathing/laugh bob move the whole upper frame")
        ctx.sil = np.ones(ctx.base.shape[:2], bool)

    # ---- 3. face patches (+ one retry per failed sheet)
    patches: dict[str, dict] = {}
    face_metrics = {}

    def run_face(name: str) -> dict:
        res = {}
        for f in raw_files(ctx, f"face_{name}"):
            got = extract_patches(ctx, name, f)
            for state, r in got.items():
                r["bad"] = face_ok(state, r["metrics"], S)
                r["file"] = f.name
                cur = res.get(state)
                if cur is None or (len(r["bad"]), -r["metrics"]["align_match"]) < (len(cur["bad"]), -cur["metrics"]["align_match"]):
                    res[state] = r
        return res

    for name in FACE_SETS:
        if name in ON_DEMAND:
            continue
        res = run_face(name)
        res = {k: v for k, v in res.items() if k.split("#")[0] not in GLANCES}
        worst = [s for s, r in res.items() if r["bad"]]
        if worst and len(raw_files(ctx, f"face_{name}")) < 2 and not args.no_retry and budget.remaining() >= G.HOLD.get(mdl, 0.3):
            why = ", ".join(f"{x} {res[x]['bad']}" for x in worst)
            log(f"  [{team}] face sheet {name}: {why} -> retry")
            zone, names = FACE_SETS[name]
            ctx.call_image(f"face_{name}", [grid_input(ctx.head)[0]], face_grid_prompt(kind, zone, names, retry=True), mdl, retry="drift")
            res = {k: v for k, v in run_face(name).items() if k.split("#")[0] not in GLANCES}
        patches.update(res)
    # glances: measured on screen, filed under the direction they really look; one stricter sheet if one is missing
    blink_p = (patches.get("blink") or {}).get("patch")
    glances, glance_log = select_glances(ctx, S, blink_p)
    if any(g not in glances or "mirror" in str(glances[g]["metrics"].get("cell")) for g in GLANCES) \
            and len(raw_files(ctx, "face_eyes_glance")) < 2 and not args.no_retry and budget.remaining() >= G.HOLD.get(mdl, 0.3):
        log(f"  [{team}] glance: {[g for g in GLANCES if g not in glances]} missing or mirrored -> another glance sheet")
        zone, names = FACE_SETS["eyes_glance"]
        ctx.call_image("face_eyes_glance", [grid_input(ctx.head)[0]], face_grid_prompt(kind, zone, names, retry=True),
                       mdl, retry="glance")
        glances, glance_log = select_glances(ctx, S, blink_p)
    for g in GLANCES:
        if g in glances:
            patches[g] = glances[g]
        else:
            flags.append(f"{g}: no sprite really looks that way on screen (dropped: eyes_fallback -> open)")
    # a robot's closed mouth must differ from rest (the jaw plate has no lips): one sheet of seam variants
    if kind == "robot" and ("closed" not in patches or "weak" in patches["closed"]["bad"]):
        if not raw_files(ctx, "face_mouth_fix_closed") and budget.remaining() >= G.HOLD.get(mdl, 0.3):
            zone, names = FACE_SETS["mouth_fix_closed"]
            ctx.call_image("face_mouth_fix_closed", [grid_input(ctx.head)[0]], face_grid_prompt(kind, zone, names), mdl)
        fixes = [r for r in run_face("mouth_fix_closed").values() if not r["bad"]]
        if fixes:
            best_c = max(fixes, key=lambda r: r["metrics"]["changed_px"])
            patches["closed"] = best_c
    # old whole-image frames as fallbacks (face-locked to this base, so consistent)
    old = {}
    for v, key in (("mid", "medium_open"), ("wide", "wide_open"), ("blink", "blink")):
        pth = ctx.tdir / f"{v}.png"
        if pth.exists() and not (team in ("fugu", "fable") and v != "blink"):
            old[key] = R.make_patch(ctx.head, R.crop(R.to_native(pth, ctx.scale), ctx.box))
    for state, p in old.items():
        if state not in patches or patches[state]["bad"]:
            flags.append(f"{state}: using the old face-locked frame ({(patches.get(state) or {}).get('bad')})")
            patches[state] = {"patch": p, "metrics": {"source": "old frame"}, "bad": []}
    for state, r in patches.items():
        face_metrics[state] = {**r["metrics"], "bad": r["bad"], "file": r.get("file")}
        if r["bad"]:
            flags.append(f"{state}: {', '.join(r['bad'])}")

    # ---- 4. body frames
    frames: dict[str, dict] = {}
    body_metrics = {}
    img_f = BCELL // n
    pad = (BCELL - n * img_f) // 2

    def run_body(name: str) -> dict:
        best = {}
        for f in raw_files(ctx, name):
            out = np.asarray(Image.open(f).convert("RGB"))
            k = out.shape[1] / (2 * BCELL)
            got = {}
            for (cimg, off), fname in zip(body_cells(out, n, img_f, pad), BODY_SETS[name]):
                frame, m, met = motion_lock(ctx, cimg, off, img_f * k)
                got[fname] = {"frame": frame, "mask": m, "metrics": met, "bad": body_ok(met, fname), "file": f.name}
            # choose per gesture ROW (keep a clip's frames from the same attempt)
            for row in BODY_ROWS.get(name, [BODY_SETS[name][:2], BODY_SETS[name][2:]]):
                score = sum(len(got[x]["bad"]) for x in row)
                cur = sum(len(best[x]["bad"]) for x in row) if all(x in best for x in row) else None
                if cur is None or score < cur:
                    for x in row:
                        best[x] = got[x]
        return best

    for name in BODY_SETS:
        res = run_body(name)
        if any(r["bad"] for r in res.values()) and len(raw_files(ctx, name)) < 2 and not args.no_retry \
                and budget.remaining() >= G.HOLD.get(mdl, 0.3):
            log(f"  [{team}] body sheet {name}: {[(x, r['bad']) for x, r in res.items() if r['bad']]} -> retry")
            ctx.call_image(name, [body_grid_input(ctx.base)[0]], body_grid_prompt(kind, BODY_SETS[name], can, retry=True),
                           mdl, retry="drift", image_size="2K")
            res = run_body(name)
        frames.update(res)
    # reaction-cam reach: hands of the listener clips inside the bust crop (checked on new-version sheets)
    hbox_early = tight_head_box(ctx)
    skin = skin_colours(ctx, hbox_early)

    def reach_fail(fr_: dict) -> list[str]:
        return [x for x in REACH_CLIPS if x in fr_ and
                (reach_metrics(ctx, hbox_early, fr_[x]["frame"], fr_[x]["mask"], skin).get("inside_pct") or 100) < REACH_MIN]

    def best_body_f() -> dict | None:  # among every body_f attempt: fewest listener frames outside the cam, then fewest bad
        best_f, best_bad = None, None
        for f in raw_files(ctx, "body_f"):
            out = np.asarray(Image.open(f).convert("RGB"))
            k = out.shape[1] / (2 * BCELL)
            got = {}
            for (cimg, off), fname in zip(body_cells(out, n, img_f, pad), BODY_SETS["body_f"]):
                frame, m, met = motion_lock(ctx, cimg, off, img_f * k)
                got[fname] = {"frame": frame, "mask": m, "metrics": met, "bad": body_ok(met, fname), "file": f.name}
            nb = (len(reach_fail(got)), sum(len(v["bad"]) for v in got.values()))
            if best_bad is None or nb < best_bad:
                best_f, best_bad = got, nb
        return best_f

    if st.get("body_f_version") == BODY_F_VERSION and reach_fail(frames) and len(raw_files(ctx, "body_f")) < 2 \
            and not args.no_retry and budget.remaining() >= G.HOLD.get(mdl, 0.3):
        log(f"  [{team}] listener clips: hands outside the reaction cam {reach_fail(frames)} -> retry body_f")
        ctx.call_image("body_f", [body_grid_input(ctx.base)[0]], body_grid_prompt(kind, BODY_SETS["body_f"], can, retry=True)
                       + " The hands must TOUCH the face or the side of the head, at face height, entirely above the "
                         "chin: the reaction camera only shows the head and a hand's width around it.", mdl,
                       retry="reach", image_size="2K")
    if len(raw_files(ctx, "body_f")) > 1:
        frames.update(best_body_f() or {})
    # react_happy when no clap fits the reaction cam: thumbs-up / fists right beside the face (new-version rigs only)
    claps_in = [(reach_metrics(ctx, hbox_early, frames[x]["frame"], frames[x]["mask"], skin).get("inside_pct") or 100)
                for x in ("clap_0", "clap_1") if x in frames]
    if claps_in and min(claps_in) < REACH_MIN and st.get("body_f_version") == BODY_F_VERSION \
            and not raw_files(ctx, "happy_fix") and budget.remaining() >= G.HOLD.get(mdl, 0.3):
        log(f"  [{team}] react_happy: claps leave the reaction cam {claps_in} -> cheer sheet")
        ctx.call_image("happy_fix", [body_grid_input(ctx.base)[0]], body_grid_prompt(kind, CHEER, can), mdl, image_size="2K")
    for f in raw_files(ctx, "happy_fix")[-1:]:
        out = np.asarray(Image.open(f).convert("RGB"))
        k = out.shape[1] / (2 * BCELL)
        for (cimg, off), fname in zip(body_cells(out, n, img_f, pad), CHEER):
            frame, m, met = motion_lock(ctx, cimg, off, img_f * k)
            frames[fname] = {"frame": frame, "mask": m, "metrics": met, "bad": body_ok(met, fname), "file": f.name}
    # enter_from_base: in-betweens from the base pose to talk_hands_0, drawn against both frames (second pass)
    if "talk_hands_0" in frames:
        if not raw_files(ctx, "enter") and budget.remaining() >= G.HOLD.get(mdl, 0.3):
            ctx.call_image("enter", [body_grid_input(ctx.base)[0], body_cell_image(frames["talk_hands_0"]["frame"])],
                           enter_prompt(kind), mdl, image_size="2K")
        best_e = None
        for f in raw_files(ctx, "enter"):
            out = np.asarray(Image.open(f).convert("RGB"))
            k = out.shape[1] / (2 * BCELL)
            got = {}
            for (cimg, off), fname in zip(body_cells(out, n, img_f, pad), ENTER):
                frame, m, met = motion_lock(ctx, cimg, off, img_f * k)
                got[fname] = {"frame": frame, "mask": m, "metrics": met, "bad": body_ok(met, fname), "file": f.name}
            if best_e is None or sum(len(v["bad"]) for v in got.values()) < sum(len(v["bad"]) for v in best_e.values()):
                best_e = got
        if best_e:
            frames.update(best_e)
    for fname, ref in CHAIN.items():
        if fname in frames and ref in frames:
            fr = frames[fname]
            locked = chain_lock(frames[ref]["frame"], fr["frame"])
            fr["metrics"]["chain_kept_pct"] = round(float(R.diff_mask(locked, frames[ref]["frame"], 0).mean()) * 100, 2)
            fr["frame"] = locked
            fr["mask"] = R.diff_mask(locked, ctx.base, 0)
    for fname, r in frames.items():
        body_metrics[fname] = {**r["metrics"], "bad": r["bad"], "file": r["file"]}
        if r["bad"]:
            flags.append(f"{fname}: {', '.join(r['bad'])}")

    # ---- 4b. background text removal (static plate: the same repaint on every frame, never on the character)
    bg_mask, bg_px, bg_info = None, None, {}
    bgs = raw_files(ctx, "bgclean")
    if bgs:
        bg_mask, bg_px, bg_info = bg_clean_fix(ctx, bgs[-1])
        if not bg_info["fixed_px"]:
            bg_mask = None

    def clean_bg(frame: np.ndarray) -> np.ndarray:
        if bg_mask is None:
            return frame
        out_ = frame.copy()
        sel = bg_mask & ~R.diff_mask(frame, ctx.base, 0)  # only where the frame still shows the base background
        out_[sel] = bg_px[sel]
        return out_

    # ---- 5. write layers (patches re-cropped from the square generation crop to the tight head box)
    rd = ctx.rd
    for sub in ("mouth/neutral", "mouth/happy", "mouth/angry", "eyes", "body"):
        (rd / sub).mkdir(parents=True, exist_ok=True)
    for old_file in list((rd / "body").glob("*.png")) + list((rd / "mouth").glob("*/*.png")) + list((rd / "eyes").glob("*.png")):
        old_file.unlink()  # a rebuild never leaves stale layers behind
    opaque = lambda a: np.concatenate([a, np.full(a.shape[:2] + (1,), 255, np.uint8)], axis=2)  # noqa: E731
    sx0, sy0, sx1, sy1 = ctx.box
    hbox = tight_head_box(ctx)
    tx0, ty0, tx1, ty1 = hbox
    def tcrop(a: np.ndarray) -> np.ndarray:  # square-crop coords -> canvas -> tight head box
        canvas = np.zeros(ctx.base.shape[:2] + a.shape[2:], a.dtype)
        canvas[sy0:sy1, sx0:sx1] = a
        return canvas[ty0:ty1, tx0:tx1]
    head_rgba = opaque(R.crop(ctx.base, hbox))
    head_rgba[..., 3] = np.where(R.crop(ctx.sil, hbox), 255, 0)
    R.save_rgba(head_rgba, rd / "head.png")
    R.save_rgba(opaque(clean_bg(ctx.base)), rd / "bg.png")
    empty = np.zeros((ty1 - ty0, tx1 - tx0, 4), np.uint8)
    visemes: dict[str, dict] = {"neutral": {"rest": "mouth/neutral/rest.png"}}
    R.save_rgba(empty, rd / visemes["neutral"]["rest"])
    for v in R.VISEMES[1:]:
        if v in patches:
            visemes["neutral"][v] = f"mouth/neutral/{v}.png"
            R.save_rgba(tcrop(patches[v]["patch"]), rd / visemes["neutral"][v])
    for mood in ("happy", "angry"):
        got = {v: patches[f"{mood}:{v}"] for v in ("rest", "small_open", "medium_open", "wide_open")
               if f"{mood}:{v}" in patches and not patches[f"{mood}:{v}"]["bad"]}
        if len(got) < 3:
            if any(k.startswith(mood + ":") for k in patches):
                flags.append(f"mood {mood}: visemes rejected, neutral used")
            continue
        table = {}
        for v, r in got.items():
            table[v] = f"mouth/{mood}/{v}.png"
            R.save_rgba(tcrop(r["patch"]), rd / table[v])
        if "rest" in table:
            table["closed"] = table["rest"]  # M/B/P while grinning/scowling = the mood's closed mouth
            table["smirk"] = table["rest"] if mood == "angry" else visemes["neutral"].get("smirk", table["rest"])
        for v in ("round", "teeth"):
            if v in visemes["neutral"]:
                table[v] = visemes["neutral"][v]
        table["laugh"] = visemes["neutral"].get("laugh", table.get("wide_open")) if mood == "happy" else table.get("wide_open")
        visemes[mood] = {k: v for k, v in table.items() if v}
    eyes = {"open": "eyes/open.png"}
    R.save_rgba(empty, rd / eyes["open"])
    for e in R.EYES[1:]:
        if e in patches:
            eyes[e] = f"eyes/{e}.png"
            R.save_rgba(tcrop(patches[e]["patch"]), rd / eyes[e])
    union = np.zeros((ty1 - ty0, tx1 - tx0), bool)
    for r in patches.values():
        union |= tcrop(r["patch"])[..., 3] > 0
    Image.fromarray((union * 255).astype(np.uint8), "L").save(rd / "face_mask.png")

    inhead = np.zeros(ctx.base.shape[:2], bool)
    inhead[ty0:ty1, tx0:tx1] = True

    def fname(name: str) -> str:
        return name.replace("^", "_up").replace("v", "_dn") if name[-1:] in "^v" and name[-2:-1].isdigit() else name

    def write_frame(name: str, frame: np.ndarray, mask: np.ndarray | None, head_off=(0, 0)) -> dict:
        rel = f"body/{fname(name)}.png"
        frame = clean_bg(frame)
        R.save_rgba(opaque(frame), rd / rel)
        occ_rel = None
        hof = False
        if mask is not None:
            hb_off = np.zeros_like(inhead)
            hb_off[max(0, ty0 + head_off[1]) : ty1 + head_off[1], max(0, tx0 + head_off[0]) : tx1 + head_off[0]] = True
            om = mask & (inhead | hb_off)
            if om.any():
                occ = np.zeros(ctx.base.shape[:2] + (4,), np.uint8)
                occ[om, :3] = frame[om]
                occ[om, 3] = 255
                occ_rel = f"body/{fname(name)}_occ.png"
                R.save_rgba(occ, rd / occ_rel)
                hof = bool((om & ctx.face_zone_canvas).any())
        return {"file": rel, "head_offset": list(head_off), "occluder": occ_rel, "face": "rig", "hand_over_face": hof}

    yc = chest_line(ctx)
    breathe = shift_vert(ctx.base, ctx.sil, -1, yc)
    gestures = {"idle_breathe": {"fps": 1.2, "loop": True, "hold": None, "exit": "cut", "frames": [
        write_frame("idle_breathe_0", ctx.base, None), write_frame("idle_breathe_1", breathe, None, (0, -1))]}}
    written: dict[str, dict] = {}
    reach_now = {x: reach_metrics(ctx, hbox, frames[x]["frame"], frames[x]["mask"], skin) for x in REACH_CLIPS if x in frames}
    clips = dict(GESTURE_CLIPS)
    c0 = reach_now.get("clap_0", {}).get("inside_pct")
    c1 = reach_now.get("clap_1", {}).get("inside_pct")
    c0 = 100 if c0 is None else c0
    c1 = 100 if c1 is None else c1
    happy_mode = "clap"
    ch = {x: (reach_now.get(x, {}).get("inside_pct") or 0) if x in frames and not frames[x]["bad"] else 0
          for x in ("cheer_1", "cheer_2", "cheer_3")}
    if c0 >= REACH_MIN and c1 >= REACH_MIN:
        pass
    elif ch["cheer_1"] >= CHEER_MIN and ch["cheer_2"] >= CHEER_MIN:
        clips["react_happy"] = (["cheer_1", "cheer_2", "cheer_1^", "cheer_2"], 6, True, None, "cut", None)
        happy_mode = "thumbs-up beside the face"
    elif max(ch.values()) >= CHEER_MIN:
        best_ch = max(ch, key=ch.get)
        clips["react_happy"] = ([best_ch, best_ch + "^"], 6, True, None, "cut", None)
        happy_mode = f"{best_ch} + bob"
    elif c1 < REACH_MIN <= c0:
        clips["react_happy"] = (["clap_0", "clap_0^"], 6, True, None, "cut", None)
        happy_mode = "hands-up bounce"
    else:  # nothing with hands fits the reaction cam: a hands-free laughing bounce (the face does the work)
        clips["react_happy"] = None
        happy_mode = "laughing bounce (no hands)"
    rs = reach_now.get("react_shock_1", {}).get("inside_pct")
    if rs is not None and rs < 60:
        flags.append(f"react_shock: only {rs}% of the hands inside the reaction cam")
    for g, spec in clips.items():
        if spec is None:
            continue
        names, fps, loop, hold, ex, phases = spec
        fr_list = []
        ok = True
        for nm in names:
            bob = -1 if nm.endswith("^") else (1 if nm.endswith("v") and nm[-2:-1].isdigit() else 0)
            src = nm[:-1] if bob else nm
            if src not in frames:
                ok = False
                break
            if nm not in written:
                r = frames[src]
                if bob:  # upper body (head + shoulders + what moves with them) one pixel up / down
                    region = ctx.sil | r["mask"]
                    cut = None if g == "laugh" else yc
                    fr = shift_vert(r["frame"], region, bob, cut)
                    m2 = r["mask"].copy()
                    if bob < 0:
                        m2[:-1] |= r["mask"][1:]
                    else:
                        m2[1:] |= r["mask"][:-1]
                    written[nm] = write_frame(nm, fr, m2, (0, bob))
                else:
                    written[nm] = write_frame(nm, r["frame"], r["mask"])
            fr_list.append(written[nm])
        if ok:
            clip = {"fps": fps, "loop": loop, "hold": hold, "exit": ex, "frames": fr_list}
            if phases:
                clip["phases"] = phases
                clip["hold_fps"] = HOLD_FPS
            gestures[g] = clip
        else:
            flags.append(f"gesture {g}: missing frames")

    if clips.get("react_happy") is None:  # bounce: the breathing pair at laughing speed (head_offset 0 / -1)
        gestures["react_happy"] = {"fps": 7, "loop": True, "hold": None, "exit": "cut",
                                   "frames": [dict(f) for f in gestures["idle_breathe"]["frames"]]}
    transitions = {}
    if all(e in frames for e in ENTER) and "talk_hands" in gestures:
        ent = [write_frame(e, frames[e]["frame"], frames[e]["mask"]) for e in ENTER]
        transitions = {"enter_from_base": {"to": "talk_hands", "fps": 12, "loop": False, "hold": None, "exit": "cut",
                                           "frames": ent},
                       "exit_to_base": {"from": "talk_hands", "fps": 12, "loop": False, "hold": None, "exit": "cut",
                                        "frames": list(reversed(ent))}}
    reach = {x: reach_metrics(ctx, hbox, frames[x]["frame"], frames[x]["mask"], skin) for x in REACH_CLIPS if x in frames}

    hb = ctx.rigd["head"]
    shift = lambda b4: [b4[0] + sx0 - tx0, b4[1] + sy0 - ty0, b4[2] + sx0 - tx0, b4[3] + sy0 - ty0]  # noqa: E731
    eb, mb = shift(hb["eyes_box"]), shift(hb["mouth_box"])
    rigj = {
        "schema": R.SCHEMA, "team": team, "kind": "rig", "generated_at": R.now(), "face_type": kind,
        "native": {"w": n, "h": n, "scale": ctx.scale, "out": n * ctx.scale},
        "background": "bg.png", "body_alpha": "opaque", "pointing": "screen-left",
        "head": {"box": hbox, "image": "head.png", "face_mask": "face_mask.png",
                 "mouth_anchor": [int(round((mb[0] + mb[2]) / 2)), int(round((mb[1] + mb[3]) / 2))],
                 "mouth_box": mb, "eye_line": int(round((eb[1] + eb[3]) / 2)), "eyes_box": eb,
                 "eye_centres": [[int(eb[0] + (eb[2] - eb[0]) * 0.25), int(round((eb[1] + eb[3]) / 2))],
                                 [int(eb[0] + (eb[2] - eb[0]) * 0.75), int(round((eb[1] + eb[3]) / 2))]],
                 "generation_crop": list(ctx.box)},
        "visemes": visemes, "viseme_fallback": R.VISEME_FALLBACK,
        "eyes": eyes, "eyes_fallback": R.EYES_FALLBACK,
        "gestures": gestures, "gesture_fallback": R.GESTURE_FALLBACK,
        "transitions": transitions,
        "qa": {"status": "ok", "viseme_source": f"{mdl} 2x2 face sheets, zone-locked", "flags": flags,
               "glance_candidates": glance_log, "reach": reach, "react_happy": happy_mode,
               "bg_text": {**(bg_chk or {}), **bg_info},
               "face": face_metrics, "body": body_metrics, "face_check": fc, "silhouette_px": int(ctx.sil.sum()),
               "zones": zinfo},
    }
    (rd / "rig.json").write_text(json.dumps(rigj, indent=2))

    # ---- 6. QA: pixel check, previews, one vision check over a frame sheet
    rig = R.Rig(rd)
    hc = R.head_check(rig)
    rigj["qa"].update(hc)
    rigj["qa"].update(R.preview(rd))
    qa_sheet = ctx.raw / "qa_sheet.png"
    tiles = []
    for g in R.GESTURES:
        for i in range(len((gestures.get(g) or {}).get("frames", []))):
            if g in ("idle_breathe",) and i:
                continue
            tiles.append(rig.compose(g, i)[..., :3])
    for v in ("wide_open", "round", "laugh"):
        a = rig.compose(viseme=v)
        tiles.append(np.asarray(Image.fromarray(R.crop(a, ctx.box)[..., :3]).resize((n, n), Image.NEAREST)))
    for e in ("angry", "surprised", "look_left"):
        a = rig.compose(eyes=e)
        tiles.append(np.asarray(Image.fromarray(R.crop(a, ctx.box)[..., :3]).resize((n, n), Image.NEAREST)))
    for mood in ("happy", "angry"):
        if mood in visemes:
            a = rig.compose(eyes=mood, mood=mood, viseme="medium_open")
            tiles.append(np.asarray(Image.fromarray(R.crop(a, ctx.box)[..., :3]).resize((n, n), Image.NEAREST)))
    cols = 5
    rows = (len(tiles) + cols - 1) // cols
    sheet = np.zeros((rows * n, cols * n, 3), np.uint8)
    for i, t in enumerate(tiles):
        sheet[(i // cols) * n : (i // cols + 1) * n, (i % cols) * n : (i % cols + 1) * n] = t
    Image.fromarray(sheet).resize((cols * n * 2, rows * n * 2), Image.NEAREST).save(qa_sheet)
    vis = None
    if not args.no_check:
        sig = json.dumps({"v": RIG_VERSION, **{k: v.get("file") for k, v in {**face_metrics, **body_metrics}.items()}},
                         sort_keys=True)
        if st.get("vision_for") == sig and st.get("vision"):
            vis = st.get("vision")
        else:
            vis = vision_json(ctx, "sheet_check", [ctx.tdir / "base.png", qa_sheet], sheet_check_prompt(len(tiles)))
            if vis:
                st.set("vision", vis)
                st.set("vision_for", sig)
    if vis:
        rigj["qa"]["vision"] = vis
        for key in ("same_character", "same_outfit", "face_consistent"):
            if vis.get(key) is False:
                flags.append(f"vision: {key} = false ({vis.get('notes', '')})")
        if vis.get("text_or_logo"):
            flags.append(f"vision: text/logo on the character ({vis.get('notes', '')})")
        if vis.get("weapon_visible"):
            flags.append("vision: weapon visible")
        bf = vis.get("broken_frames") or []
        if bf and len(bf) <= 6:  # a long list at this preview size is the checker guessing, not a finding
            flags.append(f"vision: check hands in QA frames {bf} ({vis.get('notes', '')})")
    if not hc["head_identical"]:
        flags.append(f"head pixels differ: {hc['head_diff_frames']}")
    serious = [f for f in flags if f.startswith(("head pixels", "vision: same_character", "vision: weapon", "vision: text"))
               or "missing frames" in f]
    rigj["qa"]["status"] = "ok" if not serious and not any(r["bad"] for r in frames.values()) else "partial"
    if flags and any(f.startswith("face_type") for f in flags):  # e.g. fugu/fable: old frames had a beak/snout
        rigj["qa"]["legacy_frames_rebuilt"] = rebuild_old_frames(args, team)
        ctx.meta.update(face_type=kind, face_type_fixed_by="rig vision check")
        (ctx.tdir / "meta.json").write_text(json.dumps(ctx.meta, indent=2))
    cost = team_rig_cost(args.out, team)
    rigj["qa"]["cost_usd"] = cost
    rigj["qa"]["pixellab_generations"] = 0
    rigj["qa"]["flags"] = flags
    (rd / "rig.json").write_text(json.dumps(rigj, indent=2))
    return {"team": team, "status": rigj["qa"]["status"], "cost": round(budget.spent - spent0, 4), "total_cost": cost,
            "flags": flags, "head_identical": hc["head_identical"], "frames": sum(len(g["frames"]) for g in gestures.values())}


def tight_head_box(ctx) -> list[int]:
    """Head box hugging the head (hair top .. chin, ear to ear) and containing both face zones: drives the show's
    face crops. The square generation crop (ctx.box) stays internal."""
    x0, y0, x1, y1 = ctx.box
    S = x1 - x0
    zu = ctx.zones["eye"] | ctx.zones["mouth"]
    zy, zx = np.nonzero(zu)
    sil = ctx.sil
    eb = ctx.rigd["head"]["eyes_box"]
    cx = x0 + (eb[0] + eb[2]) // 2
    band = sil[0:y1, max(0, cx - S // 6) : cx + S // 6]
    rows = np.nonzero(band.any(axis=1))[0]
    top = int(rows[0]) if len(rows) else y0
    bottom = y0 + int(zy.max()) + 2
    left, right = x0 + int(zx.min()), x0 + int(zx.max()) + 1
    mouth_top = y0 + int(np.nonzero(ctx.zones["mouth"].any(axis=1))[0][0])
    for y in range(top, min(mouth_top, y1)):
        row = sil[y]
        if not row[cx]:
            continue
        l = cx
        while l > x0 and row[l - 1]:
            l -= 1
        r = cx
        while r < x1 - 1 and row[r + 1]:
            r += 1
        left, right = min(left, l), max(right, r + 1)
    box = [max(x0, left - 1), max(0, top - 1), min(x1, right + 1), min(y1, bottom + 1)]
    if box[2] - box[0] < 0.4 * S or box[3] - box[1] < 0.5 * S:  # silhouette failed: fall back to the square crop
        return list(ctx.box)
    return box


# ============================================================================ glances (SCREEN directions, measured)
def mirror_eyes(ctx, patch: np.ndarray) -> np.ndarray:
    """Fallback glance: each eye of a glance sprite flipped left-right in place (the iris lands on the other side)."""
    head = ctx.head
    m = patch[..., 3] > 0
    sprite = head.copy()
    sprite[m] = patch[m][:, :3]
    new = head.copy()
    eb = ctx.rigd["head"]["eyes_box"]
    mid = (eb[0] + eb[2]) // 2
    for lo, hi in ((0, mid), (mid, head.shape[1])):
        mm = m.copy()
        mm[:, :lo] = False
        mm[:, hi:] = False
        ys, xs = np.nonzero(mm)
        if not len(xs):
            continue
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        new[y0:y1, x0:x1] = sprite[y0:y1, x0:x1][:, ::-1]
    return R.make_patch(head, new, ctx.zones["eye"])


def lens_glances(ctx) -> dict[str, np.ndarray]:
    """Robot eyes are plain lenses: draw the glance directly - a dark round pupil on the screen-left / screen-right
    side of each lens (exact, symmetric, no model guesswork)."""
    head = ctx.head
    zone = ctx.zones["eye"]
    lum = R._lum(head)
    eb = ctx.rigd["head"]["eyes_box"]
    band = np.zeros(zone.shape, bool)
    band[max(0, eb[1] - 12) : eb[3] + 12, max(0, eb[0] - 6) : eb[2] + 6] = True
    bright = band & (lum > 150)
    bright = ndi.binary_fill_holes(ndi.binary_closing(bright, iterations=1))
    lab, k = ndi.label(bright)
    if k < 2:
        return {}
    sizes = ndi.sum(bright, lab, np.arange(1, k + 1))
    lenses = [j + 1 for j in np.argsort(-sizes)[:2]]
    dark = ctx.palette[np.argmin(ctx.palette.sum(axis=1))]
    out = {}
    for st, sgn in (("look_left", -1), ("look_right", 1)):
        img = head.copy()
        for j in lenses:
            lens = ndi.binary_fill_holes(lab == j)
            ys, xs = np.nonzero(lens)
            cy, cx = ys.mean(), xs.mean()
            rad = max(2.0, (xs.max() - xs.min() + 1) / 2)
            pr = max(1.5, 0.28 * rad)
            px = cx + sgn * 0.42 * rad
            yy, xx = np.mgrid[0 : head.shape[0], 0 : head.shape[1]]
            disc = ((yy - cy) ** 2 + (xx - px) ** 2 <= pr ** 2) & lens
            img[disc] = dark
        out[st] = R.make_patch(head, img)
    return out


def select_glances(ctx, S: int, blink: np.ndarray | None = None) -> tuple[dict, list]:
    """Every look_* candidate from the eye sheets (and each one mirrored eye by eye) is MEASURED - with the show
    renderer's own white-of-the-eye measure and with the iris centroid - and the (look_left, look_right) PAIR that the
    renderer reads as screen-left / screen-right, with the iris agreeing, wins. Labels from the prompt are ignored.
    Returns ({state: patch-record}, candidate log)."""
    eb = ctx.rigd["head"]["eyes_box"]
    opening = R.eye_opening(blink)
    head = ctx.head
    cands = []
    for sheet in ("eyes_b", "eyes_glance"):
        for f in raw_files(ctx, f"face_{sheet}"):
            for name, r in extract_patches(ctx, sheet, f).items():
                if name.split("#")[0] not in GLANCES:
                    continue
                bad = [b_ for b_ in face_ok(name.split("#")[0], r["metrics"], S) if b_ != "weak"]
                if bad:
                    continue
                for mirrored in (False, True):
                    patch = mirror_eyes(ctx, r["patch"]) if mirrored else r["patch"]
                    if not (patch[..., 3] > 0).any():
                        continue
                    comp = head.copy()
                    m = patch[..., 3] > 0
                    comp[m] = patch[m][:, :3]
                    iris = R.glance_dx(head, patch, eb, opening)
                    cands.append({"patch": patch, "comp": comp, "metrics": r["metrics"], "file": f.name,
                                  "cell": name + (" (mirrored)" if mirrored else ""), "iris": iris,
                                  "idir": R.glance_dir(iris, 0.3), "mirrored": mirrored})
    if ctx.kind == "robot":  # drawn pupils: exact and symmetric
        for st, patch in lens_glances(ctx).items():
            comp = head.copy()
            m = patch[..., 3] > 0
            comp[m] = patch[m][:, :3]
            iris = R.glance_dx(head, patch, eb, opening)
            cands.append({"patch": patch, "comp": comp, "metrics": {"changed_px": int(m.sum())}, "file": "drawn",
                          "cell": f"{st} (drawn pupil)", "iris": iris, "idir": R.glance_dir(iris, 0.3), "mirrored": False})
    best, best_score = None, None
    drawn = {c["cell"].split(" ")[0]: c for c in cands if c["file"] == "drawn"}
    if len(drawn) == 2:  # a robot's drawn pupils win whenever the renderer reads them the right way
        rd = R.renderer_look_dirs(head, {"look_left": drawn["look_left"]["comp"], "look_right": drawn["look_right"]["comp"]})
        if rd["look_left"][0] == -1 and rd["look_right"][0] == 1:
            best, best_score = (drawn["look_left"], drawn["look_right"], rd), (9, 0, 0)
    lefts = [c for c in cands if c["idir"] != 1] if best is None else []
    rights = [c for c in cands if c["idir"] != -1]
    for cl in lefts:
        for cr in rights:
            if cl is cr:
                continue
            rd = R.renderer_look_dirs(head, {"look_left": cl["comp"], "look_right": cr["comp"]})
            if rd["look_left"][0] != -1 or rd["look_right"][0] != 1:
                continue
            margin = min(rd["look_left"][1], -rd["look_right"][1])
            iris_ok = (cl["idir"] == -1) + (cr["idir"] == 1)
            score = (iris_ok, -(cl["mirrored"] + cr["mirrored"]), margin)
            if best_score is None or score > best_score:
                best, best_score = (cl, cr, rd), score
    out = {}
    if best:
        for st, c in (("look_left", best[0]), ("look_right", best[1])):
            out[st] = {"patch": c["patch"], "bad": [], "file": c["file"],
                       "metrics": {**c["metrics"], "cell": c["cell"], "iris_dx": c["iris"],
                                   "renderer": list(best[2][st])}}
    log = [{"file": c["file"], "cell": c["cell"], "iris": c["iris"], "idir": c["idir"]} for c in cands]
    return out, log


# ============================================================================ reaction-cam reach (hands near the face)
REACH_SIDE = 145  # the show's 290 px reaction cam shows a 145 native px bust crop (whole-number x2) on every rig


def reach_box(hbox: list[int]) -> list[float]:
    """The show's reaction cam: a bust crop ~1.3 head-heights square on 256-native rigs (145 native px on any rig),
    centred on the face, from just above the hair down (media/gmbench_media/rigview.py crop='bust')."""
    x0, y0, x1, y1 = hbox
    side_ = max(1.3 * max(x1 - x0, y1 - y0), REACH_SIDE)
    cx = (x0 + x1) / 2
    return [cx - side_ / 2, y0 - 0.1 * side_, cx + side_ / 2, y0 + 0.9 * side_]


def skin_colours(ctx, hbox: list[int]) -> set:
    """Colours of the face (mouth zone) that are rare in the torso: skin for people, metal for the robot's hands."""
    zone = ctx.head[ctx.zones["mouth"]]
    cols, counts = np.unique(zone.reshape(-1, 3), axis=0, return_counts=True)
    torso = ctx.base[min(ctx.base.shape[0] - 1, hbox[3] + 6):]
    tc, tn = np.unique(torso.reshape(-1, 3), axis=0, return_counts=True)
    tfreq = {tuple(c): n / max(1, tn.sum()) for c, n in zip(tc, tn)}
    keep = set()
    for c, n in zip(cols, counts):
        if n / max(1, counts.sum()) >= 0.02 and tfreq.get(tuple(c), 0) < 0.01 and int(c.max()) > 60:
            keep.add(tuple(int(v) for v in c))
    return keep


def reach_metrics(ctx, hbox: list[int], frame: np.ndarray, mask: np.ndarray, skin: set) -> dict:
    x0, y0, x1, y1 = hbox
    inhead = np.zeros(mask.shape, bool)
    inhead[y0:y1, x0:x1] = True
    cand = mask & ~inhead
    ys, xs = np.nonzero(cand)
    if not len(xs) or not skin:
        return {"hand_px": 0, "inside_pct": None}
    px = frame[ys, xs]
    is_skin = np.array([tuple(int(v) for v in c) in skin for c in px])
    ys, xs = ys[is_skin], xs[is_skin]
    if not len(xs):
        return {"hand_px": 0, "inside_pct": None}
    bx0, by0, bx1, by1 = reach_box(hbox)
    inside = (xs >= bx0) & (xs < bx1) & (ys >= by0) & (ys < by1)
    return {"hand_px": int(len(xs)), "inside_pct": round(float(inside.mean()) * 100, 1),
            "box": [round(v, 1) for v in (bx0, by0, bx1, by1)]}


# ============================================================================ background text (static plate)
def bg_clean_fix(ctx, raw_file: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    """(mask, pixels): background spots the text-removal edit repainted, far from the character (never on it)."""
    img = np.asarray(Image.open(raw_file).convert("RGB"))
    n = ctx.base.shape[0]
    nat = sample_native(img, img.shape[0] / n)
    dx, dy, frac = best_shift(ctx.base, nat, np.ones(ctx.base.shape[:2], bool), r=4)
    q = snap(place(nat, ctx.base.shape, dx, dy, ctx.base), ctx.palette)
    strong = R.diff_mask(q, ctx.base, 40)
    keepout = ndi.binary_dilation(ctx.sil, iterations=3)
    dens = ndi.uniform_filter(strong.astype(np.float32), size=5) > 0.12
    cand = ndi.binary_closing(dens | ndi.binary_opening(strong, structure=np.ones((2, 2), bool)), iterations=2) & ~keepout
    lab, k = ndi.label(cand, structure=np.ones((3, 3), bool))
    keep = np.zeros_like(cand)
    anydiff = R.diff_mask(q, ctx.base, 0)
    H, W = cand.shape
    for j, sl in enumerate(ndi.find_objects(lab), start=1):
        if sl is None or (lab[sl] == j).sum() < 12:
            continue
        # the whole repainted patch around a text blob (letters near the picture edge are thin and sparse)
        y0, y1 = max(0, sl[0].start - 6), min(H, sl[0].stop + 6)
        x0, x1 = max(0, sl[1].start - 6), min(W, sl[1].stop + 6)
        keep[y0:y1, x0:x1] |= anydiff[y0:y1, x0:x1]
    keep &= ~keepout
    return keep, q, {"shift": [dx, dy], "match": round(frac, 3), "fixed_px": int(keep.sum())}


def team_rig_cost(out_root: Path, team: str) -> float:
    total = 0.0
    p = out_root / team / "raw" / "calls.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            if str(r.get("variant", "")).startswith("rig_"):
                total += r.get("cost") or 0.0
    return round(total, 4)


def rebuild_old_frames(args, team: str) -> list[str]:
    """Rewrite the legacy mid/wide(/_blink) frames from the rig's medium_open / wide_open patches (the 3-state
    lip-flap path used before the rig driver lands). Used for characters whose old mouth frames were wrong."""
    tdir = args.out / team
    rig = R.Rig(tdir / "rig")
    d = rig.data
    k = d["native"]["scale"]
    done = []
    for old, v in (("mid", "medium_open"), ("wide", "wide_open")):
        for suffix, eyes in (("", "open"), ("_blink", "blink")):
            a = rig.compose("idle_breathe", 0, eyes=eyes, viseme=v)[..., :3]
            G.save_png(R.upscale(a, k).convert("RGB"), tdir / f"{old}{suffix}.png", "pixel")
            done.append(f"{old}{suffix}.png")
    return done
