"""Inline delivery tags ([shouting], [laughs], ...) shared by script, TTS, captions and motion.

GM lines keep their OWN tags (from the ledger) when they are on the allowlist; unknown tags are
stripped. Tags never reach display text. Host templates only use allowlisted tags.
"""

from __future__ import annotations

import re

ALLOWED = ["excited", "shouting", "laughs", "chuckles", "sarcastic", "mischievously", "gasps", "sighs",
           "whispers", "confident", "surprised", "angry", "pause"]
TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]")
HYPE = {"shouting", "excited", "angry", "surprised", "gasps"}
FUNNY = {"laughs", "chuckles", "sarcastic", "mischievously"}


def _norm(tag: str) -> str:
    return re.sub(r"\s+", " ", tag.strip().lower())


def tags_in(text: str) -> list[str]:
    return [_norm(m.group(1)) for m in TAG_RE.finditer(text or "")]


def strip(text: str) -> str:
    """Words only (what captions show)."""
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", text or "")).strip()


def sanitize(text: str, allowed: list[str] | None = None) -> str:
    """Keep allowlisted tags (normalised to lowercase), drop the rest; words untouched."""
    ok = {_norm(t) for t in (allowed or ALLOWED)}

    def keep(m: re.Match) -> str:
        t = _norm(m.group(1))
        return f" [{t}] " if t in ok else " "

    out = TAG_RE.sub(keep, text or "")
    return re.sub(r"\s+", " ", out).strip()


def energy(text: str) -> float:
    t = tags_in(text)
    words = strip(text)
    caps = sum(1 for w in re.findall(r"[A-Za-z]{3,}", words) if w.isupper())
    return (2.0 * sum(x in HYPE for x in t) + 1.5 * sum(x in FUNNY for x in t) + 0.6 * words.count("!")
            + 0.3 * min(caps, 6))


def has(text: str, *names: str) -> bool:
    t = set(tags_in(text))
    return any(n in t for n in names)
