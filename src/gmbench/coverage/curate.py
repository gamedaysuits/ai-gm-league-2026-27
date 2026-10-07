"""Pick the best of a week's chatter with a panel of NON-league models (the same panel that judges Draft Night).

A line is featured only if every judge that answered says it makes sense, isn't mean, and gets no checkable fact
wrong against the week's facts. Survivors are ranked by how funny and how quotable the panel found them. Verdicts
are cached per week (``runs/<run>/coverage/<week>/judge.json``) so a re-run never re-asks or changes its mind.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from gmbench.coverage.facts import Line, WeekFacts

PANEL = ["minimax/minimax-m3", "mistralai/mistral-medium-3-5"]
FEATURE = 6  # lines featured per week
PER_TEAM = 1  # six different models a week, not six Geminis

PROMPT = """You are the editor of a weekly hockey column about an AI fantasy hockey league. Thirteen AI models (they go
by their model names: Gemini, Opus, Qwen...) each run a fantasy team; a fourteenth team, "the robot" (Autodraft), is a
silent control bot. Every Monday the models set lineups, claim free agents, make trade offers, post in the league chat
and give a one-line press quote. They chirp each other like a dressing room.

Below are this week's FACTS (from the league's official ledger) and every LINE the models wrote. Judge each line for
a public article on a hockey store's blog. For each line answer:
- "sense": "yes" if a hockey fan reading it cold, with the context given, gets it instantly. "no" for riddles,
  word salad, or AI jargon a fan wouldn't follow.
- "mean": "yes" if it insults or demeans a model or a real person beyond friendly chirping (cruel, humiliating,
  identity-based, profane). Friendly trash talk is "no".
- "fact_wrong": "yes" ONLY if it states something checkable that the FACTS contradict (wrong points, wrong place in
  the standings, a player on the wrong team, a trade that didn't happen). Opinions, predictions, hyperbole and jokes
  are not facts. If the FACTS can't settle it, "no".
- "why": one short sentence (required when sense is "no", mean is "yes" or fact_wrong is "yes").
- "funny": 0-3. 0 = not trying or doesn't land, 1 = a smile, 2 = a real laugh, 3 = you'd read it out loud to the
  room. Stock internet phrases and generic boasts cap at 1.
- "quotable": 0-3. How well it works pulled out as a quote in an article (short, punchy, self-contained).

Answer with JSON only: {"lines": [{"id": "...", "sense": "yes|no", "mean": "yes|no", "fact_wrong": "yes|no",
"why": "...", "funny": 0, "quotable": 0}, ...]} with one entry for every line id.

FACTS:
{facts}

LINES:
{lines}
"""


def facts_brief(f: WeekFacts) -> str:
    out = [f"Week {f.number} front office (week of {f.week_start:%B %-d, %Y}). Trading is "
           f"{'open' if f.trades_open else 'closed'} this week."]
    if f.results_dates:
        out.append(f"Results below cover games from {f.results_dates[0]} to {f.results_dates[-1]}.")
    out.append("Standings (rank. model [team] total points, points last week, rank a week earlier):")
    for s in f.standings:
        t = f.teams[s["team"]]
        out.append(f"{s['rank']}. {t.call} = {t.credit} [{t.franchise or 'Autodraft'}] {s['points']} pts, "
                   f"{s['week_points']} last week, was {s['prev_rank'] or '-'}")
    out.append("Rosters (player, position, NHL club):")
    for tid, roster in f.rosters.items():
        out.append(f"{f.teams[tid].call}: " + "; ".join(f.label(p) for p in roster))
    if f.top_players:
        out.append("Best counted performances last week: " + "; ".join(
            f"{f.name(r['pid'])} for {f.teams[r['team']].call}: {r['pts']} pts" for r in f.top_players))
    for t in f.trades:
        out.append(f"Trade {t.trade_id}: {f.teams[t.proposer].call} offers "
                   f"{', '.join(f.name(p) for p in t.give)} for {', '.join(f.name(p) for p in t.get)} from "
                   f"{f.teams[t.recipient].call}; final status {t.status}.")
    for w in f.waivers:
        out.append(f"Waivers: {f.teams[w['team']].call} added {f.name(w['add'])}, dropped {f.name(w['drop'])}.")
    return "\n".join(out)


def _lines_brief(f: WeekFacts) -> str:
    rows = []
    for ln in f.lines:
        who = f.teams[ln.team]
        rows.append(json.dumps({"id": ln.id, "speaker": f"{who.call} ({who.credit})", "kind": ln.kind,
                                "context": ln.context, "text": ln.text}, ensure_ascii=False))
    return "\n".join(rows)


def _ask(model: str, prompt: str, key: str, log=print) -> dict[str, dict[str, Any]] | None:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 16000,
            "response_format": {"type": "json_object"}}
    for attempt in range(3):
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, timeout=600,
                           headers={"Authorization": f"Bearer {key}", "HTTP-Referer": "https://gamedaysuits.ca",
                                    "X-Title": "GDS AI GM League (coverage)"})
            data = r.json()
            txt = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{.*\}", txt, re.S)
            rows = json.loads(m.group(0))["lines"] if m else None
            if rows:
                return {str(x["id"]): x for x in rows if isinstance(x, dict) and "id" in x}
            log(f"coverage judge {model}: empty answer (attempt {attempt + 1})")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log(f"coverage judge {model}: {type(exc).__name__}: {exc} (attempt {attempt + 1})")
    return None


def judge(f: WeekFacts, cache: Path, *, log=print) -> dict[str, dict[str, Any]]:
    """{line id: {"ok": bool, "score": float, "why": [...], "by": {model: verdict}}} for every line (cached)."""
    verdicts: dict[str, dict[str, Any]] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [ln for ln in f.lines if verdicts.get(ln.id, {}).get("text") != ln.text]
    key = os.environ.get("OPENROUTER_API_KEY")
    if todo and not key:
        log("coverage judge: no OPENROUTER_API_KEY; nothing new is judged, so nothing new is featured")
        return verdicts
    if todo:
        sub = WeekFacts(**{**f.__dict__, "lines": todo})
        prompt = PROMPT.replace("{facts}", facts_brief(f)).replace("{lines}", _lines_brief(sub))
        answers = {m: _ask(m, prompt, key, log) for m in PANEL}
        answered = {m: a for m, a in answers.items() if a}
        if not answered:
            log("coverage judge: every judge failed; will retry next run")
            return verdicts
        for ln in todo:
            by = {m: a[ln.id] for m, a in answered.items() if ln.id in a}
            if not by:
                continue
            bad = [v for v in by.values() if str(v.get("sense")).lower() != "yes" or str(v.get("mean")).lower() == "yes"
                   or str(v.get("fact_wrong")).lower() == "yes"]
            score = sum(2 * _num(v.get("funny")) + _num(v.get("quotable")) for v in by.values()) / len(by)
            verdicts[ln.id] = {"text": ln.text, "ok": not bad, "score": round(score, 2), "why": [v.get("why") for v in bad if v.get("why")],
                               "by": by}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(verdicts, indent=1, ensure_ascii=False))
    return verdicts


def _num(v: Any) -> float:
    try:
        return max(0.0, min(3.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def featured(f: WeekFacts, verdicts: dict[str, dict[str, Any]], n: int = FEATURE) -> list[tuple[Line, float]]:
    """The best judged lines: passing every check, a laugh or a good quote (score >= 3), at most PER_TEAM per model."""
    ok = sorted(((ln, verdicts[ln.id]["score"]) for ln in f.lines
                 if ln.id in verdicts and verdicts[ln.id]["ok"] and verdicts[ln.id]["score"] >= 3),
                key=lambda x: (-x[1], x[0].id))
    out, per = [], {}
    for ln, s in ok:
        if per.get(ln.team, 0) >= PER_TEAM:
            continue
        per[ln.team] = per.get(ln.team, 0) + 1
        out.append((ln, s))
        if len(out) == n:
            break
    return out
