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


def gm_call(L, p) -> bool:
    t = L.teams.get(p.team)
    return bool(t and not t.is_bot and not p.auto and (p.on_air_call or "").strip() and getattr(p, "airable", True))


def says_for(L, p) -> list:
    """The lines a call drew: comebacks, reactions and the table talk right after it (never the caller's own)."""
    return sorted([s for s in L.says if L.pick_for_say(s) == p.pick_no and s.team != p.team
                   and getattr(s, "airable", True)], key=lambda s: s.seq)


def candidate_specs(L) -> list[dict]:
    picks = sorted(L.picks, key=lambda p: p.pick_no)
    by_no = {p.pick_no: p for p in picks}
    specs = []
    for p in picks:
        if gm_call(L, p) and says_for(L, p):
            specs.append({"key": f"x{p.pick_no}", "kind": "exchange", "picks": [p.pick_no],
                          "says": [s.seq for s in says_for(L, p)]})
    for n in (2, 3):
        for p in picks:
            win = [by_no.get(p.pick_no + k) for k in range(n)]
            if any(w is None for w in win) or not gm_call(L, win[0]) or not gm_call(L, win[-1]):
                continue
            if sum(1 for w in win if gm_call(L, w)) < 2:
                continue
            specs.append({"key": f"c{win[0].pick_no}-{win[-1].pick_no}", "kind": "chain",
                          "picks": [w.pick_no for w in win], "says": [s.seq for w in win for s in says_for(L, w)]})
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
                          "tag": bool(lab.get("tag"))})
            if lab["role"] in ("PUNCH", "BUTTON") and not lab.get("tag") and ln["kind"] != "hook":
                punches.append({"sid": jv.get("sid"), "funny": float(jv.get("funny") or 0.0), "pass": jv.get("pass", True),
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
    cut = [{"id": t["id"], "text": t.get("removed"), "reason": t.get("reason")} for t in rd.get("trims") or []
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
    return {"rows": rows, "punches": punches, "hook": hook, "opens_hot": hot,
            "est_s": float(sh.get("est_s") or 0.0), "chars": chars,
            "lines_used": sh.get("lines_used") or [], "judge_cuts": cut,
            "comebacks": sum(1 for r in rows if r["kind"] in ("comeback", "table_talk"))}


def score(L, s: dict) -> float:
    ok = [p for p in s["punches"] if p["pass"]]
    laughs = sum(max(0.0, p["funny"] - 1.5) for p in ok)
    labs = {L.teams[r["speaker"]].lab for r in s["rows"] if r["speaker"] in L.teams}
    dens = len(ok) / max(20.0, s["est_s"]) * 30.0
    return round(2.0 * laughs + 0.6 * len(ok) + 0.8 * s["comebacks"] + 0.6 * dens + 0.3 * (len(labs) - 1), 2)


def select(cands: list[dict], n_max: int = 15) -> list[dict]:
    """The best first; no two shorts open on the same punchline; a short may share at most half its lines."""
    chosen: list[dict] = []
    hooks: set = set()
    use: Counter = Counter()
    for c in sorted(cands, key=lambda c: -c["score"]):
        h = (c["hook"] or {}).get("sid") or (c["hook"] or {}).get("text")
        if h in hooks:
            continue
        lines = c["lines_used"]
        if lines and sum(1 for x in lines if use[x]) > len(lines) / 2:
            continue
        chosen.append(c)
        hooks.add(h)
        use.update(lines)
        if len(chosen) >= n_max:
            break
    return chosen


def _gm(L, tid: str | None) -> str:
    if tid == "host":
        return "The Commissioner"
    t = L.teams.get(tid or "")
    if not t:
        return tid or "?"
    return "The robot" if t.is_bot else (t.gm_name or t.display)


def _model(L, tid: str | None) -> str:
    if tid == "host":
        return "host"
    t = L.teams.get(tid or "")
    return "control bot" if (t and t.is_bot) else (t.display if t else "?")


def write_shortlist(L, run: str, chosen: list[dict], extras: list[dict], meta: dict) -> Path:
    d = review_dir(run)
    appr = d / "approve.txt"
    if not appr.exists():
        appr.write_text("# Shorts to render: one id per line (S01, R1, D1 ...). Nothing is voiced until an id is here.\n")
    total = sum(c["chars"] for c in chosen + extras)
    out = [f"# Shorts shortlist — {run}", "",
           f"{len(chosen)} draft shorts + {len(extras)} post-draft shorts, built {time.strftime('%Y-%m-%d %H:%M')} from "
           f"{meta['lines']} GM lines. Comedy judge: {' + '.join(m.split('/')[-1] for m in meta['panel'])} "
           f"(non-league; 86% agreement on the calibration set). Gate: makes sense (the stricter judge), funny >= 2, "
           f"facts not off, no veto, the ledger's fact_check ok. Nothing below has been voiced yet."
           + (f" **{meta['unjudged']} sentences are unjudged (judge outage?) and cannot air: re-run `factory`.**"
              if meta.get("unjudged") else ""), "",
           "**Approve in two minutes:**", "",
           f"1. Put the ids to render in `{rel(appr)}` (one per line).",
           f"2. Veto a line: its sentence id (e.g. `72.0`) or a quoted fragment in `{rel(d / 'veto.txt')}`; re-run "
           f"`factory` to see the shortlist without it.",
           f"3. `python -m gmbench_media.cli factory --run {run} --render --engine elevenlabs --plan` (characters), then "
           f"the same with `--max-chars N`. Only the approved shorts' sentences are voiced (~{total} characters if all "
           f"are approved).", "",
           "| id | kind | picks | length | EL chars | laughs (judge) | hook |", "|---|---|---|---|---|---|---|"]
    for c in chosen + extras:
        laughs = " ".join(f"{p['funny']:g}" for p in c.get("punches", []) if p.get("pass"))
        hook = (c.get("hook") or {}).get("text") or c.get("title", "")
        out.append(f"| **{c['id']}** | {c['kind']} | {c.get('picks_label', '')} | {c['est_s']:.0f} s | {c['chars']} | "
                   f"{laughs} | {hook[:90].replace('|', '/')} |")
    out.append("")
    for c in chosen + extras:
        out.append(f"## {c['id']} · {c['kind']} · {c.get('picks_label', '')} · {c['est_s']:.0f} s · ~{c['chars']} chars"
                   f" · score {c.get('score', 0):g}")
        if c.get("hook"):
            h = c["hook"]
            out.append(f"**Cold open** — {_gm(L, h['speaker'])} ({_model(L, h['speaker'])}): “{h['text']}”"
                       + (f" `{h['sid']}` funny {h['funny']:g}" if h.get("sid") and h.get("funny") is not None else ""))
        for k_, r in enumerate(c.get("rows", [])):
            if r["kind"] == "hook" or (k_ == 0 and c.get("hook") and r["sentences"]
                                       and r["sentences"][0]["text"] == c["hook"]["text"]):
                continue
            bits = []
            for s in r["sentences"]:
                mark = ""
                if s["role"] in ("PUNCH", "BUTTON") and not s["tag"] and s.get("funny") is not None:
                    mark = f" `{s['sid']}` {s['funny']:g}"
                t_ = f"**{s['text']}**" if s["role"] in ("PUNCH", "BUTTON") else s["text"]
                bits.append(t_ + mark)
            to = f" → {_gm(L, r['to'])}" if r.get("to") and r["to"] != r["speaker"] else ""
            out.append(f"- {_gm(L, r['speaker'])} ({_model(L, r['speaker'])}){to}: " + " ".join(bits))
        for x in c.get("judge_cuts", [])[:6]:
            out.append(f"  - cut: “{(x['text'] or '')[:100]}” — {x['reason']}")
        if c.get("lines_used"):
            out.append(f"  - ledger lines: {', '.join(str(x) for x in c['lines_used'])}")
        out.append("")
    p = d / "shortlist.md"
    p.write_text("\n".join(out))
    return p


def run_factory(L, run: str, cfg: dict, log=print, n_max: int = 15, tempo: float = 1.12, llm: bool = True) -> dict:
    from .beatmap import label_lines
    from .script import ShortBuilder
    t0 = time.time()
    specs = candidate_specs(L)
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
        s.update({"key": c["key"], "kind": c["kind"], "picks": c["picks"], "says": c["says"],
                  "picks_label": (f"pick {c['picks'][0]}" if len(c["picks"]) == 1 else
                                  f"picks {c['picks'][0]}–{c['picks'][-1]}"),
                  "hook_seq": (rd["short"] or {}).get("hook")})
        s["score"] = score(L, s)
        cands.append(s)
    chosen = select(cands, n_max)
    for k, c in enumerate(chosen):
        c["id"] = f"S{k + 1:02d}"
    extras = post_draft_candidates(L, cfg, log, J)
    meta = {"lines": len(J.items), "panel": J.models, "unjudged": J.unjudged}
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
            rows.append({"id": ln["id"], "speaker": ln["speaker"], "kind": ln["kind"], "to": ln.get("addressed_to"),
                         "refs": ln.get("refs"), "sentences": [{"i": 0, "role": "LINE", "text": ln["display_text"],
                                                               "sid": None, "funny": None, "tag": False}]})
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
        return [x.strip().upper() for x in cli_ids.split(",") if x.strip()]
    p = review_dir(run) / "approve.txt"
    if not p.exists():
        return []
    return [re.sub(r"\s.*", "", x.strip()).upper() for x in p.read_text().splitlines()
            if x.strip() and not x.strip().startswith("#")]


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
        hits = sorted(out_dir(f"{run}-shorts").glob(f"{cid}-*/short-{cid}-*.json"))
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
