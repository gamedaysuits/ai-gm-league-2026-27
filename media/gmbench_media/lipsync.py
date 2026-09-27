"""Lip sync from the TEXT: character-level TTS alignment -> viseme timeline at the show frame rate.

No amplitude flapping. Every mouth shape comes from the letters being spoken at that moment:

    rest        silence, punctuation, pauses
    closed      M B P (the lips must meet)
    teeth       F V (and 'ph')
    round       O U W (and oo, ou, ow, wh, qu)
    wide_open   A (stressed / long), shouted open vowels
    medium_open A (short), E I (long), 'ai', 'ay', 'au', 'aw'
    small_open  E I Y (short) and every other consonant (t d n l s z k g r h c x j, th, sh, ch)
    laugh       inside a [laughs] / [chuckles] tag span (the laugh audio itself)
    smirk       optional: a closed-mouth smirk after a sarcastic tag (renderer may map to rest)

Coarticulation (animator rules): 1-frame anticipation, minimum hold of 2 frames, A-B-A flicker merged, M/B/P always
closes the mouth for at least one frame, short word gaps do not snap to rest.
Delivery tags ([laughs], [sighs] ...) are never 'spoken': their characters are removed from the alignment, but their
time span is kept as a non-verbal event (a laugh mouth, a sigh).
"""

from __future__ import annotations

import re

from . import tags as T

VISEMES = ("rest", "closed", "small_open", "medium_open", "wide_open", "round", "teeth", "laugh", "smirk")
OPEN_RANK = {"rest": 0, "closed": 0, "smirk": 0, "teeth": 1, "small_open": 1, "round": 2, "medium_open": 2,
             "wide_open": 3, "laugh": 3}
DIGRAPHS = [("oo", "round"), ("ou", "round"), ("ow", "round"), ("wh", "round"), ("qu", "round"), ("ph", "teeth"),
            ("th", "small_open"), ("sh", "small_open"), ("ch", "small_open"), ("ee", "medium_open"),
            ("ea", "medium_open"), ("ai", "medium_open"), ("ay", "medium_open"), ("au", "medium_open"),
            ("aw", "medium_open"), ("oa", "round"), ("oi", "round"), ("oy", "round")]
LAUGH_TAGS = {"laughs", "chuckles"}


def letter_viseme(ch: str, dur: float, loud: bool) -> str:
    c = ch.lower()
    if c in "mbp":
        return "closed"
    if c in "fv":
        return "teeth"
    if c in "ouwq":
        return "round"
    if c == "a":  # the jaw drops on 'a': wide once it is held a couple of frames
        return "wide_open" if (dur >= 0.07 or loud) else "medium_open"
    if c in "ei":  # teeth-showing open on e / i; only a clipped one stays a sliver
        return "medium_open" if (dur >= 0.06 or loud) else "small_open"
    if c == "y":
        return "small_open"
    if c.isdigit():
        return "medium_open"
    if c.isalpha():
        return "small_open"
    return "rest"


def strip_tags(al: dict) -> tuple[list[str], list[float], list[float], list[tuple[str, float, float]]]:
    """-> (chars, starts, ends) without tag characters, plus [(tag, t0, t1)] non-verbal spans."""
    chars, starts, ends = list(al.get("chars") or []), list(al.get("starts") or []), list(al.get("ends") or [])
    s = "".join(chars)
    keep = [True] * len(chars)
    spans: list[tuple[str, float, float]] = []
    for m in T.TAG_RE.finditer(s):
        i, j = m.start(), m.end()
        for k in range(i, min(j, len(keep))):
            keep[k] = False
        if j < len(keep) and chars[j] == " ":
            keep[j] = False
        if i < len(starts):
            spans.append((m.group(1).strip().lower(), float(starts[i]), float(ends[min(j, len(ends)) - 1])))
    return ([c for c, k in zip(chars, keep) if k], [t for t, k in zip(starts, keep) if k],
            [t for t, k in zip(ends, keep) if k], spans)


def raw_segments(al: dict, loud: bool = False) -> tuple[list[tuple[float, float, str]], list[tuple[str, float, float]]]:
    """Viseme segments (t0, t1, viseme) in line time from the character alignment."""
    chars, starts, ends, spans = strip_tags(al)
    segs: list[tuple[float, float, str]] = []
    i, n = 0, len(chars)
    while i < n:
        ch = chars[i]
        t0, t1 = float(starts[i]), float(ends[i])
        pair = (ch + chars[i + 1]).lower() if i + 1 < n else ""
        vis = None
        for dg, v in DIGRAPHS:
            if pair == dg:
                vis, t1, i = v, float(ends[i + 1]), i + 1
                break
        if vis is None:
            up = ch.isupper() and i + 1 < n and chars[i + 1].isupper()
            vis = letter_viseme(ch, t1 - t0, loud or up)
        if not ch.strip() or not (ch.isalnum() or ch in "'’-"):
            vis = "rest"
        segs.append((t0, max(t1, t0 + 0.01), vis))
        i += 1
    for tag, a, b in spans:  # non-verbal: the laugh mouth during the laugh audio
        if tag in LAUGH_TAGS and b - a >= 0.12:
            segs.append((a, b, "laugh"))
    segs.sort(key=lambda x: (x[0], x[1]))
    return segs, spans


def frames_for(segs: list[tuple[float, float, str]], start: float, end: float, fps: int,
               gap_rest_s: float = 0.14) -> list[str]:
    """Sample segments to frames: per frame the viseme with the most coverage, with MBP / laugh priority."""
    f0, f1 = int(round(start * fps)), int(round(end * fps))
    out: list[str] = []
    for f in range(f0, max(f0 + 1, f1)):
        a, b = f / fps, (f + 1) / fps
        cover: dict[str, float] = {}
        for t0, t1, v in segs:
            t0g, t1g = start + t0, start + t1
            ov = min(b, t1g) - max(a, t0g)
            if ov > 0:
                cover[v] = cover.get(v, 0.0) + ov
        if not cover:
            out.append("rest")
            continue
        vowel = max(((v, c) for v, c in cover.items() if v in ("wide_open", "medium_open", "round")),
                    key=lambda kv: (kv[1], OPEN_RANK.get(kv[0], 0)), default=None)
        if cover.get("laugh", 0) > 0.3 / fps:
            out.append("laugh")
        elif cover.get("closed", 0) > 0.25 / fps:  # the lips must meet for M/B/P, even briefly
            out.append("closed")
        elif vowel and vowel[1] >= 0.2 / fps:  # every syllable's vowel must read on screen (it carries the beat)
            out.append(vowel[0])
        else:
            out.append(max(cover.items(), key=lambda kv: (kv[1], OPEN_RANK.get(kv[0], 0)))[0])
    # short silences inside speech stay a soft small-open, not a snap to rest
    k = 0
    while k < len(out):
        if out[k] == "rest":
            j = k
            while j < len(out) and out[j] == "rest":
                j += 1
            if 0 < k and j < len(out) and (j - k) / fps < gap_rest_s:
                for m in range(k, j):
                    out[m] = "small_open" if out[k - 1] not in ("closed",) else "closed"
            k = j
        else:
            k += 1
    return out


def coarticulate(seq: list[str], min_hold: int = 2, lead: int = 1) -> list[str]:
    """Anticipate by `lead` frames, merge A-B-A flicker, enforce a minimum hold (MBP closures survive)."""
    if not seq:
        return seq
    s = seq[lead:] + [seq[-1]] * lead if lead else list(seq)
    # A-B-A with a single-frame B -> A (keep a closure: that one is the M/B/P)
    for i in range(1, len(s) - 1):
        if s[i - 1] == s[i + 1] != s[i] and s[i] not in ("closed", "laugh"):
            s[i] = s[i - 1]
    # minimum hold: runs shorter than min_hold are absorbed into the previous run (closures extended instead)
    runs: list[list] = []
    for v in s:
        if runs and runs[-1][0] == v:
            runs[-1][1] += 1
        else:
            runs.append([v, 1])
    i = 0
    while i < len(runs):
        v, n = runs[i]
        if n < min_hold and len(runs) > 1:
            if v in ("closed", "laugh"):  # important shapes steal a frame from the next run
                if i + 1 < len(runs) and runs[i + 1][1] > min_hold:
                    runs[i + 1][1] -= (min_hold - n)
                    runs[i][1] = min_hold
                i += 1
                continue
            if i > 0:
                runs[i - 1][1] += n
                runs.pop(i)
                if i < len(runs) and runs[i - 1][0] == runs[i][0]:
                    runs[i - 1][1] += runs[i][1]
                    runs.pop(i)
                continue
            if i + 1 < len(runs):
                runs[i + 1][1] += n
                runs.pop(i)
                continue
        i += 1
    out: list[str] = []
    for v, n in runs:
        out += [v] * n
    return out[: len(s)] + [out[-1]] * max(0, len(s) - len(out))


def line_visemes(line: dict, fps: int, loud: bool | None = None) -> tuple[int, list[str], list[tuple[str, float, float]]]:
    """-> (first global frame, per-frame visemes, non-verbal tag spans in global time) for one placed line."""
    al = line.get("alignment") or {}
    start, end = float(line["start"]), float(line["end"])
    if loud is None:
        loud = T.has(line.get("tts_text") or "", "shouting")
    if not al.get("chars"):
        return int(round(start * fps)), [], []
    segs, spans = raw_segments(al, loud)
    seq = coarticulate(frames_for(segs, start, end, fps))
    if seq:
        seq[-1] = "rest"
    return int(round(start * fps)), seq, [(t, start + a, start + b) for t, a, b in spans]


def speaker_tracks(lines: list[dict], fps: int, total_frames: int) -> dict[str, list[list]]:
    """RLE viseme track per speaker over the whole show: [[frame, viseme], ...] (rest between lines)."""
    per: dict[str, list[str]] = {}
    for ln in lines:
        f0, seq, _ = line_visemes(ln, fps)
        if not seq:
            continue
        arr = per.setdefault(ln["speaker"], ["rest"] * total_frames)
        for k, v in enumerate(seq):
            if 0 <= f0 + k < total_frames:
                arr[f0 + k] = v
    out: dict[str, list[list]] = {}
    for sp, arr in per.items():
        rle, prev = [], None
        for f, v in enumerate(arr):
            if v != prev:
                rle.append([f, v])
                prev = v
        out[sp] = rle
    return out


def flipbook(line: dict, fps: int) -> list[tuple[int, str, str]]:
    """(frame, viseme, the word being spoken) rows for eyeballing a line's lip sync."""
    from .captions import word_times
    f0, seq, _ = line_visemes(line, fps)
    ws = word_times(line)
    rows = []
    for k, v in enumerate(seq):
        t = (f0 + k) / fps
        w = next((x["w"] for x in ws if x["start"] <= t < x["end"]), "")
        rows.append((f0 + k, v, w))
    return rows
