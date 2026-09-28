"""The tight edit: whole sentences only, verbatim, every cut logged.

A GM's call is split into sentences (tags travel with their sentence; "Crunching the numbers... the Bonecrushers"
is one sentence). Every sentence gets a role:

  punch    the best roast / praise line of the reaction beat (or of a comeback / hook)  -- always kept
  pick     the sentence that names the drafted player                                   -- always kept
  button   the call's last sentence, after the pick                                     -- kept when the budget allows
  hinge    a one- or two-word beat right before the pick ("Fine.", "Not tonight.")      -- cheap, kept when it fits
  setup    a short line right before the punch ("The robot takes Draisaitl.")          -- low value
  callback anything a LATER kept punchline refers back to ("Turn the page, boys!" before
           "The only page you're turning is a medical chart.")                          -- kept
  stat     numbers / stat talk with no joke, no name, no pick                           -- dropped
  filler   a short aside to the room ("Now sit down, boys, this one's a story.")        -- dropped

Selection is rule-based: must-keeps first, then the rest by value while the clip fits its budget. Nothing is ever
rewritten; a removed sentence is logged with its reason. The kicker is a verbatim 2-6 word phrase from the punchline
("A MEDICAL CHART.") for the on-screen pop.
"""

from __future__ import annotations

import re

from . import tags as T

_TERM = re.compile(r"([.!?…]+)([\"”’)\]]*)(\s+|$)")
STOP = {"the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for", "with", "is", "are", "was", "were",
        "be", "it", "its", "it's", "this", "that", "that's", "you", "your", "you're", "i", "i'm", "i'll", "me", "my",
        "we", "he", "his", "him", "she", "her", "they", "them", "their", "just", "still", "like", "so", "now", "then",
        "got", "get", "gets", "boys", "buddy", "bud", "one", "ones", "every", "all", "not", "no", "yes", "yeah", "eh",
        "what", "who", "how", "why", "when", "there", "here", "have", "has", "had", "do", "does", "did", "will", "can",
        "take", "takes", "taking", "took", "pick", "picks", "guy", "fine", "sure", "some", "any", "out", "up", "down",
        "back", "into", "from", "by", "as", "if", "than", "too", "very", "real", "well"}
STAT = {"points", "point", "goals", "goal", "assists", "assist", "minutes", "minute", "games", "game", "season",
        "seasons", "percent", "projection", "projected", "pp1", "pp", "fp", "gp", "fp/gp", "per", "rate", "shots",
        "toi", "plus-minus", "xg", "years", "year", "age", "contract", "cap", "hit", "million", "tier"}
NUM_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
             "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
             "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred"}
CROWD = {"boys", "fellas", "gentlemen", "folks", "everybody", "everyone", "gents", "lads"}
PICK_VERBS = {"take", "taking", "takes", "select", "selects", "selecting", "grab", "grabbing", "grabs", "pick",
              "picking", "draft", "drafting", "call", "calling"}
YOU = {"you", "your", "you're", "youre", "yours", "ya"}


def _w(s: str) -> list[str]:
    return [re.sub(r"[^a-z0-9'/-]", "", x.lower().replace("’", "'")) for x in s.split()]


def split_parts(text: str) -> list[str]:
    """Sentences of a TTS text (tags attached to the sentence they precede). An ellipsis followed by a lowercase
    word does not end the sentence."""
    parts, start = [], 0
    for m in _TERM.finditer(text):
        end = m.end(2)
        rest = text[m.end():]
        nxt = re.match(r"(?:\[[^\]]*\]\s*)*([\"“]?)([A-Za-z0-9])", rest)
        if m.group(1) in ("...", "…", "..") and nxt and nxt.group(2).islower():
            continue
        seg = text[start:end].strip()
        if seg:
            parts.append(seg)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    # a part that is only tags joins the next sentence
    out: list[str] = []
    carry = ""
    for p in parts:
        if not T.strip(p).strip():
            carry = (carry + " " + p).strip()
            continue
        out.append((carry + " " + p).strip() if carry else p)
        carry = ""
    if carry and out:
        out[-1] = out[-1] + " " + carry
    return out


def punch_score(sent: str, i: int = 0) -> float:
    from .party import PRAISE, ROAST
    ws = _w(sent)
    sc = 2.0 if "like" in ws[1:] else 0.0
    sc += sum(1.0 for x in ws if x in PRAISE or x in ROAST)
    sc += 1.0 if any(x in YOU for x in ws) else 0.0
    sc += 0.5 if re.search(r"[!?][\"”')\]]*$", sent.strip()) else 0.0
    if any(x in CROWD for x in ws) and len(ws) <= 9:
        sc -= 1.5
    return sc + 0.1 * i


def is_stat(sent: str) -> bool:
    from .party import PRAISE, ROAST
    ws = _w(sent)
    nums = sum(1 for x in ws if re.search(r"\d", x) or x.rstrip("-") in NUM_WORDS or "-" in x and
               any(p in NUM_WORDS for p in x.split("-")))
    stats = sum(1 for x in ws if x in STAT)
    joke = any(x in PRAISE or x in ROAST for x in ws) or "like" in ws[1:] or any(x in YOU for x in ws)
    return (nums + stats) >= 2 and not joke and not any(x in PICK_VERBS for x in ws)


def is_filler(sent: str) -> bool:
    from .party import PRAISE, ROAST
    ws = _w(sent)
    return (any(x in CROWD for x in ws) and len(ws) <= 9 and not any(x in PRAISE or x in ROAST for x in ws)
            and not any(x in PICK_VERBS for x in ws) and "?" not in sent)


STOP |= {"though", "although", "really", "actually", "maybe", "about", "right", "going", "gonna", "gotta", "because",
         "their", "there", "these", "those", "which", "while", "where", "would", "could", "should", "first", "never",
         "always", "again", "other", "another", "thing", "things", "still", "guys", "boys", "fellas", "tonight",
         "today", "little", "pretty", "sweet", "beaut", "beauty", "solid", "nice", "good", "great", "whole", "enough"}


def callback_words(sent: str, names: set[str]) -> set[str]:
    """Words that make a callback: distinctive (>= 5 letters), not a function word, not a player / team / GM name."""
    return {w for w in content_words(sent) if len(w) >= 4 and w not in STOP and w not in names}


def content_words(sent: str) -> set[str]:
    out = set()
    for x in _w(sent):
        x = x.strip("'")
        if len(x) >= 4 and x not in STOP and x not in STAT and not x.isdigit():
            if x.endswith("ing") and len(x) > 6:
                x = x[:-3]  # turning -> turn
            elif x.endswith("ed") and len(x) > 5:
                x = x[:-2]
            out.add(x[:-1] if x.endswith("s") and len(x) > 4 else x)
    return out


def strong_callback(a: set[str], b: set[str]) -> bool:
    """A real callback shares two content words ("turn the page" / "the only page you're turning") or one distinctive
    word of 5+ letters ("fence", "napkin"); one short common word ("need") is a coincidence."""
    shared = a & b
    return len(shared) >= 2 or any(len(w) >= 5 for w in shared)


def kicker(sent: str) -> tuple[str, int] | None:
    """(phrase, first word index within the sentence): a verbatim 2-6 word pop from the punchline.
    'X is like Y' / 'like Y' similes -> Y (its last clause when long); 'is / was / are Y' -> Y; else the last 2-4
    words. Always contiguous words of the sentence."""
    words = T.strip(sent).split()
    if len(words) < 2:
        return None
    low = [re.sub(r"[^a-z']", "", w.lower()) for w in words]

    def clip(a: int, b: int) -> tuple[int, int]:
        b = min(b, len(words))
        if b - a > 6:
            ands = [k for k in range(a + 1, b - 1) if low[k] in ("and", "but")]
            if ands and b - (ands[-1] + 1) >= 2:
                a = ands[-1] + 1
            if b - a > 6:
                preps = [k for k in range(a + 2, b) if low[k] in ("at", "in", "on", "from", "with", "for", "to", "by")]
                if preps:
                    b = preps[0]
            if b - a > 6:
                a = b - 4
        return a, b

    for k in range(len(low) - 1, 0, -1):  # the LAST simile is the joke
        if low[k] == "like" and len(words) - (k + 1) >= 2:
            a, b = clip(k + 1, len(words))
            return " ".join(words[a:b]), a
    for k in range(len(low) - 1, 0, -1):
        if low[k] in ("is", "was", "are", "were", "'s") and len(words) - (k + 1) >= 2:
            a, b = clip(k + 1, len(words))
            if 2 <= b - a <= 6:
                return " ".join(words[a:b]), a
    n = min(4, len(words))
    return " ".join(words[len(words) - n:]), len(words) - n


def roles(parts: list[str], player: str | None, beat2_sentence: int | None) -> list[str]:
    """Role per sentence (see the module docstring)."""
    from .party import name_hit
    disp = [T.strip(p) for p in parts]
    n = len(disp)
    pick_i = beat2_sentence
    if pick_i is None and player:
        pick_i = next((i for i, s in enumerate(disp) if name_hit(s, player)), None)
    out = ["other"] * n
    if pick_i is not None and 0 <= pick_i < n:
        out[pick_i] = "pick"
    beat1 = list(range(0, pick_i)) if pick_i is not None else list(range(n))
    cands = [i for i in beat1 if not is_filler(disp[i]) and not is_stat(disp[i])]
    if cands:
        pi = max(cands, key=lambda i: punch_score(disp[i], i))
        out[pi] = "punch"
        if pi > 0 and out[pi - 1] == "other" and len(disp[pi - 1].split()) <= 8:
            out[pi - 1] = "setup"
    if pick_i is not None:
        if pick_i > 0 and out[pick_i - 1] == "other" and len(disp[pick_i - 1].split()) <= 2:
            out[pick_i - 1] = "hinge"
        if pick_i + 1 < n:
            out[n - 1] = "button" if out[n - 1] == "other" else out[n - 1]
    for i, s in enumerate(disp):
        if out[i] == "other":
            out[i] = "stat" if is_stat(s) else ("filler" if is_filler(s) else "other")
    return out


VALUE = {"punch": 1000, "pick": 1000, "button": 60, "callback": 55, "hinge": 35, "setup": 20, "other": 10,
         "filler": -1, "stat": -1}



def _ctoks(text: str) -> list[str]:
    return [w for w in _w(T.strip(text)) if len(w.strip("'")) >= 3]


def catchphrase_parts(team, parts: list[str], player: str | None = None) -> set[int]:
    """Indexes of the sentences that are the GM's own catchphrase or signature call (or a short exclamation on their own
    nickname, "Saucin' it!"): filler for the audience -- never a punch, never a laugh. A sentence that names the
    drafted player is the pick, never filler; a sentence must be mostly the catchphrase, not merely echo it."""
    if team is None or not getattr(team, "has_persona", False):
        return set()
    from .party import name_hit
    phrases = [x for x in (team.p("catchphrase"), team.p("signature_call")) if x]
    refs = []
    for ph in phrases:
        refs.append(set(_ctoks(ph)))
        refs += [set(_ctoks(x)) for x in split_parts(T.strip(ph))]
    refs = [r for r in refs if r]
    nick = re.findall(r"""['"“‘]([^'"”’]+)['"”’]""", team.gm_name or "")
    stem = re.sub(r"[^a-z]", "", nick[0].lower())[:4] if nick else ""
    out = set()
    for i, part in enumerate(parts):
        words = T.strip(part).split()
        toks = set(_ctoks(part))
        if not words or (player and name_hit(T.strip(part), player)):
            continue
        for r in refs:
            shared = toks & r
            if (len(shared) >= 2 and len(shared) >= 0.6 * min(len(toks), len(r)) and len(toks - r) <= 3) or \
                    (len(words) <= 5 and r and r <= toks):
                out.add(i)
                break
        if i not in out and stem and len(stem) == 4 and len(words) <= 4 and \
                any(re.sub(r"[^a-z]", "", w.lower()).startswith(stem) for w in words):
            out.add(i)
    return out
