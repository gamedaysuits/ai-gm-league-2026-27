"""Optional host polish: a NON-competing model rewrites host template lines as a Game-7 overtime call.

Facts are locked: every player / franchise / GM / model name and every number in the template must survive
verbatim (case-insensitive), no new numbers may appear, tags must be on the allowlist, and the line may not
grow past 1.6x. Anything that fails validation keeps the template. Results are cached by content hash, so a
re-run never re-bills and the show is reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import tags as T
from .nhl import number_words
from .paths import cache_dir

STYLE_VERSION = 3
STYLE_GUIDE = (
    "You are the play-by-play voice of Game 7 of the Stanley Cup Final, in overtime, calling a FANTASY HOCKEY DRAFT "
    "run by AI general managers. Rewrite the host line below for text-to-speech: explosive, funny, arena-sized, "
    "rapid-fire; build suspense with ellipses and hit the payoff in CAPS. Rules: keep EVERY locked phrase exactly as "
    "written (same words and spelling; capitalisation may change); add NO new facts, stats, numbers, names, teams, "
    "predictions or claims; never insult or characterise a real person; you may use only these delivery tags in "
    "square brackets: " + " ".join(f"[{t}]" for t in T.ALLOWED) + ". Keep it under {max_words} words. Return only "
    "the rewritten line, no quotes, no commentary."
)
POLISH_KINDS = {"host_pick", "host_clock", "host_open", "host_round", "host_intro", "host_seg", "host_close",
                "host_question", "host_order", "auto_note"}
NUM_WORDS = {number_words(i) for i in range(0, 200)} | {"hundred", "thousand"}


def locked_facts(line: dict, league) -> list[str]:
    text = T.strip(line["text"])
    facts: list[str] = []
    for p in league.picks:
        if p.player_name and p.player_name.lower() in text.lower():
            facts.append(p.player_name)
    for t in league.teams.values():
        for name in (t.franchise_name, t.nickname, t.gm_name, t.display, t.city or "", t.lab):
            if name and len(name) > 2 and name.lower() in text.lower():
                facts.append(name)
    facts += re.findall(r"\d+", text)
    words = re.findall(r"[a-z]+(?:-[a-z]+)?", text.lower())
    facts += [w for w in words if w in NUM_WORDS or w.endswith(("first", "second", "third")) or
              re.fullmatch(r"(?:[a-z]+-)?[a-z]+th", w) and w[:-2] in NUM_WORDS]
    return sorted(set(f for f in facts if f), key=len, reverse=True)


def numbers_in(text: str) -> set[str]:
    words = set(re.findall(r"[a-z]+(?:-[a-z]+)?", text.lower()))
    return set(re.findall(r"\d+", text)) | {w for w in words if w in NUM_WORDS}


def validate(template: str, cand: str, facts: list[str]) -> str | None:
    if not cand or "\n" in cand.strip() or len(cand) > 1.6 * len(template) + 40:
        return "length/format"
    words = T.strip(cand).lower()
    for f in facts:
        if f.lower() not in words:
            return f"lost fact: {f}"
    if not numbers_in(T.strip(cand)) <= numbers_in(T.strip(template)) | {f.lower() for f in facts}:
        return "new number"
    if any(t not in T.ALLOWED for t in T.tags_in(cand)):
        return "tag not allowed"
    if re.search(r"[\"“”*_#]", cand):
        return "markup/quotes"
    return None


def polish_lines(rundown: dict, league, model: str, key: str, log=print, max_lines: int | None = None) -> dict:
    import httpx

    competitors = {t.model for t in league.teams.values() if t.model}
    if model in competitors:
        raise SystemExit(f"{model} competes in the league; the host polish model must be non-competing")
    cdir = cache_dir("polish")
    stats = {"model": model, "polished": 0, "kept_template": 0, "rejected": {}}
    n = 0
    for ln in rundown["timeline"]:
        if ln["speaker"] != "host" or ln["kind"] not in POLISH_KINDS:
            continue
        if max_lines is not None and n >= max_lines:
            break
        n += 1
        template = ln.get("template_text") or ln["text"]
        facts = locked_facts({**ln, "text": template}, league)
        h = hashlib.sha256(json.dumps([STYLE_VERSION, model, template], ensure_ascii=False).encode()).hexdigest()[:24]
        f = cdir / f"{h}.json"
        if f.exists():
            cand = json.loads(f.read_text())["text"]
        else:
            prompt = STYLE_GUIDE.replace("{max_words}", str(max(12, int(len(template.split()) * 1.5)))) + \
                "\n\nLocked phrases: " + "; ".join(facts) + "\n\nHost line:\n" + template
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=60,
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": model, "temperature": 0.8, "max_tokens": 1600,
                                 "reasoning": {"effort": "low"},
                                 "messages": [{"role": "user", "content": prompt}]})
            if r.status_code != 200:
                stats["rejected"][ln["id"]] = f"HTTP {r.status_code}"
                continue
            cand = (r.json()["choices"][0]["message"].get("content") or "").strip().strip('"').strip()
            if not cand:
                stats["rejected"][ln["id"]] = "empty"
                continue
            f.write_text(json.dumps({"template": template, "text": cand, "model": model, "style": STYLE_VERSION}))
        cand = T.sanitize(cand)
        why = validate(template, cand, facts)
        if why:
            stats["rejected"][ln["id"]] = why
            stats["kept_template"] += 1
            continue
        ln["template_text"] = template
        ln["text"] = cand
        ln["display_text"] = T.strip(cand)
        stats["polished"] += 1
    log(f"polish[{model}]: {stats['polished']} polished, {stats['kept_template']} kept template, "
        f"rejected {stats['rejected']}")
    return stats
