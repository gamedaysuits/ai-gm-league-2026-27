"""Comic timing in the audio: a tight line is cut from its take at sentence boundaries, with beats.

  micro-beat  150-250 ms before a PUNCH (unless the model wrote its own [pause] there)
  laugh beat  300-700 ms after a PUNCH / BUTTON, sized to the spice (inside the line; a line that ENDS on its punch
              leaves the beat to the mix, which also fires comebacks fast into it)

Takes: a cached take of the model's WHOLE line is cut by its character alignment (0 new characters); free dev voices
synthesize each kept sentence on its own (exact boundaries); an uncached paid line synthesizes only the kept sentences.
Words are never touched: only whole sentences, only at their boundaries. Beat marks (the kicker's end, the beat, the
spice) travel with the line to the mix (room reactions, bed dips, gaps) and the camera (the reaction cut 80 ms after
the kicker).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np

from . import audio
from . import tags as T

MICRO = (0.15, 0.25)
LAUGH = {1: (0.30, 0.40), 2: (0.40, 0.55), 3: (0.55, 0.70)}
NATURAL = (0.16, 0.24)  # between two kept sentences that were not adjacent in the take
PAUSE_CAP = {"PUNCH": 0.70, "other": 0.50}  # a written [pause] or the take's own pause: a beat, never dead air
VOICED_DB = 30.0  # a sentence's voice ends where it drops this far under its loudest 10 ms


def _rng(key: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def laugh_beat(spice: int, key: str) -> float:
    lo, hi = LAUGH.get(max(1, min(3, int(spice or 1))), LAUGH[1])
    return round(float(_rng("laugh:" + key).uniform(lo, hi)), 3)


def micro_beat(key: str) -> float:
    return round(float(_rng("micro:" + key).uniform(*MICRO)), 3)


def voiced_end(pcm: np.ndarray, sr: int, t_from: float = 0.0) -> float | None:
    """Seconds from the segment start where the voice last sounds (at or after t_from): the end of the last 10 ms
    frame within VOICED_DB of the segment's loudest one. The take's last character often runs long ('Bold.' was
    aligned 0.6 s past the word), so kicker ends -- and the reaction cut -- are timed on the audio itself."""
    hop = max(1, int(0.01 * sr))
    n = len(pcm) // hop
    if n < 2:
        return None
    x = pcm[:n * hop].astype(np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    db = 20 * np.log10(np.sqrt(np.mean(x.reshape(n, hop) ** 2, axis=1)) + 1e-9)
    thr = float(db.max()) - VOICED_DB
    k0 = max(0, min(n - 1, int(t_from / 0.01)))
    idx = np.nonzero(db[k0:] > thr)[0]
    if not len(idx):
        return None
    return (k0 + int(idx[-1]) + 1) * hop / sr


def lead_tags(chars: list[str], i: int, j: int) -> tuple[int, list[str]]:
    """(index of the first spoken character in chars[i:j] after any leading [tags], the tags)."""
    k, depth, tags, cur = i, 0, [], ""
    while k < j:
        ch = chars[k]
        if ch == "[":
            depth, cur = depth + 1, ""
        elif ch == "]":
            depth = max(0, depth - 1)
            tags.append(cur.strip().lower())
        elif depth > 0:
            cur += ch
        elif not ch.isspace():
            break
        k += 1
    return k, tags


def locate(speech: str, parts_speech: list[str]) -> list[tuple[int, int]] | None:
    """Char spans of each part inside the take's speech text (sequential search, whitespace-tolerant)."""
    spans, pos = [], 0
    for ps in parts_speech:
        ps = ps.strip()
        if not ps:
            spans.append((pos, pos))
            continue
        i = speech.find(ps, pos)
        if i < 0:
            pat = re.escape(ps).replace(r"\ ", r"\s+")
            m = re.compile(pat).search(speech, pos)
            if not m:
                return None
            i, j = m.start(), m.end()
        else:
            j = i + len(ps)
        spans.append((i, j))
        pos = j
    return spans


def span_times(al: dict, i: int, j: int) -> tuple[float, float] | None:
    """(start of the first non-space char, end of the last non-space char) of chars [i, j)."""
    chars, starts, ends = al["chars"], al["starts"], al["ends"]
    idx = [k for k in range(i, min(j, len(chars))) if not chars[k].isspace()]
    if not idx:
        return None
    return float(starts[idx[0]]), float(ends[idx[-1]])


def part_durations(speech: str, al: dict, parts_speech: list[str]) -> list[float] | None:
    """Spoken length of each part in a take: from its first spoken character (after any leading [tag]: a [pause]
    becomes a capped beat, a cold open drops its [chuckles]) to the end of its last letter (the take's final
    punctuation often runs long)."""
    spans = locate(speech, parts_speech)
    if not spans:
        return None
    out = []
    chars = al["chars"]
    for i, j in spans:
        k1, _ = lead_tags(chars, i, j)
        letters = [q for q in range(k1, min(j, len(chars))) if chars[q].isalnum()]
        if not letters:
            out.append(0.0)
            continue
        out.append(round(max(0.0, float(al["ends"][letters[-1]]) - float(al["starts"][k1])), 3))
    return out


def assemble(segments: list[dict], sr: int, key: str, tempo: float = 1.0
             ) -> tuple[np.ndarray, dict, list[dict], float | None]:
    """segments (in order): {pcm, chars, starts, ends (seconds from the segment's own start), role, label, gap_before
    (the take's own pause when adjacent / the model's written [pause], else None), pause_before (the model wrote
    [pause] there)}.
    -> (audio, alignment, marks, end_beat): marks per PUNCH/BUTTON {role, kicker_end, punch_start, beat, spice, ...};
    end_beat = the laugh beat the mix leaves when the line ENDS on a punch (on-screen seconds).
    Designed beats are ON-SCREEN sizes: a line the short speeds up (atempo) gets them x tempo here, so they land at
    their size after the speed-up (a 300 ms beat stays 300 ms, not 270)."""
    tempo = float(tempo or 1.0)
    out: list[np.ndarray] = []
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    marks: list[dict] = []
    t = 0.0
    end_beat = None
    prev_role = None
    prev_spice = 1
    unit_start = None
    for k, sg in enumerate(segments):
        n_words = len(T.strip("".join(sg["chars"])).split())
        # a tag right on a punch -- "Bold." or a quick topper "Still stuck on four." -- rides the same laugh: one beat
        # after it, never two laughs 1.5 s apart
        is_tag = prev_role == "PUNCH" and sg["role"] in ("BUTTON", "PUNCH") and (
            bool((sg.get("label") or {}).get("tag")) or n_words <= 5)
        if k > 0:
            gap = sg["gap_before"] if sg["gap_before"] is not None else \
                float(_rng(f"{key}:{k}").uniform(*NATURAL)) * tempo
            if sg.get("pause_before") or sg["role"] == "PUNCH":
                # the model's own [pause] IS the punch beat (never doubled); a punch without one gets a micro-beat
                gap = max(gap, micro_beat(f"{key}:{k}") * tempo)
            gap = min(gap, (PAUSE_CAP["PUNCH"] if sg["role"] == "PUNCH" else PAUSE_CAP["other"]) * tempo)
            if is_tag:  # punch + its tag ("...back in August. Bold.") is one unit: a breath, then ONE laugh beat
                gap = min(gap, micro_beat(f"{key}:{k}") * tempo)
                if marks and marks[-1]["role"] == "PUNCH":
                    unit_start = marks.pop()["punch_start"]
            elif prev_role in ("PUNCH", "BUTTON"):
                gap = max(gap, laugh_beat(prev_spice, f"{key}:{k}") * tempo)
                if marks:
                    marks[-1]["beat"] = round(gap, 3)
            n = int(round(gap * sr))
            out.append(np.zeros(n, dtype=np.float32))
            chars.append(" ")
            starts.append(round(t, 4))
            t += gap
            ends.append(round(t, 4))
        pcm = sg["pcm"]
        base = t
        c0 = len(chars)
        chars.extend(sg["chars"])
        starts.extend(round(base + float(x), 4) for x in sg["starts"])
        ends.extend(round(base + float(x), 4) for x in sg["ends"])
        out.append(pcm.astype(np.float32))
        t += len(pcm) / sr
        # the take's last characters run long: nothing in this sentence ends after its voice does
        ve = voiced_end(pcm, sr)
        letters = [q for q in range(c0, len(chars)) if chars[q].isalnum()]
        if ve is not None and letters and base + ve < ends[letters[-1]] - 0.35:
            ve = None  # a last word whispered under the threshold: trust the alignment
        v_abs = round(base + ve, 4) if ve is not None else round(t, 4)
        for q in range(c0, len(chars)):
            starts[q] = round(min(max(starts[q], base), v_abs), 4)
            ends[q] = round(min(max(ends[q], starts[q]), v_abs), 4)
        role = sg["role"]
        if role in ("PUNCH", "BUTTON"):
            lab = sg["label"]
            wc = [i for i in range(c0, len(chars)) if not chars[i].isspace()]
            # the kicker: the sentence's last n words (spoken chars only; tags excluded)
            spoken = "".join(chars[c0:])
            word_ends = [m.end() - 1 + c0 for m in re.finditer(r"\S+", T.strip(spoken))] if False else None  # noqa
            txt = "".join(chars[c0:])
            clean_idx = []
            depth = 0
            for i, ch in enumerate(txt):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth = max(0, depth - 1)
                    continue
                if depth == 0:
                    clean_idx.append(c0 + i)
            words: list[list[int]] = []
            cur: list[int] = []
            for gi in clean_idx:
                if chars[gi].isspace():
                    if cur:
                        words.append(cur)
                        cur = []
                else:
                    cur.append(gi)
            if cur:
                words.append(cur)
            n = max(1, min(4, int(lab.get("kicker") or 3), len(words))) if words else 0
            if words and wc:
                kf, kl = words[-n][0], words[-1][-1]
                # the kicker is the sentence's last words: it ends where the voice does
                ke = max(v_abs, starts[kf] + 0.08) if ve is not None else ends[kl]
                marks.append({"role": "PUNCH" if is_tag else role, "spice": int(lab.get("spice") or 1),
                              "punch_start": unit_start if (is_tag and unit_start is not None) else starts[words[0][0]],
                              "kicker_start": starts[kf], "kicker_end": round(ke, 4), "beat": None,
                              "kicker_words": n, "tag": is_tag})
                unit_start = None
        prev_role = role
        prev_spice = int((sg.get("label") or {}).get("spice") or 1)
    if prev_role in ("PUNCH", "BUTTON"):
        end_beat = laugh_beat(prev_spice, f"{key}:end")
        if marks:
            marks[-1]["beat"] = end_beat
            marks[-1]["at_end"] = True
    y = np.concatenate(out) if out else np.zeros(0, np.float32)
    return y, {"chars": chars, "starts": starts, "ends": ends}, marks, end_beat


def cut_from_take(pcm: np.ndarray, sr: int, speech: str, al: dict, parts_speech: list[str], keep: list[int],
                  labels: list[dict], parts_text: list[str], trim_lead: bool = False) -> list[dict] | None:
    """Segments for assemble() from one take: each kept sentence's audio (+30 ms before its first char, +70 ms after
    its last, never into a neighbour), its chars re-based to the segment start, and the take's natural gap when the
    previous kept sentence was its neighbour. A leading [pause] the model wrote becomes the gap before its sentence
    (assemble caps it: a beat, never dead air). trim_lead (the cold open): a leading tag ([chuckles]) on the first
    kept sentence is dropped, so the hook starts on its words; the words are untouched."""
    spans = locate(speech, parts_speech)
    if not spans:
        return None
    times = [span_times(al, i, j) for i, j in spans]
    segs = []
    for k, idx in enumerate(keep):
        tt = times[idx]
        if tt is None:
            continue
        i, j = spans[idx]
        lead_pause = None
        k1, tags = lead_tags(al["chars"], i, j)
        if tags and k1 < j and (("pause" in tags and k > 0) or (trim_lead and k == 0)):
            spoken0 = float(al["starts"][k1])
            lead_pause = max(0.0, spoken0 - tt[0])
            tt, i = (spoken0, tt[1]), k1
        lo = tt[0] - 0.03
        hi = tt[1] + 0.07
        if idx > 0 and times[idx - 1]:
            lo = max(lo, (times[idx - 1][1] + tt[0]) / 2)
        if idx + 1 < len(times) and times[idx + 1]:
            hi = min(hi, (tt[1] + times[idx + 1][0]) / 2)
        a, b = max(0, int(lo * sr)), min(len(pcm), int(hi * sr))
        seg = pcm[a:b].copy()
        f = min(len(seg) // 4, int(0.008 * sr))
        if f > 1:
            seg[:f] *= np.linspace(0, 1, f, dtype=np.float32)
            seg[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
        off = a / sr
        adjacent = k > 0 and keep[k - 1] == idx - 1 and times[idx - 1] is not None
        gap_before = None
        if adjacent:
            gap_before = max(0.0, tt[0] - times[idx - 1][1] - 0.10)  # the take's own pause (minus the 30/70 ms pads)
        elif lead_pause is not None and k > 0:
            gap_before = lead_pause  # the model's own [pause]
        pause_before = bool(re.search(r"\[pause\]\s*$", parts_text[idx - 1] if idx > 0 else "")) or \
            parts_text[idx].lstrip().startswith("[pause]")
        segs.append({"pcm": seg, "chars": list(al["chars"][i:j]),
                     "starts": [float(x) - off for x in al["starts"][i:j]],
                     "ends": [float(x) - off for x in al["ends"][i:j]],
                     "role": labels[idx]["role"], "label": labels[idx], "gap_before": gap_before,
                     "pause_before": pause_before})
    return segs


def linear_alignment(text: str, dur: float, lead: float = 0.0) -> dict:
    """Dev voices carry no timestamps: spread the characters across the clip (layout only)."""
    chars = list(text)
    n = max(1, len(chars))
    step = max(0.0, dur - lead) / n
    starts = [round(lead + i * step, 4) for i in range(n)]
    return {"chars": chars, "starts": starts, "ends": [round(s + step, 4) for s in starts]}


def derived_key(parts: dict) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def read_pcm(path: Path, sr: int) -> np.ndarray:
    return audio.decode(path, sr=sr)
