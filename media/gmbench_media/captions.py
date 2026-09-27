"""Caption pages from script text, timed to each line's audio.

Each line's display_text is wrapped (measured with the real Questrial font) into rows that fit the
caption box, grouped into pages of <= max_lines rows. Row timing comes from TTS character
alignment when the engine provides it, otherwise from a punctuation-weighted character share of
the line's duration. The row being spoken is the "active" row (highlighted on screen).
"""

from __future__ import annotations

import re
from functools import lru_cache

from PIL import ImageFont

from .paths import SHOW_ASSETS


@lru_cache(maxsize=64)
def font(name: str, size: int, weight: int = 400, width: int = 100) -> ImageFont.FreeTypeFont:
    files = {"questrial": "questrial-latin-400-normal.woff2", "archivo": "archivo-latin-standard-normal.woff2"}
    f = ImageFont.truetype(str(SHOW_ASSETS / "fonts" / files[name]), size)
    if name == "archivo":
        try:
            f.set_variation_by_axes([weight, width])
        except Exception:  # noqa: BLE001
            pass
    return f


def text_width(text: str, name: str, size: int, weight: int = 400, width: int = 100,
               letter_spacing: float = 0.0) -> float:
    return font(name, size, weight, width).getlength(text) + letter_spacing * size * max(0, len(text) - 1)


def wrap(text: str, max_px: float, name: str, size: int, weight: int = 400, width: int = 100) -> list[str]:
    words = text.split()
    rows: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if cur and text_width(cand, name, size, weight, width) > max_px:
            rows.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        rows.append(cur)
    return rows


def fit_size(text: str, max_px: float, name: str, max_size: int, min_size: int, max_lines: int = 1,
             weight: int = 400, width: int = 100) -> tuple[int, list[str]]:
    """Largest font size (step 2px) at which text wraps into <= max_lines rows within max_px."""
    size = max_size
    while size > min_size:
        rows = wrap(text, max_px, name, size, weight, width)
        if len(rows) <= max_lines and all(text_width(r, name, size, weight, width) <= max_px for r in rows):
            return size, rows
        size -= 2
    return min_size, wrap(text, max_px, name, min_size, weight, width)


def _weight(s: str) -> float:
    w = float(len(s))
    w += 4.0 * len(re.findall(r"[.!?…]", s)) + 2.0 * len(re.findall(r"[,;:—–]", s))
    return max(w, 1.0)


def _aligned_starts(rows: list[str], alignment: dict | None, start: float) -> list[float] | None:
    if not alignment or not alignment.get("chars"):
        return None
    chars = alignment["chars"]
    starts = alignment["starts"]
    aligned = "".join(chars)
    pos, out = 0, []
    for row in rows:
        first = row.split()[0]
        i = aligned.find(first, pos)
        if i < 0:
            return None
        out.append(start + float(starts[i]))
        pos = i + len(first)
    return out


def pages_for_line(line: dict, cfg: dict) -> list[dict]:
    c = cfg["captions"]
    size, max_px, max_lines = int(c["font_px"]), float(c["max_width_px"]), int(c["max_lines"])
    rows = wrap(line["display_text"], max_px, "questrial", size)
    if not rows:
        return []
    start, end = float(line["start"]), float(line["end"])
    dur = max(0.2, end - start)
    starts = _aligned_starts(rows, line.get("alignment"), start)
    if starts is None:
        weights = [_weight(r) for r in rows]
        total = sum(weights)
        acc, starts = 0.0, []
        for w in weights:
            starts.append(start + dur * acc / total)
            acc += w
    ends = starts[1:] + [end]
    row_objs = [{"text": r, "start": round(s, 3), "end": round(e, 3)} for r, s, e in zip(rows, starts, ends)]
    pages = []
    for k in range(0, len(row_objs), max_lines):
        grp = row_objs[k:k + max_lines]
        pages.append({"line": line["id"], "speaker": line["speaker"], "rows": grp,
                      "start": grp[0]["start"], "end": grp[-1]["end"]})
    return pages


def build_pages(lines: list[dict], cfg: dict) -> list[dict]:
    hold = float(cfg["captions"]["hold_s"])
    pages: list[dict] = []
    for ln in lines:
        pages.extend(pages_for_line(ln, cfg))
    for i, p in enumerate(pages):
        nxt = pages[i + 1]["start"] if i + 1 < len(pages) else p["end"] + hold
        p["show"] = round(max(0.0, p["start"] - 0.04), 3)
        p["hide"] = round(min(p["end"] + hold, nxt - 0.02) if nxt > p["end"] else p["end"], 3)
    return pages


# --------------------------------------------------------------------------- meme captions (word pops)

def _core(w: str) -> str:
    return re.sub(r"[^\w']+", "", w.lower())


def word_times(line: dict) -> list[dict]:
    """Every display word with start/end, from the TTS character alignment when present (tags removed; hyphens,
    dashes and decimals ignored when matching, so 'thirty-three', 'boys—another' and '1.3' line up; a display word
    the voice says differently, like '1' read as 'one', is interpolated between its neighbours), otherwise a
    punctuation-weighted share of the line."""
    words = (line.get("display_text") or "").split()
    if not words:
        return []
    start, end = float(line["start"]), float(line["end"])
    al = line.get("alignment") or {}
    if al.get("chars") and al.get("starts"):
        from .lipsync import strip_tags
        chars, starts, ends, _ = strip_tags(al)
        norm = [(ch.lower(), i) for i, ch in enumerate(chars) if re.match(r"[\w']", ch.lower())]
        ns = "".join(c for c, _ in norm)
        pos = 0
        at: list[float | None] = []
        said: list[float | None] = []  # when the voice finishes the word (its last letter), pauses excluded
        for w in words:
            c = _core(w)
            i = ns.find(c, pos) if c else -1
            if i < 0 or i - pos > 24 + 2 * len(c):
                at.append(None)
                said.append(None)
                continue
            at.append(float(starts[norm[i][1]]))
            said.append(float(ends[norm[i + len(c) - 1][1]]) if ends else None)
            pos = i + len(c)
        hits = sum(1 for x in at if x is not None)
        if hits >= max(1, int(0.6 * len(words))):
            known = [(k, x) for k, x in enumerate(at) if x is not None]
            for k in range(len(at)):
                if at[k] is None:
                    lo = max((q for q in known if q[0] < k), default=(-1, 0.0), key=lambda q: q[0])
                    hi = min((q for q in known if q[0] > k), default=(len(at), end - start), key=lambda q: q[0])
                    at[k] = lo[1] + (hi[1] - lo[1]) * (k - lo[0]) / max(1, hi[0] - lo[0])
            out = [{"w": w, "start": round(start + float(t), 3)} for w, t in zip(words, at)]
            for k, o in enumerate(out):
                o["end"] = out[k + 1]["start"] if k + 1 < len(out) else round(end, 3)
                if o["end"] < o["start"]:
                    o["end"] = o["start"]
                sd = said[k]
                o["said"] = round(min(max(start + sd, o["start"]), o["end"]), 3) if sd is not None else o["end"]
            return out
    out = []
    weights = [_weight(w) + 1.0 for w in words]
    total = sum(weights)
    acc = 0.0
    dur = max(0.2, end - start)
    for w, wt in zip(words, weights):
        s0 = start + dur * acc / total
        acc += wt
        e0 = round(start + dur * acc / total, 3)
        out.append({"w": w, "start": round(s0, 3), "end": e0, "said": e0})
    return out


def meme_pages(lines: list[dict], cfg: dict, max_px: float | None = None, size: int | None = None,
               layout_for=None) -> list[dict]:
    """Pages of <= max_lines rows of timed words, wrapped with the real caption font. `layout_for(line)` may return a
    per-line (max_px, font_px) (the studio has a wide board caption and a narrower speaker-column caption)."""
    c = cfg["captions"]
    size0 = int(size or c["font_px"])
    max_px0 = float(max_px or c["max_width_px"])
    rows_per = int(c["max_lines"])
    fam, wt, wd = c.get("font", "archivo"), int(c.get("weight", 800)), int(c.get("width", 100))
    pages: list[dict] = []
    for ln in lines:
        max_px, size = layout_for(ln) if layout_for else (max_px0, size0)
        space = text_width(" ", fam, size, wt, wd)
        words = word_times(ln)
        rows: list[list[dict]] = []
        cur: list[dict] = []
        width = 0.0
        for w in words:
            ww = text_width(w["w"], fam, size, wt, wd)
            if cur and width + space + ww > max_px:
                rows.append(cur)
                cur, width = [], 0.0
            width = ww if not cur else width + space + ww
            cur.append(w)
        if cur:
            rows.append(cur)
        for k in range(0, len(rows), rows_per):
            grp = rows[k:k + rows_per]
            pages.append({"line": ln["id"], "speaker": ln["speaker"], "rows": grp, "size": size,
                          "mode": ln.get("_mode"), "start": grp[0][0]["start"], "end": grp[-1][-1]["end"]})
    hold = float(c["hold_s"])
    for i, p in enumerate(pages):
        nxt = pages[i + 1]["start"] - 0.04 if i + 1 < len(pages) else p["end"] + hold
        p["show"] = round(max(0.0, p["start"] - 0.04), 3)
        p["hide"] = round(max(p["show"] + 0.2, min(p["end"] + hold, nxt)), 3)
    return pages
