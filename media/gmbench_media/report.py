"""Report Card helpers: which grader comments go on air (spice ranking), letters, the Delusion Index.

Data comes from League.report_card() (exports/grades.json from `gmbench export`, every comment cross-checked
against the grader's REPORT_SUBMITTED event in the ledger). Comments are the graders' own words and are only
ever trimmed at a sentence boundary (logged), like every other GM line.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from . import tags as T
from .ledger import GRADE_POINTS
from .nhl import number_words

# delivery tags that make a roast land on air, by weight
SPICE_TAGS = {"laughs": 3, "sarcastic": 3, "shouting": 3, "mischievously": 2, "chuckles": 2, "angry": 2,
              "gasps": 1, "surprised": 1, "whispers": 1, "excited": 1}
NUM_WORDS = set("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand "
                "million".split())


def count_numbers(text: str) -> int:
    """Numeric mentions (digits, or a run of spelled-out number words like 'eighty-three' = one)."""
    words = T.strip(text or "")
    n = len(re.findall(r"\d+(?:[.,]\d+)?", words))
    run = False
    for tok in re.findall(r"[a-z]+|\d+", words.lower()):
        if tok in NUM_WORDS:
            if not run:
                n += 1
            run = True
        else:
            run = False
    return n


def tag_score(text: str) -> int:
    return sum(SPICE_TAGS.get(t, 0) for t in set(T.tags_in(text or "")))


def points(grade: str) -> float:
    return GRADE_POINTS.get(str(grade).strip().upper(), 2.0)


REPEAT_PENALTY = 0.45  # grade points per earlier appearance of the same grader (one harsh grader can't take over)


def spice_key(g: dict, uses: Counter) -> tuple:
    """Lower sorts first: low grade, then roast tags, then graders we have heard least, then shorter. Grader
    diversity is folded into the grade term so the league's harshest grader cannot air every roast."""
    h = int(hashlib.sha1(f"{g.get('from')}:{g.get('seq')}:{g.get('comment')}".encode()).hexdigest()[:6], 16)
    return (round(points(g["grade"]) + REPEAT_PENALTY * uses[g["from"]], 2), -tag_score(g["comment"]), uses[g["from"]],
            len(T.strip(g["comment"])) // 40, h)


def airable(g: dict, voiced: set[str] | None = None, max_numbers: int = 1) -> bool:
    if not T.strip(g.get("comment") or ""):
        return False
    if count_numbers(g["comment"]) > max_numbers:
        return False  # "a UTA two and a Philly two": numbers are hard to follow on air
    return voiced is None or g["from"] in voiced


def choose_comments(teams: list[dict], counts: dict[str, int], voiced: set[str] | None = None) -> dict[str, list[dict]]:
    """teams sorted bottom -> top; counts: team -> how many comments to air. The top of the class ends on a high:
    its spiciest roast, then its loudest praise."""
    uses: Counter = Counter()
    out: dict[str, list[dict]] = {}
    top = teams[-1]["team"] if teams else None
    for t in teams:
        n = counts.get(t["team"], 1)
        pool = sorted((g for g in t["grades"] if airable(g, voiced)), key=lambda g: spice_key(g, uses))
        picks: list[dict] = []
        for g in pool:
            if len(picks) >= n:
                break
            if any(p["from"] == g["from"] for p in picks):
                continue
            picks.append(g)
        if t["team"] == top and n >= 2 and pool:
            rest = [g for g in pool if g is not pool[0]]
            praise = max(rest, key=lambda g: (points(g["grade"]), tag_score(g["comment"]), -uses[g["from"]]),
                         default=None)
            picks = [pool[0]] + ([praise] if praise else [])
        for g in picks:
            uses[g["from"]] += 1
            g["praise"] = points(g["grade"]) >= float(t["gpa"]) - 0.05
        out[t["team"]] = picks
    return out


def best_roasts(teams: list[dict], n: int, voiced: set[str] | None = None) -> list[dict]:
    """The n best roast lines of the whole class for the short: spiciest first, every grader and every target
    at most once when possible."""
    pool = [{**g, "team": t["team"], "gpa": t["gpa"]} for t in teams for g in t["grades"] if airable(g, voiced)]
    pool = [g for g in pool if points(g["grade"]) <= 2.7 or tag_score(g["comment"]) >= 3]  # roasts, not compliments
    uses: Counter = Counter()
    chosen: list[dict] = []
    for relax in (False, True):
        for g in sorted(pool, key=lambda g: spice_key(g, uses)):
            if len(chosen) >= n:
                break
            if g in chosen:
                continue
            if not relax and (any(c["from"] == g["from"] for c in chosen) or any(c["team"] == g["team"] for c in chosen)):
                continue
            chosen.append(g)
            uses[g["from"]] += 1
    return chosen[:n]


def letter_spoken(letter: str) -> str:
    """'C-' -> 'C-MINUS' (TTS input + display in host lines)."""
    letter = letter.strip().upper()
    base, mod = letter[:1], letter[1:]
    return base + {"+": "-PLUS", "-": "-MINUS"}.get(mod, "")


def article(letter: str) -> str:
    return "an" if letter.strip().upper()[:1] in ("A", "F") else "a"


def decimal_words(x: float, places: int = 2) -> str:
    """2.85 -> 'two point eight five'; 6.0 -> 'six'; 8.5 -> 'eight point five'."""
    s = f"{x:.{places}f}".rstrip("0").rstrip(".")
    if "." not in s:
        return number_words(int(s))
    a, b = s.split(".")
    digits = " ".join(number_words(int(d)) for d in b)
    return f"{number_words(int(a))} point {digits}"


def decimal_display(x: float, places: int = 2) -> str:
    return f"{x:.{places}f}".rstrip("0").rstrip(".")


def ranked(teams: list[dict]) -> list[dict]:
    """Class ranking, best first (ties broken by team id so the board and the segment always agree)."""
    return sorted(teams, key=lambda t: (-float(t["gpa"]), t["team"]))


def delusion_rows(teams: list[dict]) -> list[dict]:
    """Self-predicted finish vs the league's average prediction; gap > 0 = rosier than the league."""
    rows = []
    for t in teams:
        sp, avg = t.get("self_prediction"), t.get("avg_predicted_finish")
        if sp is None or avg is None:
            continue
        rows.append({"team": t["team"], "self": int(sp), "avg": float(avg), "gap": round(float(avg) - float(sp), 1)})
    rows.sort(key=lambda r: (-r["gap"], r["team"]))
    return rows
