"""Shorts factory -- Monday's deliverable: the funniest moments of the draft as reels / TikToks.

From the judged beat maps, auto-assemble 10-15 candidate shorts of 20-45 s:
  EXCHANGE  one call and the comeback / table talk it drew;
  CHAIN     2-3 consecutive picks with their comebacks;
each hook-first, with the context strip, big model names, comic-timing beats, kicker pops, the suit card and the end
card (the tight-short layout). Plus the Report Card roast short and the Delusion Index short once the post-draft
grades are in.

Nothing is voiced before the owner approves. The shortlist (media/out/review/<run>/shortlist.md) shows every
candidate's transcript, judge scores, length, the ledger lines it uses and its ElevenLabs characters. The owner puts
the ids to render in approve.txt (and any line vetoes in veto.txt); `--render` then voices ONLY the sentences of the
approved shorts (a cold open is cut from its body line's take, so no sentence is paid twice).

  python -m gmbench_media.cli factory --run live                                  # label + judge -> shortlist
  python -m gmbench_media.cli factory --run live --render --engine elevenlabs --plan       # characters to voice
  python -m gmbench_media.cli factory --run live --render --engine elevenlabs --max-chars N
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from . import tags as T
from .judge import PANEL, judge_run, review_dir
from .paths import ROOT

SHORT_MIN_S, SHORT_MAX_S = 20.0, 45.0


def rel(p: Path) -> str:
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def gm_call(L, p, cut: set[str] | frozenset = frozenset()) -> bool:
    t = L.teams.get(p.team)
    return bool(t and not t.is_bot and not p.auto and (p.on_air_call or "").strip() and getattr(p, "airable", True)
                and p.team not in cut)


def says_for(L, p, cut: set[str] | frozenset = frozenset()) -> list:
    """The lines a call drew: comebacks, reactions and the table talk right after it (never the caller's own, never a
    cut model's)."""
    return sorted([s for s in L.says if L.pick_for_say(s) == p.pick_no and s.team != p.team
                   and getattr(s, "airable", True) and s.team not in cut], key=lambda s: s.seq)


def candidate_specs(L, cut: set[str] | frozenset = frozenset()) -> list[dict]:
    """Candidate windows. A cut model (--cut-models) never speaks: its picks are context only (the host calls them)."""
    picks = sorted(L.picks, key=lambda p: p.pick_no)
    by_no = {p.pick_no: p for p in picks}
    specs = []
    for p in picks:
        if gm_call(L, p, cut) and says_for(L, p, cut):
            specs.append({"key": f"x{p.pick_no}", "kind": "exchange", "picks": [p.pick_no],
                          "says": [s.seq for s in says_for(L, p, cut)]})
    for n in (2, 3):
        for p in picks:
            win = [by_no.get(p.pick_no + k) for k in range(n)]
            if any(w is None for w in win) or not gm_call(L, win[0], cut) or not gm_call(L, win[-1], cut):
                continue
            if sum(1 for w in win if gm_call(L, w, cut)) < 2:
                continue
            specs.append({"key": f"c{win[0].pick_no}-{win[-1].pick_no}", "kind": "chain",
                          "picks": [w.pick_no for w in win], "says": [s.seq for w in win for s in says_for(L, w, cut)]})
    return specs


def _spec(c: dict, cfg: dict, J, tempo: float) -> dict:
    return {"name": c.get("id") or c["key"], "picks": c["picks"], "says": c["says"], "tight": True, "judge": True,
            "judgement": J, "tempo": tempo, "end_s": float(cfg["mix"]["postroll_s"]),
            "target_s": 43.0 if c["kind"] == "chain" else 38.0, "hook": c.get("hook")}


def build(L, cfg: dict, c: dict, J, tempo: float) -> dict:
    from .script import ShortBuilder
    return ShortBuilder(L, cfg).build_short(_spec(c, cfg, J, tempo))


def summarize(L, rd: dict) -> dict:
    """What a candidate airs: transcript rows, its punches with the judge's scores, length, characters, lines."""
    rows, punches = [], []
    hook = None
    chars = 0
    body_keep: dict[tuple, list[int]] = {}
    for ln in rd["timeline"]:
        b = ln.get("beats")
        if not b:
            continue
        if ln["kind"] != "hook":
            body_keep[(ln["speaker"], tuple(ln.get("refs") or []))] = list(b["keep"])
    for ln in rd["timeline"]:
        b = ln.get("beats")
        if not b:
            continue
        sents = []
        for i in b["keep"]:
            lab = b["labels"][i]
            jv = lab.get("judge") or {}
            sents.append({"i": i, "role": lab["role"], "text": T.strip(b["parts"][i]), "sid": jv.get("sid"),
                          "funny": jv.get("funny"), "sense": jv.get("makes_sense"), "facts": jv.get("factually_ok"),
                          "tone": jv.get("tone"), "tag": bool(lab.get("tag"))})
            if lab["role"] in ("PUNCH", "BUTTON") and not lab.get("tag") and ln["kind"] != "hook":
                punches.append({"sid": jv.get("sid"), "funny": float(jv.get("funny") or 0.0), "pass": jv.get("pass", True),
                                "tone": jv.get("tone") or "friendly",
                                "role": lab["role"], "speaker": ln["speaker"], "text": T.strip(b["parts"][i])})
        text = " ".join(b["parts"][i] for i in b["keep"])
        if ln["kind"] == "hook":
            hook = {"speaker": ln["speaker"], "text": T.strip(text), "sid": (sents[0] if sents else {}).get("sid"),
                    "funny": (sents[0] if sents else {}).get("funny")}
            bk = body_keep.get((ln["speaker"], tuple(ln.get("refs") or [])), [])
            if not set(b["keep"]) <= set(bk):
                chars += len(text)  # a hook that is not inside its body's take is voiced on its own
        else:
            chars += len(text)
        rows.append({"id": ln["id"], "speaker": ln["speaker"], "kind": ln["kind"], "to": ln.get("addressed_to"),
                     "refs": ln.get("refs"), "sentences": sents})
    cut = [{"id": t["id"], "text": t.get("removed"), "reason": t.get("reason"), "sid": t.get("sid")}
           for t in rd.get("trims") or []
           if str(t.get("reason") or "").startswith(("judge", "owner", "setup of a cut", "tag of a cut"))]
    sh = rd.get("short") or {}
    # no cold open needed when the first line lands its joke in the first few seconds
    hot = False
    body = next((r for r in rows if r["kind"] != "hook"), None)
    if body is not None:
        t = 0.35
        for s_ in body["sentences"]:
            t += len(s_["text"]) / 13.5 + 0.2
            if s_["role"] in ("PUNCH", "BUTTON") and s_.get("funny") is not None and float(s_["funny"]) >= 2:
                hot = t <= 6.0
                break
    # the longest stretch without a laugh (the QA gate fails a short over 13 s): estimated on the delivery
    t, last, worst = 0.35, 0.0, 0.0  # from one laugh to the next joke's first word (a long joke is not dry)
    for r in rows:
        for s_ in r["sentences"]:
            d = len(s_["text"]) / 14.0 + 0.25
            if s_["role"] in ("PUNCH", "BUTTON") and not s_["tag"] and s_.get("funny") is not None \
                    and float(s_["funny"]) >= 2:
                worst = max(worst, t - last)
                t += d
                last = t
                t += 0.45
            else:
                t += d
        t += 0.3
    worst = max(worst, t - last)
    return {"rows": rows, "punches": punches, "hook": hook, "opens_hot": hot, "dry_s": round(worst, 1),
            "est_s": float(sh.get("est_s") or 0.0), "chars": chars,
            "lines_used": sh.get("lines_used") or [], "judge_cuts": cut,
            "comebacks": sum(1 for r in rows if r["kind"] in ("comeback", "table_talk"))}


def score(L, s: dict) -> float:
    ok = [p for p in s["punches"] if p["pass"]]
    tone_adj = {"friendly": 0.25, "edgy": -0.25, "mean": -2.0}
    laughs = sum(max(0.0, p["funny"] + tone_adj.get(p.get("tone"), 0.0) - 1.5) for p in ok)
    labs = {L.teams[r["speaker"]].lab for r in s["rows"] if r["speaker"] in L.teams}
    dens = len(ok) / max(20.0, s["est_s"]) * 30.0
    dry = max(0.0, float(s.get("dry_s") or 0.0) - 10.0)  # a long stretch without a laugh costs
    last = next((x for r in reversed(s["rows"]) if r["kind"] != "hook" for x in reversed(r["sentences"])), None)
    ends_flat = 1.0 if last and last["role"] not in ("PUNCH", "BUTTON") else 0.0  # ends on a pick, not a laugh
    return round(2.0 * laughs + 0.6 * len(ok) + 0.8 * s["comebacks"] + 0.6 * dens + 0.3 * (len(labs) - 1)
                 - 0.8 * dry - ends_flat, 2)


def select(cands: list[dict], n_max: int = 15) -> list[dict]:
    """The best first; no two shorts open on the same punchline; a short may share at most 60% of its lines."""
    chosen: list[dict] = []
    hooks: set = set()
    use: Counter = Counter()
    for c in sorted(cands, key=lambda c: -c["score"]):
        h = (c["hook"] or {}).get("sid") or (c["hook"] or {}).get("text")
        if h in hooks:
            continue
        lines = c["lines_used"]
        if lines and sum(1 for x in lines if use[x]) > 0.6 * len(lines):
            continue
        chosen.append(c)
        hooks.add(h)
        use.update(lines)
        if len(chosen) >= n_max:
            break
    return chosen


def stable_ids(run: str, chosen: list[dict]) -> None:
    """A short keeps its id (S07) across re-runs -- a veto never renames the others -- so an answer given in chat
    ("render S03 and S07") means the same shorts after the list is rebuilt. media/out/review/<run>/ids.json."""
    p = review_dir(run) / "ids.json"
    ids = json.loads(p.read_text()) if p.exists() else {}
    used = {int(v[1:]) for v in ids.values() if re.fullmatch(r"S\d+", v)}
    for c in chosen:
        if c["key"] not in ids:
            n = max(used, default=0) + 1
            ids[c["key"]] = f"S{n:02d}"
            used.add(n)
        c["id"] = ids[c["key"]]
    p.write_text(json.dumps(ids, indent=1))


def _gm(L, tid: str | None) -> str:
    """Every line is labelled with the MODEL: 'Qwen (Qwen3.8 Max, Alibaba)'."""
    if tid == "host":
        return "The Commissioner (host)"
    t = L.teams.get(tid or "")
    if not t:
        return tid or "?"
    return t.model_label


def _model(L, tid: str | None) -> str:
    if tid == "host":
        return "host"
    t = L.teams.get(tid or "")
    return "control bot" if (t and t.is_bot) else (t.display if t else "?")


def write_shortlist(L, run: str, chosen: list[dict], extras: list[dict], meta: dict) -> Path:
    d = review_dir(run)
    appr = d / "approve.txt"
    if not appr.exists():
        appr.write_text("# Shorts to render: one id per line (S01, R1, D1 ...; 'S3, S07 r1' works too).\n"
                        "# Nothing is voiced until an id is here.\n")
    total = sum(c["chars"] for c in chosen + extras)
    ex_id = (chosen + extras)[0]["id"] if (chosen + extras) else "S01"
    ex_sid = next((s_["sid"] for c in chosen for r in c.get("rows") or [] for s_ in r["sentences"] if s_.get("sid")),
                  "72.0")
    out = [f"# Shorts shortlist — {run}", "",
           "## How to answer (reply in chat — the files are written for you)", "",
           "| you say | means | goes in |", "|---|---|---|",
           f"| `{ex_id} S0x R1` | render these shorts (ids never change between re-runs) | `approve.txt`: one id per line |",
           f"| `veto {ex_sid}` | never air that sentence (its id is printed next to it below) | `veto.txt`: `{ex_sid}` |",
           f"| `veto {ex_sid.split('.')[0]}` | never air that whole line (the ledger line number) | "
           f"`veto.txt`: `{ex_sid.split('.')[0]}` |",
           "| `veto \"outhouse\"` | never air a sentence containing those words | `veto.txt`: `\"outhouse\"` |",
           f"| `keep {ex_sid}` | air a sentence the judges cut | `veto.txt`: `+{ex_sid}` |", "",
           f"Files: `{rel(appr)}` and `{rel(d / 'veto.txt')}` (one entry per line, `#` starts a comment). After a veto, "
           f"re-run `factory --run {run}` to see the new list; then `factory --run {run} --render --engine elevenlabs "
           f"--plan` (characters) and the same with `--max-chars N`. Nothing below has been voiced yet; only approved "
           f"shorts are (~{total} characters if all are approved; 0 when the complete show's takes exist).", "",
           f"{len(chosen)} draft shorts + {len(extras)} post-draft shorts, built {time.strftime('%Y-%m-%d %H:%M')} from "
           f"{meta['lines']} GM lines. Comedy judge: {' + '.join(m.split('/')[-1] for m in meta['panel'])} "
           f"(non-league). A joke airs if it makes sense, lands instantly and gets tonight right (board + who said "
           f"what; the stricter judge), scores funny >= 2, isn't mean or catchphrase filler, has no fact wrong, isn't "
           f"vetoed and passed the ledger's fact_check; friendly ribbing ranks first."
           + (f" Cut models (never air; the host calls their picks): {', '.join(meta['cut'])}." if meta.get("cut") else "")
           + (f" **{meta['unjudged']} sentences are unjudged (judge outage?) and cannot air: re-run `factory`.**"
              if meta.get("unjudged") else ""), "",
           "| id | kind | picks | length | EL chars | laughs (judge) | longest dry | hook |",
           "|---|---|---|---|---|---|---|---|"]
    for c in chosen + extras:
        laughs = " ".join(f"{p['funny']:g}" for p in c.get("punches", []) if p.get("pass"))
        hook = (c.get("hook") or {}).get("text") or c.get("title", "")
        out.append(f"| **{c['id']}** | {c['kind']} | {c.get('picks_label', '')} | {c['est_s']:.0f} s | {c['chars']} | "
                   f"{laughs} | {c.get('dry_s', 0):.0f} s | {hook[:90].replace('|', '/')} |")
    out.append("")
    for c in chosen + extras:
        out.append(f"## {c['id']} · {c['kind']} · {c.get('picks_label', '')} · {c['est_s']:.0f} s · ~{c['chars']} chars"
                   f" · score {c.get('score', 0):g}")
        if c.get("hook"):
            h = c["hook"]
            out.append(f"**Cold open** — {_gm(L, h['speaker'])}: “{h['text']}”"
                       + (f" `{h['sid']}` funny {h['funny']:g}" if h.get("sid") and h.get("funny") is not None else ""))
        for k_, r in enumerate(c.get("rows", [])):
            if r["kind"] == "hook" or (k_ == 0 and c.get("hook") and r["sentences"]
                                       and r["sentences"][0]["text"] == c["hook"]["text"]):
                continue
            bits = []
            for s in r["sentences"]:
                joke = s["role"] in ("PUNCH", "BUTTON") and not s["tag"] and s.get("funny") is not None
                mark = (f" `{s['sid']} · funny {s['funny']:g}{' · ' + s['tone'] if s.get('tone') and s['tone'] != 'friendly' else ''}`" if joke else
                        (f" `{s['sid']}`" if s.get("sid") else ""))
                t_ = f"**{s['text']}**" if s["role"] in ("PUNCH", "BUTTON") else s["text"]
                bits.append(t_ + mark)
            to = f" → {_gm(L, r['to'])}" if r.get("to") and r["to"] != r["speaker"] else ""
            out.append(f"- {_gm(L, r['speaker'])}{to}: " + " ".join(bits))
        for x in c.get("judge_cuts", [])[:8]:
            out.append(f"  - cut: “{(x['text'] or '')[:100]}” — {x['reason']}" + (f" `{x['sid']}`" if x.get("sid") else ""))
        if c.get("lines_used"):
            out.append(f"  - ledger lines: {', '.join(str(x) for x in c['lines_used'])}")
        out.append("")
    p = d / "shortlist.md"
    p.write_text("\n".join(out))
    return p


def run_factory(L, run: str, cfg: dict, log=print, n_max: int = 15, tempo: float = 1.12, llm: bool = True) -> dict:
    from .beatmap import label_lines
    from .script import ShortBuilder
    from .script import resolve_models
    t0 = time.time()
    cut = resolve_models(L, cfg["script"].get("cut_models"))
    if cut:
        log(f"factory: cut models (never air): {', '.join(sorted(L.teams[x].call_name for x in cut))}")
    specs = candidate_specs(L, cut)
    # 1) every line once through the labeller (batched), 2) the panel judges every sentence of the draft
    items: list[dict] = []
    for c in specs:
        sp = {**_spec(c, cfg, None, tempo), "judge": False, "collect": items}
        try:
            ShortBuilder(L, cfg).build_short(sp)
        except SystemExit:
            continue
    uniq = list({it["id"] + "|" + it["text"]: it for it in items}.values())
    log(f"factory: {len(specs)} candidate windows, {len(uniq)} lines to label")
    label_lines(uniq, log=log, use_llm=llm)
    J = judge_run(L, log=log, use_llm=llm)
    cands, errors = [], Counter()
    for c in specs:
        try:
            rd = build(L, cfg, c, J, tempo)
        except SystemExit as e:
            errors[str(e)[:60]] += 1
            continue
        s = summarize(L, rd)
        ok = [p for p in s["punches"] if p["pass"]]
        if len(ok) < 2 or not (s["hook"] or s["opens_hot"]) or not (SHORT_MIN_S - 2 <= s["est_s"] <= SHORT_MAX_S + 1):
            continue
        if s["dry_s"] > 13.5:  # a laugh at least every ~10 s (the QA gate fails at 13; this is an estimate)
            continue
        s.update({"key": c["key"], "kind": c["kind"], "picks": c["picks"], "says": c["says"],
                  "picks_label": (f"pick {c['picks'][0]}" if len(c["picks"]) == 1 else
                                  f"picks {c['picks'][0]}–{c['picks'][-1]}"),
                  "hook_seq": (rd["short"] or {}).get("hook")})
        s["score"] = score(L, s)
        cands.append(s)
    chosen = select(cands, n_max)
    stable_ids(run, chosen)
    extras = post_draft_candidates(L, cfg, log, J)
    meta = {"lines": len(J.items), "panel": J.models, "unjudged": J.unjudged,
            "cut": [L.teams[x].model_label for x in sorted(cut)]}
    if J.unjudged:
        log(f"factory: WARNING {J.unjudged} sentences have no verdict (judge outage?): they cannot air as jokes; "
            f"re-run to retry")
    p = write_shortlist(L, run, chosen, extras, meta)
    data = {"run": run, "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "tempo": tempo, "panel": J.models,
            "candidates": [{k: v for k, v in c.items() if k not in ("rows",)} | {"rows": c.get("rows")}
                           for c in chosen + extras]}
    (review_dir(run) / "candidates.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    log(f"factory: {len(cands)} viable of {len(specs)} windows -> {len(chosen)} shorts + {len(extras)} post-draft; "
        f"shortlist -> {rel(p)} ({time.time() - t0:.0f}s)")
    if errors:
        log(f"factory: skipped windows: {dict(errors.most_common(4))}")
    return data


# ------------------------------------------------------------------------------------------ post-draft shorts
def post_draft_candidates(L, cfg: dict, log=print, J=None) -> list[dict]:
    """The Report Card roast short and the Delusion Index short (when the grades and predictions are in)."""
    out = []
    try:
        rc = L.report_card()
    except Exception:  # noqa: BLE001
        rc = None
    if not rc:
        return out
    from .script import build_delusion_rundown, build_roast_rundown
    for cid, kind, fn in (("R1", "roast", lambda: build_roast_rundown(L, cfg, {"name": "roast", "n": 4,
                                                                               "judgement": J})),
                          ("D1", "delusion", lambda: build_delusion_rundown(L, cfg, {"name": "delusion",
                                                                                     "judgement": J}))):
        try:
            rd = fn()
        except SystemExit as e:
            log(f"factory: no {kind} short ({e})")
            continue
        rows = []
        chars = 0
        est = 0.35 + float(cfg["mix"]["postroll_s"])
        for ln in rd["timeline"]:
            gid = (f"{ln['refs'][0]}-{ln['addressed_to']}" if ln["kind"] in ("grade_comment", "hook") and ln.get("refs")
                   and ln.get("addressed_to") and ln["speaker"] != "host" else None)
            rows.append({"id": ln["id"], "speaker": ln["speaker"], "kind": ln["kind"], "to": ln.get("addressed_to"),
                         "refs": ln.get("refs"), "sentences": [{"i": 0, "role": "LINE", "text": ln["display_text"],
                                                               "sid": gid, "funny": None, "tag": False}]})
            chars += len(ln["text"])
            est += float(ln.get("est_s") or 0.0) / 1.1 + 0.4
        out.append({"id": cid, "kind": kind, "rows": rows, "chars": chars, "est_s": round(est, 1), "punches": [],
                    "hook": {"speaker": rd["timeline"][0]["speaker"], "text": rd["timeline"][0]["display_text"]},
                    "picks_label": "post-draft", "lines_used": sorted({r for ln in rd["timeline"] for r in ln["refs"]}),
                    "judge_cuts": [], "score": 0})
    return out


# ------------------------------------------------------------------------------------------ render the approved set
def approved_ids(run: str, cli_ids: str | None) -> list[str]:
    if cli_ids:
        out = []
        for tok in re.findall(r"(?i)\b([SRD])\s*0*(\d{1,3})\b", cli_ids):
            out.append(f"{tok[0].upper()}{int(tok[1]):02d}" if tok[0].upper() == "S" else f"{tok[0].upper()}{int(tok[1])}")
        return out
    p = review_dir(run) / "approve.txt"
    if not p.exists():
        return []
    out: list[str] = []
    for raw in p.read_text().splitlines():
        for tok in re.findall(r"(?i)\b([SRD])\s*0*(\d{1,3})\b", raw.split("#", 1)[0]):
            cid = f"{tok[0].upper()}{int(tok[1]):02d}" if tok[0].upper() == "S" else f"{tok[0].upper()}{int(tok[1])}"
            if cid not in out:
                out.append(cid)
    return out


def rundown_for(L, cfg: dict, c: dict, tempo: float, log=print) -> dict:
    J = judge_run(L, log=log)
    if c["kind"] == "roast":
        from .script import build_roast_rundown
        return build_roast_rundown(L, cfg, {"name": "roast", "n": 4, "judgement": J})
    if c["kind"] == "delusion":
        from .script import build_delusion_rundown
        return build_delusion_rundown(L, cfg, {"name": "delusion", "judgement": J})
    rd = build(L, cfg, {**c, "hook": c.get("hook_seq")}, J, tempo)
    now = summarize(L, rd)
    before = [[s["text"] for s in r["sentences"]] for r in c.get("rows") or []]
    after = [[s["text"] for s in r["sentences"]] for r in now["rows"]]
    if before and before != after:
        log(f"factory: {c['id']}: the edit changed since the shortlist (a veto or a new verdict) -- rendering the "
            f"current, gated version")
    return rd


# ------------------------------------------------------------------------------------------ the 3-minute montage
def montage(run: str, ids: list[str], log=print) -> Path | None:
    """'Draft Night in 3 minutes': the approved shorts, already rendered and voiced, back to back (each cut where its
    end card starts; the last keeps its end card). Nothing new is voiced or rendered."""
    from . import audio
    from .paths import out_dir
    clips = []
    for cid in ids:
        hits = sorted(x for x in out_dir(f"{run}-shorts").glob(f"{cid}-*/short-{cid}-*.json")
                      if not x.name.endswith(".package.json"))
        if not hits:
            log(f"montage: {cid} is not rendered yet; skipped")
            continue
        info = json.loads(hits[-1].read_text())
        mp4 = ROOT / info["mp4"]
        if mp4.exists():
            clips.append((mp4, info.get("end_card_s"), float(info["duration_s"])))
    if len(clips) < 2:
        log("montage: needs at least two rendered shorts")
        return None
    parts, inputs = [], []
    for k, (mp4, end_t, dur) in enumerate(clips):
        inputs += ["-i", str(mp4)]
        t1 = dur if k == len(clips) - 1 or not end_t else float(end_t)
        parts.append(f"[{k}:v]trim=0:{t1:.3f},setpts=PTS-STARTPTS[v{k}];"
                     f"[{k}:a]atrim=0:{t1:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={max(0.0, t1 - 0.12):.3f}:d=0.12[a{k}]")
    chain = "".join(f"[v{k}][a{k}]" for k in range(len(clips)))
    out = out_dir(f"{run}-shorts") / f"montage-{run}.mp4"
    audio.run([audio.FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex",
               ";".join(parts) + f";{chain}concat=n={len(clips)}:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]",
               "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(out)])
    log(f"montage: {len(clips)} shorts -> {rel(out)} ({audio.duration_s(out):.0f}s)")
    return out
