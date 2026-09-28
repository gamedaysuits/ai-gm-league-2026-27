"""Per-model comedy stats -- the numbers behind "cut model X".

  cd media && uv run python -m gmbench_media.modelstats --runs party-2,party-3,party-4,party-5,party-6,party-7,party-8
  cd media && uv run python -m gmbench_media.modelstats --runs party-10 --out out/review/model-comedy-party-10.md
  (--cached: score only what the judge has already cached; ask nothing)

Every run is judged with the CURRENT panel and prompt first (cached per line: only new lines cost anything), so runs
judged by older prompts are re-scored on the same scale. Per model, over its GM lines (pick calls, comebacks,
reactions, table talk; grade comments when a run has them):

  lines        lines with a verdict for every sentence
  all green    % of lines where every sentence is green once catchphrase filler is cut -- exactly what the complete
               show's all-green policy airs
  shorts       % of lines with at least one sentence that passes the shorts gate (green and funny >= 2)
  funny        mean of each line's best sentence (its punch), 0-3
  instant      % of lines where every sentence lands instantly (no riddle, no jargon, no invented human life)
  consistent   % of lines where every sentence gets tonight right (the board, who said what)
  mean         % of lines with a sentence the panel calls mean
  slop         % of lines with a stock internet phrase ("bold move", "chef's kiss", ...)
  filler       % of lines with a catchphrase / signature-call sentence (cut before airing)
  calls green  green pick calls / pick calls (a call that isn't green is replaced by the host)

Written to media/out/review/model-comedy.md (+ .json). Audits (the producer's per-run verdicts) print beside the stats.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from .judge import PANEL, PROMPT_V, judge_run, spend_since
from .paths import OUT

# The producer's audit of party-8 (Sep 27), per model: ✅ works, 🟡 so-so, ⚠️ problem, ❌ fails.
AUDITS: dict[str, dict[str, str]] = {
    "party-8": {"qwen": "✅✅🟡", "fugu": "✅✅✅", "deepseek": "✅✅", "grok": "❌", "sol": "✅✅", "muse": "✅",
                "mimo": "✅✅", "fable": "✅✅", "gemini": "✅⚠️", "glm": "❌🟡", "opus": "✅✅", "kimi": "✅✅",
                "astra": "🟡"},
}


def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.0f}%" if b else "-"


def line_rows(L, J) -> list[dict]:
    """One row per judged GM line: its model, kind and per-line verdict flags."""
    out = []
    for it in J.items.values():
        t = L.teams.get(it.team)
        if not t or t.is_bot:
            continue
        vs = [J.v.get(f"{it.id[3:]}.{i}") or {} for i in range(len(it.parts))]
        if not vs or not all(v.get("judged") for v in vs):
            continue
        core = [v for v in vs if not v.get("catchphrase")]  # the show cuts catchphrase filler before it airs
        out.append({
            "run": L.run, "team": it.team, "kind": it.kind, "seq": it.seq,
            "green": bool(core) and all(v.get("green") for v in core),
            "shorts": any(v.get("pass") for v in vs),
            "funny": max(float(v.get("funny") or 0.0) for v in vs),
            "instant": all(v.get("lands_instantly") == "yes" for v in vs),
            "consistent": all(v.get("consistent", "yes") == "yes" for v in vs),
            "mean": any(v.get("tone") == "mean" for v in vs),
            "slop": any(v.get("stock_phrase") for v in vs),
            "catchphrase": any(v.get("catchphrase") for v in vs),
            "facts_off": any(v.get("factually_ok") == "no" for v in vs),
            "sense": all(v.get("makes_sense") == "yes" for v in vs),
            "call": it.kind in ("on_air_call", "pick_statement", "call"),
        })
    return out


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    calls = [r for r in rows if r["call"]]
    return {
        "lines": n,
        "green": sum(r["green"] for r in rows), "shorts": sum(r["shorts"] for r in rows),
        "funny": round(statistics.mean(r["funny"] for r in rows), 2) if rows else 0.0,
        "instant": sum(r["instant"] for r in rows), "consistent": sum(r["consistent"] for r in rows),
        "mean": sum(r["mean"] for r in rows), "slop": sum(r["slop"] for r in rows),
        "filler": sum(r["catchphrase"] for r in rows),
        "sense": sum(r["sense"] for r in rows), "facts_off": sum(r["facts_off"] for r in rows),
        "catchphrase": sum(r["catchphrase"] for r in rows),
        "calls": len(calls), "calls_green": sum(r["green"] for r in calls),
    }


def first_fail(r: dict) -> str | None:
    for k, w in (("sense", "doesn't make sense"), ("instant", "doesn't land instantly"),
                 ("consistent", "gets tonight wrong")):
        if not r[k]:
            return w
    if r["facts_off"]:
        return "a fact is wrong"
    if r["mean"]:
        return "mean"
    if r["catchphrase"]:
        return "catchphrase"  # (only when the line is nothing but filler)
    return None


def aired_rows(L, use_llm: bool = True) -> dict[str, dict]:
    """What each model gets on air in the complete show (the current air plan: all green + a laugh, sentence-level
    call cuts, the aired-context check): per team {calls, calls_jokes (a call whose joke airs, not just the pick),
    calls_host, laughs (aired sentences with funny >= 2), says, says_aired}."""
    from .config import load_config
    from .judge import FUNNY_MIN
    from .script import RundownBuilder
    cfg = load_config("complete", {"script": {"comic_timing_llm": use_llm}})
    b = RundownBuilder(L, cfg)
    b.complete_selection()
    J = b.J
    out: dict[str, dict] = {}
    for pl in b.call_plan.values():
        if not pl["gm"]:
            continue
        a = out.setdefault(pl["team"], {"calls": 0, "calls_jokes": 0, "calls_host": 0, "laughs": 0, "says": 0,
                                        "says_aired": 0})
        a["calls"] += 1
        if pl["host"]:
            a["calls_host"] += 1
            continue
        jokes = [i for i in pl["keep"] if i != pl["pick_idx"]]
        a["calls_jokes"] += bool(jokes)
        a["laughs"] += sum(1 for i in pl["keep"] if float((J.v.get(f"{pl['seq']}.{i}") or {}).get("funny") or 0)
                           >= FUNNY_MIN)
    aired_tt = {x for v in b.complete_tt.values() for x in v}
    for x in L.says:
        if x.team not in L.teams or L.teams[x.team].is_bot:
            continue
        a = out.setdefault(x.team, {"calls": 0, "calls_jokes": 0, "calls_host": 0, "laughs": 0, "says": 0,
                                    "says_aired": 0})
        a["says"] += 1
        if x.seq in b.complete_says or x.seq in aired_tt:
            a["says_aired"] += 1
            it = J.items.get(f"seq{x.seq}")
            a["laughs"] += sum(1 for i in range(len(it.parts) if it else 0)
                               if float((J.v.get(f"{x.seq}.{i}") or {}).get("funny") or 0) >= FUNNY_MIN)
    return out


def run_stats(runs: list[str], out: Path, use_llm: bool = True, log=print, aired: bool = True) -> dict:
    from .ledger import load_league
    t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows: list[dict] = []
    teams: dict[str, object] = {}
    judged_runs = []
    air: dict[str, dict] = {}
    for run in runs:
        L = load_league(run)
        J = judge_run(L, log=lambda m: None, use_llm=use_llm)
        if aired:
            for tid, a in aired_rows(L, use_llm).items():
                acc = air.setdefault(tid, {k: 0 for k in a})
                for k, v in a.items():
                    acc[k] += v
        rr = line_rows(L, J)
        log(f"modelstats: {run}: {len(rr)} GM lines judged ({J.unjudged} sentences unjudged)")
        rows += rr
        judged_runs.append({"run": run, "lines": len(rr), "unjudged": J.unjudged})
        for tid, t in L.teams.items():
            if not t.is_bot:
                teams.setdefault(tid, t)
    usd, calls = spend_since(t0)
    per = {tid: aggregate([r for r in rows if r["team"] == tid]) for tid in teams}
    allm = aggregate(rows)
    if air:  # what actually gets on air, per line the model wrote: laughs aired, then all green
        order = sorted(per, key=lambda k: (air.get(k, {}).get("laughs", 0) / max(1, per[k]["lines"]),
                                           per[k]["green"] / max(1, per[k]["lines"]), per[k]["funny"]), reverse=True)
    else:
        order = sorted(per, key=lambda k: (per[k]["green"] / max(1, per[k]["lines"]), per[k]["funny"]), reverse=True)
    med = statistics.median([per[k]["green"] / max(1, per[k]["lines"]) for k in per if per[k]["lines"]] or [0.0])
    audits = {run: AUDITS[run] for run in runs if run in AUDITS}

    md = [f"# Per-model comedy stats — {', '.join(runs)}", "",
          f"Judge: {' + '.join(m.split('/')[-1] for m in PANEL)} (non-league), prompt v{PROMPT_V}, every run re-scored "
          f"on the same scale ({time.strftime('%Y-%m-%d %H:%M')}). One row per model; its GM lines = pick calls, "
          f"comebacks, reactions, table talk. **All green** is what the complete show airs (every sentence makes "
          f"sense, lands instantly, gets tonight right, no fact wrong, not mean; a catchphrase sentence is cut "
          f"first); **shorts** = the line has a joke good enough for a short (green and funny ≥ 2); **funny** = the "
          f"line's best sentence, 0–3; **mean / slop / filler** = the line has a mean sentence / a stock internet "
          f"phrase / a catchphrase. **On air** = what the complete show actually airs under the current policy (all "
          f"green + a laugh, calls cut sentence by sentence, the aired-context check): calls whose joke airs (not just "
          f"the plain pick sentence), table lines (comebacks / reactions / table talk) that air, and aired sentences "
          f"that score a laugh (funny ≥ 2). Sorted by laughs on air per line written (then all green).", "",
          "| model | lines | all green | shorts | funny | instant | consistent | mean | slop | filler | calls green | "
          + ("on air: calls with a joke | on air: table lines | on air: laughs | " if air else "")
          + " | ".join(f"audit {r}" for r in audits) + (" |" if audits else "|"),
          "|---|---|---|---|---|---|---|---|---|---|---|" + ("---|---|---|" if air else "") + "---|" * len(audits)]
    for k in order:
        a = per[k]
        t = teams[k]
        flag = " ⬇" if a["lines"] and a["green"] / a["lines"] < med - 0.15 else ""
        if a["lines"] < 8:
            flag += " (few lines)"
        md.append(f"| **{t.model_label}**{flag} | {a['lines']} | {_pct(a['green'], a['lines'])} | "
                  f"{_pct(a['shorts'], a['lines'])} | {a['funny']:.2f} | {_pct(a['instant'], a['lines'])} | "
                  f"{_pct(a['consistent'], a['lines'])} | {_pct(a['mean'], a['lines'])} | {_pct(a['slop'], a['lines'])} | "
                  f"{_pct(a['filler'], a['lines'])} | {a['calls_green']}/{a['calls']} | "
                  + (f"{air.get(k, {}).get('calls_jokes', 0)}/{air.get(k, {}).get('calls', 0)} | "
                     f"{air.get(k, {}).get('says_aired', 0)}/{air.get(k, {}).get('says', 0)} | "
                     f"{air.get(k, {}).get('laughs', 0)} | " if air else "")
                  + " | ".join(audits[r].get(k, "") for r in audits) + (" |" if audits else ""))
    md.append(f"| *all models* | {allm['lines']} | {_pct(allm['green'], allm['lines'])} | "
              f"{_pct(allm['shorts'], allm['lines'])} | {allm['funny']:.2f} | {_pct(allm['instant'], allm['lines'])} | "
              f"{_pct(allm['consistent'], allm['lines'])} | {_pct(allm['mean'], allm['lines'])} | "
              f"{_pct(allm['slop'], allm['lines'])} | {_pct(allm['filler'], allm['lines'])} | "
              f"{allm['calls_green']}/{allm['calls']} |"
              + (f" {sum(a['calls_jokes'] for a in air.values())}/{sum(a['calls'] for a in air.values())} | "
                 f"{sum(a['says_aired'] for a in air.values())}/{sum(a['says'] for a in air.values())} | "
                 f"{sum(a['laughs'] for a in air.values())} |" if air else "") + " |" * len(audits))
    ns = sorted(per[k]["lines"] for k in per if per[k]["lines"])
    md += ["", (f"⬇ = all-green rate more than 15 points under the median ({100 * med:.0f}%). Samples are small "
                f"({ns[0]}–{ns[-1]} lines per model): one line moves a model's rate by {100 / ns[-1]:.0f}–"
                f"{100 / ns[0]:.0f} points, so treat gaps under ~20 points as noise; the per-run table below shows "
                f"whether a model is consistently low.") if ns else "", ""]

    # the trend: all green per run (a model that improved on the new prompts shows it here)
    md += ["## All green, per run", "", "| model | " + " | ".join(runs) + " |", "|---|" + "---|" * len(runs)]
    for k in order:
        cells = []
        for run in runs:
            rr = [r for r in rows if r["team"] == k and r["run"] == run]
            cells.append(f"{_pct(sum(r['green'] for r in rr), len(rr))} ({len(rr)})" if rr else "-")
        md.append(f"| {teams[k].model_label} | " + " | ".join(cells) + " |")
    md.append("")

    # why lines fail, per model: the first check each non-green line fails
    md += ["## Why lines aren't green (first failing check)", "",
           "| model | doesn't make sense | doesn't land instantly | gets tonight wrong | fact wrong | mean | catchphrase |",
           "|---|---|---|---|---|---|---|"]
    for k in order:
        rr = [r for r in rows if r["team"] == k and not r["green"]]
        cnt = {w: 0 for w in ("doesn't make sense", "doesn't land instantly", "gets tonight wrong", "a fact is wrong",
                              "mean", "catchphrase")}
        for r in rr:
            w = first_fail(r)
            if w:
                cnt[w] += 1
        md.append(f"| {teams[k].model_label} | " + " | ".join(str(v) for v in cnt.values()) + " |")
    md += ["", "## What cutting a model does", "",
           "`--cut-models x,y` (rundown / all / factory / short): those models' lines never air anywhere; the host "
           "announces their picks, and a comeback that answered one of their calls goes with it. The per-run judge "
           "logs (every sentence, both judges' reasons) are in `media/out/review/<run>/judge.md`.", "",
           f"Judged: " + ", ".join(f"{x['run']} ({x['lines']} lines" + (f", {x['unjudged']} sentences unjudged"
                                                                      if x['unjudged'] else "") + ")"
                                    for x in judged_runs)
           + f". New judge calls for this report: {calls} (${usd:.2f}); everything else came from the judge's cache.",
           ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md))
    data = {"runs": runs, "panel": PANEL, "prompt_v": PROMPT_V, "per_model": per, "all": allm, "audits": audits,
            "on_air": air,
            "rows": rows, "spend_usd": round(usd, 4)}
    out.with_suffix(".json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    log(f"modelstats: {len(rows)} lines, {len(per)} models -> {out} (judge spend ${usd:.2f}, {calls} calls)")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gmbench_media.modelstats")
    ap.add_argument("--runs", required=True, help="comma-separated runs, e.g. party-2,party-3,party-8")
    ap.add_argument("--out", default=None, help="markdown path (default media/out/review/model-comedy.md)")
    ap.add_argument("--cached", action="store_true", help="score only cached verdicts; ask the panel nothing")
    ap.add_argument("--no-aired", action="store_true", help="skip the on-air columns (the complete show's air plan)")
    a = ap.parse_args(argv)
    runs = [x.strip() for x in a.runs.split(",") if x.strip()]
    out = Path(a.out) if a.out else OUT / "review" / "model-comedy.md"
    run_stats(runs, out, use_llm=not a.cached, aired=not a.no_aired)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
