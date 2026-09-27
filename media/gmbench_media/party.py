"""Draft-party format helpers: the two-beat on-air call, its tone, and pick chains.

Each DRAFT_PICK.on_air_call is the GM's own words in two beats:
  beat 1  a reaction to the PREVIOUS pick, spoken to that GM (rib, question or respect; the bot gets chirped)
  beat 2  their own pick: the player's full name once, plus a quick why
The split is found on sentence boundaries: beat 2 starts at the sentence that names the player (or, when the name is
missing, right after the last sentence that addresses the previous GM). Nothing here changes a word: it only measures the text.
"""

from __future__ import annotations

import difflib
import re

from . import tags as T

SENT_SPLIT = re.compile(r"(?<=[.!?…])[\"”’)\]]*\s+")
WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]+")

PRAISE = {"nice", "great", "love", "loved", "respect", "solid", "smart", "steal", "good", "gorgeous", "beauty", "beaut",
          "props", "genius", "legend", "stud", "classy", "clean", "sharp", "hats", "hat", "tip", "tipping", "well",
          "congrats", "congratulations", "sweet", "fair", "unreal", "killer", "strong", "heck", "atta", "attaboy"}
ROAST = {"reach", "really", "seriously", "bust", "brutal", "yikes", "terrible", "wasted", "washed", "overpaid", "sloppy",
         "cute", "adorable", "bold", "interesting", "huh", "c'mon", "come", "robot", "bot", "rust", "rusty", "garbage",
         "trash", "pylon", "pylons", "bum", "joke", "clown", "sucker", "whiff", "whiffed", "panic", "panicked", "desperate",
         "overreach", "cooked", "toast", "dud", "dusters", "plugger", "pluggers", "weak", "soft"}
ROAST_TAGS = {"laughs", "sarcastic", "mischievously", "chuckles"}
PRAISE_TAGS = {"excited", "confident"}


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def words(text: str) -> list[str]:
    return WORD.findall(T.strip(text or ""))


def mention_keys(team) -> list[str]:
    """How a buddy would address this GM: first/last name, quoted nickname, franchise nickname, town, abbrev.
    The control bot answers to 'robot' / 'bot' / 'Autodraft'."""
    keys: list[str] = []
    if team is None:
        return keys
    if team.is_bot:
        keys += ["robot", "bot", "autodraft", "machine", "computer", "algorithm", "spreadsheet"]
    name = team.gm_name or ""
    for nick in re.findall(r"[\"'“‘]([^\"'“”‘’]+)[\"'”’]", name):
        keys.append(nick)
        if nick.lower().startswith("the "):
            keys.append(nick[4:])  # "The Matrix" -> "Matrix"
    plain = re.sub(r"[\"'“‘][^\"'“”‘’]*[\"'”’]", " ", name).split()
    keys += [w for w in plain if len(w) >= 3 and w.lower() not in ("the", "and")]
    for k in (team.nickname if team.has_persona else None, team.city, team.p("franchise_abbrev"),
              None if team.has_persona else team.display):
        if k:
            keys.append(str(k))
    out = []
    for k in keys:
        k = k.strip().lower()
        if k and k not in out:
            out.append(k)
    return out


def mentions(sentence: str, keys: list[str]) -> bool:
    s = " " + re.sub(r"\s+", " ", T.strip(sentence).lower()) + " "
    for k in keys:
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", s):
            return True
    return False


def name_hit(sentence: str, player_name: str) -> bool:
    """Does this sentence say the player (full name or surname; fuzzy for spelling variants)?"""
    target = [_norm(w) for w in player_name.split() if _norm(w)]
    if not target:
        return False
    ws = [_norm(w) for w in words(sentence)]
    sur = target[-1]
    for i in range(len(ws)):
        w = ws[i].removesuffix("'s")
        if w == sur or (len(sur) >= 5 and difflib.SequenceMatcher(None, sur, w).ratio() >= 0.8):
            return True
        if len(target) >= 2 and i + 1 < len(ws):
            joined = ws[i] + ws[i + 1]
            if difflib.SequenceMatcher(None, "".join(target[-2:]), joined).ratio() >= 0.85:
                return True
    return False


def tone(beat1: str) -> str:
    """'praise' | 'roast' for the reaction beat (keep it simple: tags + a small lexicon; ties go to the chirp)."""
    tg = set(T.tags_in(beat1))
    ws = {_norm(w) for w in words(beat1)}
    p = len(ws & PRAISE) + len(tg & PRAISE_TAGS) * 0.5
    r = len(ws & ROAST) + len(tg & ROAST_TAGS) + (0.5 if "?" in T.strip(beat1) else 0)
    return "praise" if p > r else "roast"


def split_call(display_text: str, player_name: str, prev_team=None) -> dict:
    """-> {beat2_word: index of the first display word of beat 2 (0 = no reaction beat), named: bool,
    tone: 'praise'|'roast'|None, addressed: bool}. Works on the display text (tags already stripped)."""
    sents = [s for s in SENT_SPLIT.split(display_text.strip()) if s.strip()]
    name_idx = next((i for i, s in enumerate(sents) if name_hit(s, player_name)), None)
    keys = mention_keys(prev_team)
    limit = name_idx if name_idx is not None else len(sents)
    ment = [i for i in range(limit) if keys and mentions(sents[i], keys)]
    # beat 2 starts at the sentence that names the pick (a short "alright, my turn" before it stays with the reaction:
    # better than cutting the roast off mid-chirp); no name found -> right after the last line to the previous GM
    split = name_idx if name_idx is not None else ((ment[-1] + 1) if ment else 0)
    beat1 = " ".join(sents[:split])
    n_words = sum(len(s.split()) for s in sents[:split])
    return {"beat2_word": n_words, "named": name_idx is not None, "addressed": bool(ment),
            "tone": tone(beat1) if beat1 else None, "beat1": beat1}


# --------------------------------------------------------------------------- chains

def call_score(p, prev, text: str) -> float:
    """How well a call plays off the previous pick: a real reaction beat that addresses the previous GM, delivery
    energy, and a named pick."""
    if not text:
        return 0.0
    info = split_call(T.strip(text), p.player_name, prev)
    sc = T.energy(text) + (1.5 if info["addressed"] else 0.0) + (0.8 if info["beat2_word"] >= 4 else 0.0)
    sc += 0.6 if info["named"] else -1.0
    sc += 0.4 * len(set(T.tags_in(text)) & (ROAST_TAGS | {"shouting"}))
    return sc


def best_chains(picks: list, teams: dict, words_for, n: int, length: int, bonus=None) -> list[list]:
    """Non-overlapping runs of `length` consecutive picks where each GM plays off the last (highest total score).
    words_for(pick) -> the on-air words (or '' for bots / auto picks, which break a chain). bonus(window) -> extra
    score (shorts: comebacks, cross-lab matchups, callbacks, punch density)."""
    picks = sorted(picks, key=lambda p: p.pick_no)
    by_no = {p.pick_no: p for p in picks}
    scored = []
    for i in range(0, max(0, len(picks) - length + 1)):
        win = picks[i:i + length]
        if any(b.pick_no != a.pick_no + 1 for a, b in zip(win, win[1:])):
            continue
        total = 0.0
        ok = True
        for p in win:
            txt = words_for(p)
            if not txt:
                ok = False
                break
            prev = by_no.get(p.pick_no - 1)
            total += call_score(p, teams.get(prev.team) if prev else None, txt)
        if ok:
            if bonus is not None:
                total += float(bonus(win))
            scored.append((round(total, 3), win[0].pick_no, win))
    chosen: list[list] = []
    used: set[int] = set()
    for sc, _, win in sorted(scored, key=lambda x: (-x[0], x[1])):
        if len(chosen) >= n:
            break
        if any(p.pick_no in used for p in win):
            continue
        chosen.append(win)
        used |= {p.pick_no for p in win}
    return sorted(chosen, key=lambda w: w[0].pick_no)
