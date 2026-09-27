"""Beat map: the comedic structure of every GM line, sentence by sentence.

    SETUP  | PUNCH | BUTTON | PICK | FILLER | STAT      (+ kicker = the last 1-4 words of a PUNCH / BUTTON,
                                                           spice 1 mild / 2 solid / 3 savage)

A non-league LLM labels (it never writes: labels are validated against the sentences it was given), with the rules
of tighten.py as the fallback; answers are cached per line. The beat map drives the edit (which sentences stay), the
timing (micro-beat before a punch, a laugh beat after it, the reaction cut 80 ms after the kicker) and the QA gates.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from . import tags as T
from .paths import cache_dir
from .tighten import kicker as rule_kicker
from .tighten import roles as rule_roles
from .tighten import split_parts

ROLES = ("SETUP", "PUNCH", "BUTTON", "PICK", "FILLER", "STAT")
LABEL_MODELS = ["cohere/command-a", "mistralai/mistral-medium-3.1"]  # never a league model
PROMPT_V = 4
PROMPT = """You are a comedy editor labelling lines from a fantasy-hockey draft party where AI models, playing hockey
GMs, rib each other. You LABEL; you never rewrite. A GM's pick call usually goes: rib the previous GM (the reaction
beat), then announce the pick, then maybe a closing tag. Label each numbered sentence:
  SETUP  sets up a joke or the pick (context the joke needs)
  PUNCH  the sentence where the joke on another GM lands (usually in the reaction beat, before the pick)
  PICK   announces the drafted player
  BUTTON a short closing zinger AFTER the pick (or ending a comeback)
  FILLER an aside that adds nothing (e.g. "Now sit down, boys, this one's a story.")
  STAT   numbers or stats talk with no joke
For PUNCH and BUTTON also give kicker = how many of the sentence's LAST words carry the laugh (1-4), and spice =
1 mild, 2 solid, 3 savage.
Example line: 0 "Sparky, the robot lifted your guy and you still got Makar, that's like losing your wallet and finding a
truck." 1 "Now sit down, boys, this one's a story." 2 "Every year Kirill Kaprizov gets the top-five talent tag and
every year he slides." 3 "Not tonight." -> [{"i":0,"role":"PUNCH","kicker":3,"spice":2},{"i":1,"role":"FILLER"},
{"i":2,"role":"PICK"},{"i":3,"role":"BUTTON","kicker":2,"spice":1}]
Return ONLY JSON: {"lines": {"<line id>": [{"i": 0, "role": "..."}, ...]}} with one entry per sentence, in order.

"""


def _rules(parts: list[str], player: str | None, beat2: int | None) -> list[dict]:
    rr = rule_roles(parts, player, beat2)
    m = {"punch": "PUNCH", "pick": "PICK", "button": "BUTTON", "setup": "SETUP", "hinge": "SETUP", "callback": "SETUP",
         "stat": "STAT", "filler": "FILLER", "other": "FILLER"}
    out = []
    from .party import PRAISE, ROAST
    for i, (p, r) in enumerate(zip(parts, rr)):
        role = m.get(r, "FILLER")
        row = {"i": i, "role": role}
        if role in ("PUNCH", "BUTTON"):
            words = T.strip(p).split()
            k = rule_kicker(p)
            n = (len(words) - k[1]) if (k and k[1] + len(k[0].split()) == len(words)) else 3
            ws = [re.sub(r"[^a-z']", "", w.lower()) for w in words]
            row["kicker"] = max(1, min(4, n, len(words)))
            row["spice"] = max(1, min(3, 1 + sum(1 for w in ws if w in ROAST) + (1 if "like" in ws[1:] else 0)))
        out.append(row)
    return out


def _validate(raw: list, parts: list[str], player: str | None) -> list[dict] | None:
    from .party import name_hit
    if not isinstance(raw, list) or len(raw) != len(parts):
        return None
    out = []
    for i, (row, p) in enumerate(zip(raw, parts)):
        if not isinstance(row, dict):
            return None
        role = str(row.get("role") or "").upper()
        if role not in ROLES:
            return None
        n_words = len(T.strip(p).split())
        r = {"i": i, "role": role}
        if player and name_hit(T.strip(p), player):
            r["role"] = "PICK"  # the sentence naming the drafted player is the pick, whatever else it does
        if r["role"] in ("PUNCH", "BUTTON"):
            try:
                r["kicker"] = max(1, min(4, int(row.get("kicker") or 3), n_words))
                r["spice"] = max(1, min(3, int(row.get("spice") or 2)))
            except (TypeError, ValueError):
                return None
        out.append(r)
    if player and not any(x["role"] == "PICK" for x in out):
        pass  # a comeback / table talk names no pick: fine
    return out


def sanity(labels: list[dict], parts: list[str], player: str | None, addressed: bool) -> list[dict]:
    """Structure beats the labeller: a BUTTON comes after the pick (a room aside before it is FILLER); a PUNCH has a
    joke's length (>= 4 words); a call aimed at someone has a PUNCH in its reaction beat (the best rule-scored
    sentence is promoted when the labeller found none)."""
    from .tighten import is_filler, is_stat, punch_score
    disp = [T.strip(x) for x in parts]
    out = [dict(x) for x in labels]
    pick_i = next((i for i, x in enumerate(out) if x["role"] == "PICK"), None)
    for i, x in enumerate(out):
        words = len(disp[i].split())
        tag_after_punch = i > 0 and out[i - 1]["role"] == "PUNCH" and words <= 4 and not is_filler(disp[i])
        if x["role"] in ("SETUP", "FILLER") and tag_after_punch and pick_i is not None and i < pick_i:
            x["role"] = "BUTTON"  # a short tag right on the punch ("Bold.") is its button
            x.setdefault("kicker", words)
            x.setdefault("spice", 1)
        if x["role"] == "BUTTON" and pick_i is not None and i < pick_i and not tag_after_punch:
            x["role"] = "FILLER" if is_filler(disp[i]) else ("STAT" if is_stat(disp[i]) else "SETUP")
        if x["role"] == "PUNCH" and words < 4:
            x["role"] = "BUTTON" if (pick_i is not None and i > pick_i) or pick_i is None else "SETUP"
        if x["role"] not in ("PUNCH", "BUTTON"):
            x.pop("kicker", None)
            x.pop("spice", None)
        else:
            if "kicker" not in x:
                x["kicker"] = min(3, words)
                x["spice"] = x.get("spice", 1)
            ws = [re.sub(r"[^a-z']", "", w.lower()) for w in disp[i].split()]
            k = int(x["kicker"])
            while k > 1 and ws[-k] in ("is", "are", "was", "were", "at", "to", "of", "in", "on", "and", "but", "that's",
                                       "it's", "like", "so", "just", "for", "with", "from", "by", "as"):
                k -= 1  # the kicker starts on the joke, not on 'is' / 'at' / 'and'
            x["kicker"] = k
    if pick_i is None and addressed and not any(x["role"] == "PUNCH" for x in out):  # a comeback: its best shot
        cands = [i for i in range(len(out)) if out[i]["role"] in ("BUTTON", "SETUP") and len(disp[i].split()) >= 4]
        if cands:
            i = max(cands, key=lambda k: (out[k]["role"] == "BUTTON", punch_score(disp[k], 0)))
            out[i] = {"i": i, "role": "PUNCH", "kicker": out[i].get("kicker") or 3, "spice": out[i].get("spice") or 2,
                      "promoted": True}
    beat1 = list(range(0, pick_i)) if pick_i is not None else []
    if addressed and beat1 and not any(out[i]["role"] == "PUNCH" for i in beat1):
        cands = [i for i in beat1 if not is_filler(disp[i]) and not is_stat(disp[i]) and len(disp[i].split()) >= 4]
        if cands:
            i = max(cands, key=lambda k: punch_score(disp[k], k))
            out[i] = {"i": i, "role": "PUNCH", "kicker": 3, "spice": 2, "promoted": True}
    for i in range(1, len(out)):  # after the promotions: a short tag right on a punch ("Bold.") is its button
        words = len(disp[i].split())
        if out[i]["role"] in ("SETUP", "BUTTON") and out[i - 1]["role"] == "PUNCH" and words <= 3 \
                and not is_filler(disp[i]) and (pick_i is None or i < pick_i):
            out[i] = {"i": i, "role": "BUTTON", "kicker": words, "spice": 1, "tag": True}
    for i in range(1, len(out)):  # a punch right after a punch (+ its tag) tops it: it never opens the short alone
        j = i - 1 - (1 if out[i - 1].get("tag") else 0)
        if out[i]["role"] == "PUNCH" and j >= 0 and out[j]["role"] == "PUNCH":
            out[i]["topper"] = True
    return out


def _key(line_text: str, ctx: dict) -> str:
    return hashlib.sha1(json.dumps({"v": PROMPT_V, "t": line_text, "c": ctx}, sort_keys=True).encode()).hexdigest()[:20]


def label_lines(items: list[dict], log=print, use_llm: bool = True) -> dict[str, dict]:
    """items: [{id, text (TTS text with tags), speaker, to, player, kind, beat2}] -> {id: {"parts", "labels",
    "source": "llm:<model>" | "rules"}}. Cached per line; one request labels every uncached line."""
    cdir = cache_dir("beatmap")
    out: dict[str, dict] = {}
    todo = []
    for it in items:
        parts = split_parts(it["text"])
        ctx = {k: it.get(k) for k in ("speaker", "to", "player", "kind")}
        key = _key(it["text"], ctx)
        f = cdir / f"{key}.json"
        if f.exists():
            out[it["id"]] = json.loads(f.read_text())
            continue
        todo.append((it, parts, ctx, f))
    if todo and use_llm:  # batches of 20 lines, in parallel (a whole draft is ~300 lines)
        from concurrent.futures import ThreadPoolExecutor
        chunks = [todo[k:k + 20] for k in range(0, len(todo), 20)]

        def ask(ch):
            return ch, _ask([(it["id"], parts, ctx) for it, parts, ctx, _ in ch], log)

        with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as ex:
            answers = list(ex.map(ask, chunks))
        for ch, got in answers:
            for it, parts, ctx, f in ch:
                labels = _validate(got.get("lines", {}).get(it["id"]), parts, it.get("player")) if got else None
                src = f"llm:{got.get('model')}" if labels else "rules"
                if not labels:
                    labels = _rules(parts, it.get("player"), it.get("beat2"))
                rec = {"parts": parts, "labels": labels, "source": src,
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                f.write_text(json.dumps(rec, indent=1, ensure_ascii=False))
                out[it["id"]] = rec
    for it, parts, ctx, f in todo:
        if it["id"] not in out:
            rec = {"parts": parts, "labels": _rules(parts, it.get("player"), it.get("beat2")), "source": "rules"}
            out[it["id"]] = rec
    for it in items:
        rec = out[it["id"]]
        rec["labels"] = sanity(rec["labels"], rec["parts"], it.get("player"), bool(it.get("to")))
    return out


def _ask(batch: list[tuple[str, list[str], dict]], log=print) -> dict:
    """One labelling request for many lines. {} on any failure (the caller falls back to the rules)."""
    try:
        import httpx

        from .tts import load_env
    except Exception:  # noqa: BLE001
        return {}
    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        return {}
    blocks = []
    for lid, parts, ctx in batch:
        head = f"LINE {lid} (speaker {ctx.get('speaker')}"
        if ctx.get("to"):
            head += f", speaking to {ctx['to']}"
        if ctx.get("player"):
            head += f", drafting {ctx['player']}"
        head += f", kind {ctx.get('kind')})"
        blocks.append(head + "\n" + "\n".join(f"  {i}. {T.strip(p)}" for i, p in enumerate(parts)))
    prompt = PROMPT + "\n\n".join(blocks)
    for model in LABEL_MODELS:
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=120,
                           json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                 "max_tokens": 4000, "temperature": 0},
                           headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
        except Exception as e:  # noqa: BLE001
            log(f"beatmap: {model}: {type(e).__name__}")
            continue
        if r.status_code != 200:
            log(f"beatmap: {model}: HTTP {r.status_code}")
            continue
        txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        data["model"] = model
        log(f"beatmap: labelled {len(batch)} lines with {model} (labels only; validated)")
        return data
    return {}


def kicker_span(part: str, label: dict) -> tuple[int, int] | None:
    """(first, last) word index of the kicker within the sentence's display words."""
    if label.get("role") not in ("PUNCH", "BUTTON"):
        return None
    words = T.strip(part).split()
    n = int(label.get("kicker") or 3)
    return (max(0, len(words) - n), len(words) - 1) if words else None
