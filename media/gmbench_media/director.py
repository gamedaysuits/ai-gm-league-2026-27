"""Director pass: may ONLY add delivery hints (audio tags from the safe list) to tts_text.

It never returns or edits words. The LLM mode asks a model for {line_id: [tags]} and we build
tts_text ourselves as "<tags> <original text>", so a model cannot alter a GM's words even if it
tried. Host "polish" is a separate, optional step with fact-preservation checks; template-only is
the default and needs no network.
"""

from __future__ import annotations

import json
import re

from .script import TAG_RE

COMPETITOR_HINT = "a league competitor model; the director/polish model must be non-competing"


def sanitize_tags(tags: list[str], safe: list[str]) -> list[str]:
    ok = []
    safe_l = {s.lower(): s for s in safe}
    for t in tags or []:
        k = str(t).strip().strip("[]").lower()
        if k in safe_l and safe_l[k] not in ok:
            ok.append(safe_l[k])
    return ok[:2]


def apply_hints(rundown: dict, hints: dict[str, list[str]], safe: list[str]) -> int:
    """Prepend director tags. Never removes or edits anything: a GM's own inline tags stay exactly as written."""
    n = 0
    for ln in rundown["timeline"]:
        have = {m.group(1).strip().lower() for m in TAG_RE.finditer(ln["text"])}
        tags = [t for t in sanitize_tags(hints.get(ln["id"], []), safe) if t.lower() not in have]
        if tags:
            ln["text"] = " ".join(f"[{t}]" for t in tags) + " " + ln["text"]
        ln["delivery"] = tags
        n += bool(tags)
    return n


def rule_hints(rundown: dict) -> dict[str, list[str]]:
    """Deterministic defaults. Deliberately sparse: the voices carry the personas."""
    hints: dict[str, list[str]] = {}
    # e.g. hints["open-01"] = ["confident"]; the host stays neutral by default and GM delivery comes
    # from each GM's designed voice, so the rules pass currently adds nothing.
    return hints


def competitor_models(league) -> set[str]:
    return {t.model for t in league.teams.values() if t.model}


def llm_hints(rundown: dict, cfg: dict, league) -> dict[str, list[str]]:  # pragma: no cover - network
    """Optional: ask a non-competing model for tags only. Disabled unless director.mode == 'llm'."""
    import httpx

    from .tts import load_env

    model = (cfg["script"].get("director") or {}).get("model")
    if not model:
        raise SystemExit("director.mode=llm needs script.director.model (a non-competing model)")
    if model in competitor_models(league):
        raise SystemExit(f"{model} is {COMPETITOR_HINT}")
    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY missing from .env")
    safe = cfg["tts"]["safe_tags"]
    items = [{"id": ln["id"], "speaker": ln["speaker"], "text": ln["display_text"][:400]}
             for ln in rundown["timeline"]]
    prompt = ("You are a broadcast director. For each line, choose at most two delivery tags from this list "
              f"only: {safe}. Return JSON {{\"<id>\": [tags]}}. Do not return or rewrite any text.\n"
              + json.dumps(items, ensure_ascii=False))
    r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=120,
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": model, "messages": [{"role": "user", "content": prompt}],
                         "response_format": {"type": "json_object"}})
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    return {k: v for k, v in data.items() if isinstance(v, list)}


def direct(rundown: dict, cfg: dict, league) -> dict:
    mode = (cfg["script"].get("director") or {}).get("mode", "rules")
    safe = cfg["tts"]["safe_tags"]
    hints = llm_hints(rundown, cfg, league) if mode == "llm" else rule_hints(rundown)
    rundown["director"] = {"mode": mode, "tagged_lines": apply_hints(rundown, hints, safe)}
    return rundown


# --------------------------------------------------------------------------- host polish (optional)

FACT_RE = re.compile(r"\d+|[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)*")


def facts_preserved(template: str, polished: str) -> bool:
    return all(f in polished for f in FACT_RE.findall(template))


def polish_host(rundown: dict, cfg: dict, league) -> dict:
    """Optional Game-7 polish of host template lines by a NON-competing model (see polish.py)."""
    pol = cfg["script"].get("host_polish") or {}
    if not pol.get("enabled"):
        rundown["host_polish"] = {"enabled": False}
        return rundown
    from .polish import polish_lines
    from .tts import load_env

    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY missing from .env")
    stats = polish_lines(rundown, league, pol.get("model") or "cohere/command-a-plus", key)
    rundown["host_polish"] = {"enabled": True, **stats}
    return rundown
