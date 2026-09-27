"""Face-lock + drift QA for avatar frames.

Given the raw base image and the three raw edits (mid / wide mouth, blink), produce
768x768 frames where ONLY the mouth region (mid, wide) or the eye region (blink) differs
from the base, plus the mouth+blink combos, and measure how much each edit drifted.

pixel   : detect the model's pixel grid, sample one colour per cell, shared palette,
          lock on the cell grid, nearest-neighbour upscale to exactly 768.
painted : masks from a blurred diff, feathered alpha blend, Lanczos to 768.

Masks come from what each edit actually changed: noise specks are dropped, the eye mask
is the blink's biggest one or two change blobs, and the mouth mask is the biggest change
blob BELOW the eyes (so a mouth edit that also furrowed the brows gets its brows thrown
away by the lock, and that area is reported as "dropped" drift).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi

OUT = 768
EDITS = ("mid", "wide", "blink")

# QA thresholds (percent of image area). Calibrated on the Sep 2026 spike + first test run.
OUT_MAX = {"pixel": 2.5, "painted": 1.0}  # changed px outside the edit region
DROP_MAX = 1.5  # strong changes the lock had to throw away (brows, mouth-in-blink, ...)
MASK_MAX = {"mid": 8.0, "wide": 10.0, "blink": 6.0}  # region implausibly large -> face redrawn
WEAK_MIN = {"mid": 0.04, "wide": 0.08, "blink": 0.04}  # edit too small to read


# ============================================================================ helpers
def load_rgb(path: Path | str) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def _diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)


def _components(mask: np.ndarray, min_size: float):
    lab, n = ndi.label(mask, structure=np.ones((3, 3), bool))
    if n == 0:
        return lab, []
    idx = np.arange(1, n + 1)
    sizes = ndi.sum(mask, lab, idx)
    coms = ndi.center_of_mass(mask, lab, idx)
    boxes = ndi.find_objects(lab)
    comps = [
        {"label": int(i), "size": float(s), "cy": float(c[0]), "cx": float(c[1]), "box": b}
        for i, s, c, b in zip(idx, sizes, coms, boxes)
        if s >= min_size
    ]
    return lab, comps


def _grow(mask: np.ndarray, r: int) -> np.ndarray:
    if r > 0 and mask.any():
        mask = ndi.binary_dilation(mask, structure=np.ones((3, 3), bool), iterations=r)
    return ndi.binary_fill_holes(mask)


def _in_box(c: dict, box, pad: float) -> bool:
    ys, xs = box
    h, w = ys.stop - ys.start, xs.stop - xs.start
    return (ys.start - pad * h <= c["cy"] <= ys.stop + pad * h) and (xs.start - pad * w <= c["cx"] <= xs.stop + pad * w)


def select_regions(strong: dict[str, np.ndarray], min_size: float):
    """strong: variant -> bool map of strong changes vs base. Returns (mouth, eyes, info)."""
    shape = next(iter(strong.values())).shape
    info: dict = {}
    # --- eyes: the blink's one or two biggest blobs (+ small fragments around them)
    lab_b, comps_b = _components(strong["blink"], min_size)
    eyes = np.zeros(shape, bool)
    eye_cy = None
    if comps_b:
        comps_b.sort(key=lambda c: -c["size"])
        top = comps_b[:6]
        keep = [top[0]]
        best = 0.0  # prefer the biggest PAIR of blobs at the same height (two eyes), else the single biggest
        for i in range(len(top)):
            for j in range(i + 1, len(top)):
                a, b = top[i], top[j]
                ha = a["box"][0].stop - a["box"][0].start
                hb = b["box"][0].stop - b["box"][0].start
                same_row = abs(a["cy"] - b["cy"]) <= 0.6 * max(ha, hb) + 2
                apart = abs(a["cx"] - b["cx"]) > 0.5 * ((a["box"][1].stop - a["box"][1].start) + (b["box"][1].stop - b["box"][1].start)) / 2
                balanced = min(a["size"], b["size"]) >= 0.3 * max(a["size"], b["size"])
                if same_row and apart and balanced and a["size"] + b["size"] > best:
                    best, keep = a["size"] + b["size"], [a, b]
        ys = slice(min(c["box"][0].start for c in keep), max(c["box"][0].stop for c in keep))
        xs = slice(min(c["box"][1].start for c in keep), max(c["box"][1].stop for c in keep))
        keep += [c for c in comps_b if c not in keep and _in_box(c, (ys, xs), 0.15)]
        eyes = np.isin(lab_b, [c["label"] for c in keep])
        eye_cy = float(np.mean(np.nonzero(eyes)[0]))
        info["eye_box"] = [xs.start, ys.start, xs.stop, ys.stop]
    # --- mouth: biggest blob of mid|wide whose centre is below the eyes (+ fragments)
    union = strong["mid"] | strong["wide"]
    lab_m, comps_m = _components(union, min_size)
    mouth = np.zeros(shape, bool)
    cands = [c for c in comps_m if eye_cy is None or c["cy"] > eye_cy]
    if cands:
        primary = max(cands, key=lambda c: c["size"])
        keep_m = [primary] + [c for c in cands if c is not primary and _in_box(c, primary["box"], 0.3)]
        mouth = np.isin(lab_m, [c["label"] for c in keep_m])
        ys, xs = primary["box"]
        info["mouth_box"] = [xs.start, ys.start, xs.stop, ys.stop]
        # a blink edit that also moved the mouth: keep only eye blobs above the mouth
        if eyes.any():
            top = ys.start
            lab_e, comps_e = _components(eyes, 1)
            eyes = np.isin(lab_e, [c["label"] for c in comps_e if c["cy"] < top]) if comps_e else eyes
    info["eye_cy"] = eye_cy
    return mouth, eyes, info


# ============================================================================ pixel grid
def _edge_signal(a: np.ndarray, axis: int) -> np.ndarray:
    g = a.astype(np.float32).mean(axis=2)
    return np.abs(np.diff(g, axis=axis)).sum(axis=0 if axis == 1 else 1)


def _period(sig: np.ndarray, pmin: float = 3.5, pmax: float = 16.0) -> tuple[float, float]:
    s = (sig - sig.mean()) * np.hanning(len(sig))
    n = 1 << 15
    spec = np.abs(np.fft.rfft(s, n=n))
    fr = np.fft.rfftfreq(n)
    band = (fr >= 1 / pmax) & (fr <= 1 / pmin)
    i = int(np.argmax(np.where(band, spec, 0)))
    return 1.0 / fr[i], float(spec[i] / (np.median(spec[band]) + 1e-9))


def _phase(sig: np.ndarray, p: float, buckets: int = 32) -> tuple[float, float]:
    c = np.arange(len(sig)) + 1.0  # diff index i is the boundary at x = i + 1
    k = np.floor((c % p) / p * buckets).astype(int) % buckets
    sums = np.bincount(k, weights=sig, minlength=buckets)
    pair = sums + np.roll(sums, -1)
    j = int(np.argmax(pair))
    return ((j + 1) / buckets * p) % p, float(pair[j] / sig.sum() * buckets / 2)


def detect_grid(a: np.ndarray) -> dict:
    gx, gy = _edge_signal(a, 1), _edge_signal(a, 0)
    px, prom_x = _period(gx)
    py, prom_y = _period(gy)
    ox, sx = _phase(gx, px)
    oy, sy = _phase(gy, py)
    ok = min(sx, sy) >= 1.8 and abs(px - py) / px < 0.03
    return {"px": px, "py": py, "ox": ox, "oy": oy, "score": round(min(sx, sy), 2),
            "prominence": round(min(prom_x, prom_y), 1), "ok": bool(ok)}


def sample_cells(a: np.ndarray, grid: dict) -> np.ndarray:
    h, w = a.shape[:2]

    def centres(p: float, o: float, size: int) -> np.ndarray:
        k0 = int(np.ceil(-o / p))
        k1 = int(np.floor((size - o) / p)) - 1
        return o + (np.arange(k0, k1 + 1) + 0.5) * p

    ix = np.clip(np.round(centres(grid["px"], grid["ox"], w) - 0.5).astype(int), 0, w - 1)
    iy = np.clip(np.round(centres(grid["py"], grid["oy"], h) - 0.5).astype(int), 0, h - 1)
    r = 1 if min(grid["px"], grid["py"]) >= 4.5 else 0
    stack = [a[np.clip(iy + dy, 0, h - 1)][:, np.clip(ix + dx, 0, w - 1)]
             for dy in range(-r, r + 1) for dx in range(-r, r + 1)]
    return np.median(np.stack(stack), axis=0).astype(np.uint8)


def fit_plan(n: int) -> dict:
    """Crop/pad a logical n x n image to L x L with L*k == 768 (crisp integer scaling)."""
    best = None
    for k in (2, 3, 4, 6, 8):
        size = OUT // k
        d = n - size
        if d > 0.16 * n or -d > 0.10 * n:
            continue
        cost = d / n if d >= 0 else 1.5 * (-d) / n
        if best is None or cost < best["cost"]:
            best = {"k": k, "L": size, "cost": cost, "n": n}
    return best or {"k": None, "L": None, "cost": None, "n": n}


def apply_fit(a: np.ndarray, plan: dict) -> np.ndarray:
    h, w = a.shape[:2]
    n = min(h, w)
    a = a[(h - n) // 2 : (h - n) // 2 + n, (w - n) // 2 : (w - n) // 2 + n]
    if plan["L"] is None:
        return a
    d = n - plan["L"]
    if d > 0:
        s = d // 2
        return a[s : s + plan["L"], s : s + plan["L"]]
    if d < 0:
        s, e = (-d) // 2, (-d) - (-d) // 2
        pad = ((s, e), (s, e)) + (((0, 0),) if a.ndim == 3 else ())
        return np.pad(a, pad, mode="reflect")
    return a


def upscale(a: np.ndarray, plan: dict) -> Image.Image:
    img = Image.fromarray(a if a.dtype == np.uint8 else (a * 255).astype(np.uint8))
    return img.resize((OUT, OUT), Image.NEAREST)


def build_palette(frames: list[np.ndarray], face: tuple[int, int, int, int] | None, colours: int = 48) -> Image.Image:
    parts = [f.reshape(-1, 3) for f in frames]
    if face:
        x0, y0, x1, y1 = face
        parts += [f[y0:y1, x0:x1].reshape(-1, 3) for f in frames] * 4
    src = np.concatenate(parts)
    side = int(np.ceil(np.sqrt(len(src))))
    src = np.concatenate([src, np.repeat(src[:1], side * side - len(src), axis=0)])
    return Image.fromarray(src.reshape(side, side, 3)).quantize(
        colors=colours, method=Image.Quantize.MAXCOVERAGE, kmeans=8, dither=Image.Dither.NONE
    )


def quantize(a: np.ndarray, pal: Image.Image) -> np.ndarray:
    return np.asarray(Image.fromarray(a).quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB"))


# ============================================================================ QA
def judge(variant: str, style: str, m: dict) -> dict:
    reasons = []
    if m["outside_pct"] > OUT_MAX[style]:
        reasons.append("drift: changes outside the edit region")
    if m["dropped_pct"] > DROP_MAX:
        reasons.append("drift: expression/extra changes thrown away by the lock")
    if m["mask_pct"] > MASK_MAX[variant]:
        reasons.append("drift: edit region implausibly large (face redrawn?)")
    if m.get("grid_delta", 0) > 0.02:
        reasons.append("drift: pixel grid rescaled")
    weak = m["strong_pct"] < WEAK_MIN[variant]
    if weak:
        reasons.append("weak: edit barely visible")
    kind = None
    if any(r.startswith("drift") for r in reasons):
        kind = "drift"
    elif weak:
        kind = "weak"
    return {**m, "bad": kind is not None, "retry": kind, "reasons": reasons}


def score(m: dict) -> float:
    """Lower is better; used to pick between two attempts of the same edit."""
    return (10 if m["bad"] else 0) + m["outside_pct"] + m["dropped_pct"] + (5 if m["retry"] == "weak" else 0)


# ============================================================================ main entry
def process(style: str, base_path: Path, edit_paths: dict[str, Path], allow_swap: bool = True) -> dict:
    base = load_rgb(base_path)
    edits = {}
    resized = {}
    for v, p in edit_paths.items():
        e = load_rgb(p)
        resized[v] = e.shape != base.shape
        if resized[v]:
            e = np.asarray(Image.fromarray(e).resize(base.shape[1::-1], Image.LANCZOS))
        edits[v] = e
    if style == "pixel":
        res = _pixel(base, edits, allow_swap)
    else:
        res = _painted(base, edits, allow_swap)
    for v in EDITS:
        if resized.get(v):
            res["metrics"][v]["reasons"].append("drift: edit came back at a different size")
            res["metrics"][v].update(bad=True, retry="drift")
    return res


def _finish(style: str, metrics: dict, strong_in_mouth: dict, allow_swap: bool) -> tuple[dict, bool]:
    out = {v: judge(v, style, metrics[v]) for v in EDITS}
    swapped = allow_swap and strong_in_mouth["wide"] > 0 and strong_in_mouth["mid"] > 1.2 * strong_in_mouth["wide"]
    return out, swapped


def _pixel(base: np.ndarray, edits: dict[str, np.ndarray], allow_swap: bool = True) -> dict:
    grid = detect_grid(base)
    if grid["ok"]:
        logical = {"base": sample_cells(base, grid), **{v: sample_cells(e, grid) for v, e in edits.items()}}
    else:  # no clean grid: plain pixelation to 192
        small = lambda a: np.asarray(Image.fromarray(a).resize((192, 192), Image.BOX))  # noqa: E731
        logical = {"base": small(base), **{v: small(e) for v, e in edits.items()}}
    n = min(logical["base"].shape[:2])
    plan = fit_plan(n)
    logical = {k: apply_fit(v, plan) for k, v in logical.items()}
    lb = logical["base"]
    area = lb.shape[0] * lb.shape[1]

    strong = {v: _diff(logical[v], lb) > 40 for v in EDITS}
    mouth_core, eyes_core, info = select_regions(strong, min_size=3)
    eyes = _grow(eyes_core, 1)
    mouth = _grow(mouth_core, 1) & ~eyes

    # palette: all frames, face-weighted (union box of both regions, generously padded)
    face = None
    ys, xs = np.nonzero(mouth | eyes)
    if len(ys):
        h, w = ys.max() - ys.min(), xs.max() - xs.min()
        face = (max(0, xs.min() - w), max(0, ys.min() - h), min(lb.shape[1], xs.max() + w), min(lb.shape[0], ys.max() + h // 2))
    pal = build_palette(list(logical.values()), face)
    q = {k: quantize(v, pal) for k, v in logical.items()}

    metrics, strong_in_mouth = {}, {}
    for v in EDITS:
        region = eyes if v == "blink" else mouth
        core = eyes_core if v == "blink" else mouth_core
        changed = _diff(q[v], q["base"]) > 24  # same visibility threshold as painted
        outside = ~_grow(mouth | eyes, 1)  # drift = change outside BOTH animated regions
        dropped = strong[v] & ~_grow(mouth | eyes, 2)
        # a drop only counts if it is a real blob (not palette noise)
        _, dcomps = _components(dropped, 3)
        g = detect_grid(edits[v]) if grid["ok"] else None
        metrics[v] = {
            "mask_pct": round(region.sum() / area * 100, 3),
            "strong_pct": round((strong[v] & core).sum() / area * 100, 3),
            "outside_pct": round((changed & outside).sum() / outside.sum() * 100, 3),
            "dropped_pct": round(sum(c["size"] for c in dcomps) / area * 100, 3),
            "grid_delta": round(abs(g["px"] - grid["px"]) / grid["px"], 4) if g else 0.0,
        }
        strong_in_mouth[v] = int((strong[v] & mouth).sum())

    metrics, swapped = _finish("pixel", metrics, strong_in_mouth, allow_swap)
    if swapped:
        q["mid"], q["wide"] = q["wide"], q["mid"]
        metrics["mid"], metrics["wide"] = metrics["wide"], metrics["mid"]

    def lock(src: np.ndarray, m: np.ndarray, onto: np.ndarray) -> np.ndarray:
        out = onto.copy()
        out[m] = src[m]
        return out

    locked = {"base": q["base"]}
    locked["mid"] = lock(q["mid"], mouth, q["base"])
    locked["wide"] = lock(q["wide"], mouth, q["base"])
    locked["blink"] = lock(q["blink"], eyes, q["base"])
    locked["mid_blink"] = lock(q["blink"], eyes, locked["mid"])
    locked["wide_blink"] = lock(q["blink"], eyes, locked["wide"])

    return {
        "frames": {k: upscale(v, plan) for k, v in locked.items()},
        "unlocked": {v: upscale(q[v], plan) for v in EDITS},
        "masks": {"mouth": upscale(mouth.astype(np.uint8) * 255, plan), "eyes": upscale(eyes.astype(np.uint8) * 255, plan)},
        "metrics": metrics,
        "swapped_mid_wide": bool(swapped),
        "info": {
            "grid": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in grid.items()},
            "logical_px": int(lb.shape[0]),
            "scale": plan["k"],
            "fit": {"from": n, "to": plan["L"]},
            "palette_colours": int(len(np.unique(np.concatenate([f.reshape(-1, 3) for f in q.values()]), axis=0))),
            **{k: v for k, v in info.items() if k != "eye_cy"},
        },
    }


def _painted(base: np.ndarray, edits: dict[str, np.ndarray], allow_swap: bool = True) -> dict:
    size = base.shape[0]
    g = 256  # analysis grid

    def small_diff(e: np.ndarray) -> np.ndarray:
        d = Image.fromarray(_diff(e, base).clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
        return np.asarray(d.resize((g, g), Image.BOX)).astype(np.int16)

    sd = {v: small_diff(e) for v, e in edits.items()}
    strong = {v: sd[v] > 22 for v in EDITS}
    mouth_core, eyes_core, info = select_regions(strong, min_size=8)
    eyes = _grow(eyes_core, 2)
    mouth = _grow(mouth_core, 2) & ~_grow(eyes_core, 1)

    def alpha(m: np.ndarray) -> np.ndarray:
        big = Image.fromarray(m.astype(np.uint8) * 255).resize((size, size), Image.NEAREST)
        return np.asarray(big.filter(ImageFilter.GaussianBlur(size / 200))).astype(np.float32)[..., None] / 255.0

    am, ae = alpha(mouth), alpha(eyes)

    def blend(src: np.ndarray, a: np.ndarray, onto: np.ndarray) -> np.ndarray:
        return (onto.astype(np.float32) * (1 - a) + src.astype(np.float32) * a).round().clip(0, 255).astype(np.uint8)

    down = lambda a: Image.fromarray(a).resize((OUT, OUT), Image.LANCZOS)  # noqa: E731
    area = g * g
    base768 = np.asarray(down(base)).astype(np.int16)
    metrics, strong_in_mouth = {}, {}
    for v in EDITS:
        region = eyes if v == "blink" else mouth
        core = eyes_core if v == "blink" else mouth_core
        e768 = np.asarray(down(edits[v])).astype(np.int16)
        changed = np.abs(e768 - base768).max(axis=2) > 24
        both = mouth | eyes
        outside = ~np.asarray(Image.fromarray(_grow(both, 2).astype(np.uint8) * 255).resize((OUT, OUT), Image.NEAREST)).astype(bool)
        dropped = strong[v] & ~_grow(both, 3)
        _, dcomps = _components(dropped, 8)
        metrics[v] = {
            "mask_pct": round(region.sum() / area * 100, 3),
            "strong_pct": round((strong[v] & core).sum() / area * 100, 3),
            "outside_pct": round((changed & outside).sum() / max(1, outside.sum()) * 100, 3),
            "dropped_pct": round(sum(c["size"] for c in dcomps) / area * 100, 3),
        }
        strong_in_mouth[v] = int((sd[v] * mouth).sum())

    metrics, swapped = _finish("painted", metrics, strong_in_mouth, allow_swap)
    e = dict(edits)
    if swapped:
        e["mid"], e["wide"] = e["wide"], e["mid"]
        metrics["mid"], metrics["wide"] = metrics["wide"], metrics["mid"]

    locked = {"base": base}
    locked["mid"] = blend(e["mid"], am, base)
    locked["wide"] = blend(e["wide"], am, base)
    locked["blink"] = blend(e["blink"], ae, base)
    locked["mid_blink"] = blend(e["blink"], ae, locked["mid"])
    locked["wide_blink"] = blend(e["blink"], ae, locked["wide"])
    to_img = lambda m: Image.fromarray(m.astype(np.uint8) * 255).resize((OUT, OUT), Image.NEAREST)  # noqa: E731
    return {
        "frames": {k: down(v) for k, v in locked.items()},
        "unlocked": {v: down(e[v]) for v in EDITS},
        "masks": {"mouth": to_img(mouth), "eyes": to_img(eyes)},
        "metrics": metrics,
        "swapped_mid_wide": bool(swapped),
        "info": {k: v for k, v in info.items() if k != "eye_cy"},
    }


def overlay(base: Image.Image, masks: dict[str, Image.Image]) -> Image.Image:
    """Debug image: mouth region tinted yellow, eye region tinted green."""
    img = base.convert("RGB").copy()
    for key, rgb in (("mouth", (255, 214, 0)), ("eyes", (0, 230, 118))):
        m = masks[key].convert("L")
        tint = Image.new("RGB", img.size, rgb)
        img = Image.composite(Image.blend(img, tint, 0.55), img, m)
    return img


# ============================================================================ gesture poses
def _pixel_logical(a: np.ndarray, grid: dict | None) -> np.ndarray:
    if grid and grid["ok"]:
        return sample_cells(a, grid)
    return np.asarray(Image.fromarray(a).resize((192, 192), Image.BOX))


def process_single(style: str, path: Path) -> dict:
    """A pose without a lip-flap partner: clean it up exactly like the talking frames."""
    a = load_rgb(path)
    if style != "pixel":
        return {"frame": Image.fromarray(a).resize((OUT, OUT), Image.LANCZOS), "info": {}}
    grid = detect_grid(a)
    lg = _pixel_logical(a, grid)
    plan = fit_plan(min(lg.shape[:2]))
    lg = apply_fit(lg, plan)
    q = quantize(lg, build_palette([lg], None))
    return {"frame": upscale(q, plan), "info": {"grid": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in grid.items()},
                                                 "logical_px": int(lg.shape[0]), "scale": plan["k"]}}


def lock_pair(style: str, open_path: Path, closed_path: Path) -> dict:
    """Talking pose: keep the open-mouth pose as the reference and paste ONLY the mouth of the
    closed-mouth edit onto it, so the pair differs in the mouth alone (lip-flap in pose)."""
    ref, ed = load_rgb(open_path), load_rgb(closed_path)
    if ed.shape != ref.shape:
        ed = np.asarray(Image.fromarray(ed).resize(ref.shape[1::-1], Image.LANCZOS))
    if style == "pixel":
        grid = detect_grid(ref)
        lr, le = _pixel_logical(ref, grid), _pixel_logical(ed, grid)
        plan = fit_plan(min(lr.shape[:2]))
        lr, le = apply_fit(lr, plan), apply_fit(le, plan)
        strong = _diff(le, lr) > 40
        min_size = 3
    else:
        g = 256
        d = Image.fromarray(_diff(ed, ref).clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
        strong = np.asarray(d.resize((g, g), Image.BOX)) > 22
        min_size = 8
    area = strong.size
    lab, comps = _components(strong, min_size)
    mouth_core = np.zeros(strong.shape, bool)
    if comps:
        primary = max(comps, key=lambda c: c["size"])
        keep = [primary] + [c for c in comps if c is not primary and _in_box(c, primary["box"], 0.3)]
        mouth_core = np.isin(lab, [c["label"] for c in keep])
    mouth = _grow(mouth_core, 1 if style == "pixel" else 2)
    _, dcomps = _components(strong & ~_grow(mouth, 2), min_size)
    if style == "pixel":
        ys, xs = np.nonzero(mouth)
        face = None
        if len(ys):
            h, w = ys.max() - ys.min(), xs.max() - xs.min()
            face = (max(0, xs.min() - 2 * w), max(0, ys.min() - 2 * h), min(lr.shape[1], xs.max() + 2 * w), min(lr.shape[0], ys.max() + h))
        pal = build_palette([lr, le], face)
        qr, qe = quantize(lr, pal), quantize(le, pal)
        closed = qr.copy()
        closed[mouth] = qe[mouth]
        changed = _diff(qe, qr) > 24
        outside = ~_grow(mouth, 1)
        frames = {"open": upscale(qr, plan), "closed": upscale(closed, plan)}
        mask_img = upscale(mouth.astype(np.uint8) * 255, plan)
    else:
        size = ref.shape[0]
        big = Image.fromarray(mouth.astype(np.uint8) * 255).resize((size, size), Image.NEAREST)
        alpha = np.asarray(big.filter(ImageFilter.GaussianBlur(size / 200))).astype(np.float32)[..., None] / 255.0
        closed = (ref.astype(np.float32) * (1 - alpha) + ed.astype(np.float32) * alpha).round().astype(np.uint8)
        down = lambda x: Image.fromarray(x).resize((OUT, OUT), Image.LANCZOS)  # noqa: E731
        frames = {"open": down(ref), "closed": down(closed)}
        r768, e768 = np.asarray(frames["open"]).astype(np.int16), np.asarray(down(ed)).astype(np.int16)
        changed = np.abs(e768 - r768).max(axis=2) > 24
        outside = ~np.asarray(Image.fromarray(_grow(mouth, 2).astype(np.uint8) * 255).resize((OUT, OUT), Image.NEAREST)).astype(bool)
        mask_img = Image.fromarray(mouth.astype(np.uint8) * 255).resize((OUT, OUT), Image.NEAREST)
    m = {
        "mask_pct": round(mouth.sum() / area * 100, 3),
        "strong_pct": round(mouth_core.sum() / area * 100, 3),
        "outside_pct": round((changed & outside).sum() / max(1, outside.sum()) * 100, 3),
        "dropped_pct": round(sum(c["size"] for c in dcomps) / area * 100, 3),
    }
    return {"frames": frames, "metrics": judge("mid", style, m), "mask": mask_img}
