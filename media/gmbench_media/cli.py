"""gmbench_media CLI (run from media/: `uv run python -m gmbench_media.cli <command> ...`).

  Show (profiles: full | r1high = Round-1 highlight cut | r1clip | acceptance):
    rundown --run <run> [--profile P] [--target-minutes 45] [--polish]
    tts     --run <run> [--profile P] --engine elevenlabs --fallback say --max-chars N   (or --engine say: free)
    mix     --run <run> [--profile P] [--music extra_bed.wav]
    avatars --run <run> [--profile P]
    render  --run <run> [--profile P] [--minutes N] [--jobs 3]
    all     --run <run> [--profile P] --engine elevenlabs --fallback say --max-chars N [--jobs 3]
  Report Card preview (the full-show closer on its own; needs runs/<run>/exports/grades.json):
    all --run pd-real --profile report --engine elevenlabs --fallback say --max-chars N
  Draft-party format (default, script.format: party): host cue ("Ponoka, you're up!") -> the GM's own two-beat call
  (beat 1 reacts to the previous pick, with that GM in the reaction cam; beat 2 is the pick, slammed on the name).
    all --run <run> --profile highlights ...   # R1 highlights = the best chains of consecutive picks
  Vertical shorts (hook -> chain or moment -> end card with the suit CTA; full audio + motion pipeline):
    short --run <run> --name chain --engine elevenlabs --fallback say --max-chars N        # best 3-pick chain
    short --run <run> --name p5-7 --picks 5,6,7 [--hook say-<seq>] ...                     # a chosen chain
    roast --run <run> --name roast --n 7 --engine elevenlabs --fallback say --max-chars N   (the AIs roast each other)
  Add --plan to tts / short / roast to print the billable characters and stop (no synthesis).
  QA (the flip-book critique, automated; runs after render / all / short / roast unless --no-qa):
    qa --run <run> [--profile P] [--name N] [--clip party-2-shorts/chain-b ...] [--names] [--no-whisper] [--strict]
  Names + dropped phrases (Whisper small.en, local): after tts + mix
    uv run --with faster-whisper python -m gmbench_media.pronounce check --run <run> [--profile P | --short NAME]
    uv run python -m gmbench_media.pronounce seed --run <run>        # one cheap LLM call -> candidate respellings
    uv run --with faster-whisper python -m gmbench_media.pronounce fix --run <run> [--profile P] --max-chars N
    (confirmed respellings are applied to TTS input only; re-run tts + mix + render afterwards)

Outputs: media/out/<run>[-<profile>]/ and media/out/<run>-shorts/<name>/.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import avatars as av
from .config import load_config
from .director import direct, polish_host
from .ledger import load_league
from .paths import out_dir, rel
from .script import build_rundown, save_rundown, verify_own_words


def log(msg: str) -> None:
    print(msg, flush=True)


def odir_for(args) -> Path:
    name = args.run if args.profile == "full" else f"{args.run}-{args.profile}"
    if getattr(args, "name", None) and args.command not in ("short", "roast"):
        return out_dir(name, args.name)  # e.g. out/party-2-highlights/chain-a/
    return out_dir(name)


def parse_chains(spec: str) -> list[list[int]]:
    """'1-3;7-11' or '1,2,3;7,8' -> [[1, 2, 3], [7, 8, 9, 10, 11]]"""
    out = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if "-" in part and "," not in part:
            a, b = part.split("-", 1)
            out.append(list(range(int(a), int(b) + 1)))
        else:
            out.append([int(x) for x in part.split(",") if x.strip()])
    return out


def load_json(p: Path) -> dict:
    if not p.exists():
        raise SystemExit(f"missing {rel(p)}; run the earlier pipeline step first")
    return json.loads(p.read_text())


def cmd_rundown(args, cfg) -> dict:
    L = load_league(args.run)
    rd = build_rundown(L, cfg)
    rd = polish_host(rd, cfg, L)
    rd = direct(rd, cfg, L)
    bad = verify_own_words(rd, L)
    if bad:
        raise SystemExit(f"own-words guardrail failed for lines: {bad}")
    from .tts import load_voice_book
    load_voice_book(L)  # creates media/voices_dev.json (dev `say` voices) on first run
    od = odir_for(args)
    save_rundown(rd, od / "rundown.json")
    kinds: dict[str, int] = {}
    for ln in rd["timeline"]:
        kinds[ln["kind"]] = kinds.get(ln["kind"], 0) + 1
    log(f"rundown: {len(rd['timeline'])} lines in {len(rd['segments'])} segments; est {rd['estimated_duration_s'] / 60:.1f} "
        f"min (target {rd['target_duration_s'] / 60:.0f}); fit level {rd['fit_level']}; trims {len(rd['trims'])}; "
        f"dropped {len(rd['dropped'])} -> {rel(od / 'rundown.json')}")
    log("rundown: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    from .packaging import write_transcript
    log(f"rundown: transcript (model-labelled; what doesn't air and why) -> {rel(write_transcript(rd, L, od))}")
    ag = rd.get("all_green")
    if ag:
        log(f"rundown: ALL GREEN{' + a laugh' if ag.get('require_laugh') else ''}: {ag['calls_aired']}/{ag['calls']} "
            f"GM calls air, {ag.get('calls_trimmed', 0)} trimmed, {len(ag.get('calls_pick_only') or [])} down to the "
            f"plain pick sentence (host announces picks "
            f"{', '.join(str(x) for x in ag['calls_to_host']) or '-'}); {ag['says_aired']}/{ag['says_total']} table "
            f"lines air" + (f"; cut: {', '.join(ag['cut'])}" if ag.get("cut") else ""))
    if cfg["script"].get("complete"):
        log_projection(rd, L, cfg)
    return rd


def log_projection(rd: dict, L, cfg: dict) -> None:
    from .script import project_complete
    if rd.get("segment_demo"):  # one stretch of the show: nothing to project
        return
    pr = project_complete(rd, L, cfg)
    tgt = pr.get("target_chars") or 0
    head = (f"complete show: {pr['rounds']} of {pr['of']} rounds on the ledger -> PROJECTED for the full draft"
            if pr.get("projected") and pr["rounds"] < pr["of"] else "complete show")
    log(f"{head}: ~{pr['min']:.0f} min, ~{pr['chars'] / 1000:.1f}k ElevenLabs characters"
        + (f" (target ~{tgt / 1000:.0f}k)" if tgt else "")
        + (f"; {'; '.join(pr['assumed'])}" if pr.get("assumed") else ""))
    if pr.get("per_round"):
        q = pr["per_round"]
        log(f"complete show: a round with every comeback ~{q['full_min']:.1f} min / {q['full_chars']} chars "
            f"({q['comebacks_per_round_seen']} comebacks seen per round); a capped round ~{q['capped_min']:.1f} min / "
            f"{q['capped_chars']} chars")


def cmd_tts(args, cfg) -> dict:
    from .tts import run_tts
    od = odir_for(args)
    rd = load_json(od / "rundown.json")
    L = load_league(args.run)
    only = set(args.only.split(",")) if args.only else None
    fallback = None if args.fallback in ("", "none") else args.fallback
    man = run_tts(rd, cfg, args.engine, L, only=only, max_chars=args.max_chars, fallback=fallback, log=log,
                  plan=args.plan, cache_only=args.cache_only)
    if man.get("plan"):
        if cfg["script"].get("complete"):
            log_projection(rd, L, cfg)
        return man
    name = "tts_manifest.json" if not only else f"tts_manifest.{args.engine}.partial.json"
    (od / name).write_text(json.dumps(man, indent=1))
    log(f"tts: {len(man['lines'])} lines, {man['speech_s'] / 60:.1f} min of speech -> {rel(od / name)}")
    return man


def cmd_mix(args, cfg) -> dict:
    from .mix import run_mix
    od = odir_for(args)
    rd = load_json(od / "rundown.json")
    if args.align:  # dev voices: word timing from Whisper (run under `uv run --with faster-whisper`)
        from .pronounce import align_manifest
        align_manifest(od, rd, log=log)
    man = load_json(od / "tts_manifest.json")
    if args.music:
        cfg["mix"]["music"]["path"] = args.music
    cues = run_mix(rd, man, cfg, od, load_league(args.run), log=log)
    try:
        from .packaging import write_chapters
        ch, _ = write_chapters(od, f"draft-night-{od.name}", cues, load_league(args.run))
        log(f"chapters: {rel(ch)}")
    except Exception as e:  # noqa: BLE001
        log(f"chapters: skipped ({type(e).__name__}: {e})")
    return cues


def cmd_avatars(args, cfg) -> dict:
    od = odir_for(args)
    L = load_league(args.run)
    cues = load_json(od / "cues.json")
    env = load_json(od / "envelopes.json")
    speakers = av.all_speakers(L)
    fs = av.load_framesets(L, speakers)
    tracks = av.build_tracks(cues, env, cfg, speakers, od, fs)
    av.contact_sheet(fs, od / "avatar_frames.png")
    (od / "avatar_frames.json").write_text(json.dumps(av.describe(fs), indent=1))
    log(f"avatars: {len(speakers)} frame sets ({', '.join(sorted({f.style for f in fs.values()}))}); "
        f"{av.mouth_changes_count(tracks)} state changes -> {rel(od / 'avatar_tracks.json')}")
    return tracks


def cmd_compose(args, cfg, minutes: float | None) -> dict:
    from .compose import ShowModel, write_project
    od = odir_for(args)
    L = load_league(args.run)
    rd = load_json(od / "rundown.json")
    cues = load_json(od / "cues.json")
    tracks = load_json(od / "avatar_tracks.json")
    env = load_json(od / "envelopes.json")
    fs = av.load_framesets(L, av.all_speakers(L))
    model = ShowModel(L, rd, cues, tracks, fs, cfg, env=env)
    man = write_project(model, cfg, minutes, od / "mix.wav", log=log)
    (od / "compose.json").write_text(json.dumps(man, indent=1))
    return man


def cmd_render(args, cfg) -> dict:
    from .render import mux, render_all
    od = odir_for(args)
    t0 = time.time()
    man = cmd_compose(args, cfg, args.minutes)
    t1 = time.time()
    rr = render_all(man, cfg, od, jobs=args.jobs, log=log)
    t2 = time.time()
    name = (f"{args.name}.mp4" if args.name else
            f"draft-night-{odir_for(args).name}" + (f"-{args.minutes:g}min" if args.minutes else "") + ".mp4")
    info = mux(man, cfg, od, name, log=log)
    t3 = time.time()
    try:  # YouTube chapters + description next to the video
        from .packaging import write_chapters
        cues_ = load_json(od / "cues.json")
        ch, _ = write_chapters(od, Path(name).stem, cues_, load_league(args.run))
        log(f"chapters: {ch.read_text().count(chr(10))} -> {rel(ch)}")
    except Exception as e:  # noqa: BLE001
        log(f"chapters: skipped ({type(e).__name__}: {e})")
    rendered = [c for c in rr["chunks"] if not c["cached"]]
    report = {
        "output": info, "video_s": man["duration_s"], "chunks": len(man["chunks"]),
        "timing": {"compose_s": round(t1 - t0, 1), "render_s": round(t2 - t1, 1), "mux_s": round(t3 - t2, 1),
                   "chunks_rendered": len(rendered), "render_jobs": rr["jobs"]},
    }
    if rendered and not any(c["cached"] for c in rr["chunks"]):
        rate = man["duration_s"] / max(0.1, t2 - t1)
        report["timing"]["realtime_factor"] = round(rate, 3)
        report["timing"]["extrapolated_45min_render_min"] = round(45 * 60 / rate / 60, 1)
    (od / "render_report.json").write_text(json.dumps(report, indent=1))
    log(f"render: {man['duration_s']:.1f}s video; render wall {t2 - t1:.1f}s; -> {info['path']}")
    return report


def _short_pipeline(args, cfg, L, rd, od, name) -> dict:
    from .mix import run_mix
    from .short import render_short
    from .tts import run_tts
    bad = verify_own_words(rd, L)
    if bad:
        raise SystemExit(f"own-words guardrail failed for lines: {bad}")
    save_rundown(rd, od / "rundown.json")
    fallback = None if args.fallback in ("", "none") else args.fallback
    man = run_tts(rd, cfg, args.engine, L, max_chars=args.max_chars, fallback=fallback, log=log, plan=args.plan,
                  cache_only=args.cache_only)
    if man.get("plan"):
        return man
    (od / "tts_manifest.json").write_text(json.dumps(man, indent=1))
    if args.align:  # dev voices: word timing from Whisper (run under `uv run --with faster-whisper`)
        from .pronounce import align_manifest
        align_manifest(od, rd, log=log)
        man = json.loads((od / "tts_manifest.json").read_text())
    cues = run_mix(rd, man, cfg, od, L, log=log)
    speakers = av.all_speakers(L)
    fs = av.load_framesets(L, speakers)
    env = load_json(od / "envelopes.json")
    tracks = av.build_tracks(cues, env, cfg, speakers, od, fs)
    return render_short(od, name, L, rd, cues, tracks, env, cfg, log=log, render=not args.no_render)


def cmd_short(args, cfg) -> dict:
    """Vertical short: hook -> moment -> end card, through the full audio + motion pipeline."""
    from .script import build_short_rundown
    cfg = load_config("short", {"script": {"target_minutes": 1}, "mix": ({"tempo": args.tempo} if args.tempo else {})})
    if not args.picks and cfg["script"].get("format", "party") != "party":
        raise SystemExit("short needs --picks N[,N...] (e.g. --picks 2 or --picks 9,10) and --name")
    picks = [int(x) for x in args.picks.split(",")] if args.picks else []  # party format: none = the best chain
    name = args.name or ("p" + "-".join(args.picks.split(",")) if args.picks else "chain")
    spec = {"name": name, "picks": picks, "hook": args.hook, "chain_len": args.chain_len, "reactions": 99,
            "tempo": args.tempo or cfg["mix"].get("tempo") or 1.0, "end_s": float(cfg["mix"]["postroll_s"])}
    if args.says:  # exactly these SAY lines (a factory candidate's lines)
        spec["says"] = [int(x) for x in args.says.split(",") if x.strip()]
    if args.judge:  # the comedy judge's gate, as the factory applies it
        spec.update({"judge": True, "target_s": 43.0 if len(picks) > 1 else 38.0})
    if args.target_s:  # the edit's budget (the QA gate still fails a short over 50 s)
        spec.update({"target_s": float(args.target_s), "max_s": min(49.5, float(args.target_s) + 1.5)})
    if args.engine == "elevenlabs":
        from .tts import take_durations
        spec["durations"] = lambda sp, text, parts: take_durations(cfg, load_league(args.run), sp, text, parts)
    L = load_league(args.run)
    od = out_dir(f"{args.run}-shorts", name)
    rd = build_short_rundown(L, cfg, spec)
    if args.cta:
        rd["short"]["cta"] = args.cta
    return _short_pipeline(args, cfg, L, rd, od, name)


def cmd_roast(args, cfg) -> dict:
    """'The AIs roast each other's drafts': the best report-card roasts back to back (vertical)."""
    from .script import build_roast_rundown
    cfg = load_config("short", {"script": {"target_minutes": 1}, "mix": {"tempo": args.tempo or 1.1}})
    name = args.name or "roast"
    L = load_league(args.run)
    od = out_dir(f"{args.run}-shorts", name)
    rd = build_roast_rundown(L, cfg, {"name": name, "n": args.n})
    if args.cta:
        rd["short"]["cta"] = args.cta
    return _short_pipeline(args, cfg, L, rd, od, name)


def cmd_factory(args) -> list[Path]:
    """The shorts factory: without --render, label + judge the draft and write the owner's shortlist (nothing is
    voiced); with --render, voice and render ONLY the approved shorts (approve.txt or --approve S01,S02)."""
    import copy

    from .factory import approved_ids, rundown_for, run_factory
    from .judge import review_dir
    tempo = float(args.tempo or 1.12)
    over = {"script": {"target_minutes": 1}, "mix": {"tempo": tempo}}
    if getattr(args, "cut_models", None):  # those models never speak in a short either
        over["script"]["cut_models"] = [x.strip() for x in args.cut_models.split(",") if x.strip()]
    cfg = load_config("short", over)
    L = load_league(args.run)
    if args.montage and not args.render:  # just the montage, from shorts already rendered
        from .factory import montage
        montage(args.run, approved_ids(args.run, args.approve), log=log)
        return []
    if not args.render:
        run_factory(L, args.run, cfg, log=log, tempo=tempo)
        return []
    ids = approved_ids(args.run, args.approve)
    if not ids:
        raise SystemExit(f"factory --render: nothing approved yet: put ids in "
                         f"{review_dir(args.run) / 'approve.txt'} (or pass --approve S01,S03)")
    cpath = review_dir(args.run) / "candidates.json"
    if not cpath.exists():
        raise SystemExit("factory --render: no shortlist yet: run `factory --run <run>` first")
    by_id = {c["id"]: c for c in json.loads(cpath.read_text())["candidates"]}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"factory --render: not on the shortlist: {missing}")
    budget = args.max_chars
    planned, dirs = 0, []
    for cid in ids:
        c = by_id[cid]
        ccfg = cfg if c["kind"] not in ("roast", "delusion") else load_config(
            "short", {"script": {"target_minutes": 1}, "mix": {"tempo": 1.1}})
        rd = rundown_for(L, ccfg, c, tempo, log=log)
        name = f"{cid}-{c.get('key') or c['kind']}"
        od = out_dir(f"{args.run}-shorts", name)
        a2 = copy.copy(args)
        a2.max_chars = budget
        res = _short_pipeline(a2, ccfg, L, rd, od, name)
        if res.get("plan"):
            planned += int(res.get("billable_chars") or 0)
            log(f"factory plan: {cid}: {res.get('billable_chars', 0)} billable characters")
            continue
        man = json.loads((od / "tts_manifest.json").read_text()) if (od / "tts_manifest.json").exists() else {}
        spent = int(man.get("billable_chars") or 0)
        if budget is not None:
            budget = max(0, budget - spent)
        if res.get("mp4"):
            dirs.append(od)
        log(f"factory: {cid} -> {rel(od)} ({spent} characters)")
    if args.plan:
        log(f"factory plan: {len(ids)} approved shorts, {planned} billable characters in total "
            f"(render with --max-chars {planned})")
    elif args.montage:
        from .factory import montage
        montage(args.run, ids, log=log)
    return dirs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gmbench_media", description="AI GM League · Draft Night show pipeline")
    ap.add_argument("command", choices=["rundown", "tts", "mix", "avatars", "compose", "render", "all", "short", "roast",
                                        "qa", "factory"])
    ap.add_argument("--run", default="show-dev")
    ap.add_argument("--profile", default="full", help="config profile from media/show.yaml / defaults (full, acceptance)")
    ap.add_argument("--engine", default=None, help="tts engine: say | silent | elevenlabs")
    ap.add_argument("--fallback", default="say",
                    help="tts: for speakers with no voice on --engine: say | silent | host (host voice reads the GM's "
                         "unchanged words) | none (fail)")
    ap.add_argument("--only", default=None, help="tts: comma-separated line ids (smoke tests)")
    ap.add_argument("--max-chars", type=int, default=None, help="tts: hard cap on billable characters (paid engines)")
    ap.add_argument("--minutes", type=float, default=None, help="render only the first N minutes")
    ap.add_argument("--jobs", type=int, default=None, help="concurrent chunk renders")
    ap.add_argument("--music", default=None, help="music bed file (repo-relative); ducked under voices")
    ap.add_argument("--target-minutes", type=float, default=None)
    ap.add_argument("--polish", action="store_true", help="rundown: Game-7 host polish by a non-competing model")
    ap.add_argument("--picks", default=None, help="short: pick number(s) to feature, e.g. 2 or 9,10")
    ap.add_argument("--hook", default=None, help="short: ledger seq (or say-<seq>) of the cold-hook line; default = best")
    ap.add_argument("--name", default=None, help="short/roast: output name; render: output file stem")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--plan", action="store_true", help="tts/short/roast: print billable characters and stop")
    ap.add_argument("--align", action="store_true",
                    help="mix/short/roast/all: time dev-voice words with Whisper (needs `uv run --with faster-whisper`)")
    ap.add_argument("--n", type=int, default=6, help="roast: number of roast lines (hook included)")
    ap.add_argument("--chain-len", type=int, default=3, help="short (party format, no --picks): picks in the auto chain")
    ap.add_argument("--chains", default=None, help="highlights: pinned chains, e.g. '1-3' or '1-3;7-11' (clip mode)")
    ap.add_argument("--tempo", type=float, default=None, help="short/roast: delivery tightening (roast default 1.1)")
    ap.add_argument("--cta", default=None, help="short/roast: team id whose suit the end card promotes")
    ap.add_argument("--clip", action="append", default=None,
                    help="qa: clip directory / mp4 / out-relative name (repeatable); default = this command's output")
    ap.add_argument("--no-qa", action="store_true", help="render/all/short/roast: skip the automatic QA pass")
    ap.add_argument("--set", action="append", default=None, metavar="KEY=VALUE",
                    help="override a config value, e.g. --set script.comebacks_per_round=2 --set script.meet=false")
    ap.add_argument("--says", default=None, help="short: exactly these SAY seqs (comma-separated)")
    ap.add_argument("--voice-by-model", action="store_true",
                    help="rehearsal proofs only: each model's existing ElevenLabs voice and avatar art even if they were "
                         "made for an older character (the default guard is unchanged); marks the video 'rehearsal cast'")
    ap.add_argument("--personas-from", default=None,
                    help="proofs only: another run's Media Day cards (persona, suit, cup pick) over this run's lines")
    ap.add_argument("--keep-own", default=None,
                    help="with --personas-from: card fields kept from this run's own cards, e.g. cup_pick (its lines "
                         "were written to them)")
    ap.add_argument("--cut-models", default=None,
                    help="rundown/all/factory/short: models whose lines never air, e.g. grok,glm (ids or table names); "
                         "the host announces their picks")
    ap.add_argument("--judge", action="store_true", help="short: apply the comedy judge's gate (as the factory does)")
    ap.add_argument("--target-s", type=float, default=None, help="short: the tight edit's length budget in seconds")
    ap.add_argument("--render", action="store_true", help="factory: voice + render the approved shorts")
    ap.add_argument("--approve", default=None, help="factory --render: ids to render (default: review/<run>/approve.txt)")
    ap.add_argument("--montage", action="store_true",
                    help="factory: also cut 'Draft Night in 3 minutes' from the approved, rendered shorts")
    ap.add_argument("--no-whisper", action="store_true", help="qa: skip the Whisper sync probe")
    ap.add_argument("--names", action="store_true", help="qa: re-run the Whisper name check first")
    ap.add_argument("--strict", action="store_true", help="qa: exit 1 when any check FAILs")
    ap.add_argument("--cache-only", action="store_true",
                    help="tts/all/short/roast: paid engine from cache only; uncached lines use the --fallback dev voice "
                         "(no spend)")
    args = ap.parse_args(argv)
    over: dict = {}
    if args.target_minutes:
        over.setdefault("script", {})["target_minutes"] = args.target_minutes
    if args.polish:
        over.setdefault("script", {})["host_polish"] = {"enabled": True}
    if args.chains:  # pinned chains: a clip of exactly these picks, with every reaction that belongs to them
        over.setdefault("script", {})["highlights"] = {"chains": parse_chains(args.chains), "reactions": 99,
                                                        "button": False}
    if args.cut_models:
        over.setdefault("script", {})["cut_models"] = [x.strip() for x in args.cut_models.split(",") if x.strip()]
    if args.personas_from:
        from . import ledger as _ledger
        _ledger.PERSONA_RUN = args.personas_from
        _ledger.PERSONA_KEEP = {x.strip() for x in (args.keep_own or "").split(",") if x.strip()}
    if args.voice_by_model:
        from . import avatars as _av
        from . import tts as _tts
        _av.BY_MODEL = True
        _tts.VOICE_BY_MODEL = True
        over["rehearsal_cast"] = True
    for kv in args.set or []:  # --set script.comebacks_per_round=2
        key, _, raw = kv.partition("=")
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        node = over
        parts = key.strip().split(".")
        for k in parts[:-1]:
            node = node.setdefault(k, {})
        node[parts[-1]] = val
    cfg = load_config(args.profile, over)
    args.engine = args.engine or cfg["tts"]["engine"]
    t0 = time.time()
    qa_dirs: list[Path] = []
    if args.command == "rundown":
        cmd_rundown(args, cfg)
    elif args.command == "tts":
        cmd_tts(args, cfg)
    elif args.command == "mix":
        cmd_mix(args, cfg)
    elif args.command == "avatars":
        cmd_avatars(args, cfg)
    elif args.command == "compose":
        cmd_compose(args, cfg, args.minutes)
    elif args.command == "render":
        cmd_render(args, cfg)
        qa_dirs = [odir_for(args)]
    elif args.command == "short":
        info = cmd_short(args, cfg)
        qa_dirs = [out_dir(f"{args.run}-shorts", info["name"])] if info.get("mp4") else []
    elif args.command == "roast":
        info = cmd_roast(args, cfg)
        qa_dirs = [out_dir(f"{args.run}-shorts", info["name"])] if info.get("mp4") else []
    elif args.command == "all":
        cmd_rundown(args, cfg)
        if cmd_tts(args, cfg).get("plan"):
            return 0
        cmd_mix(args, cfg)
        cmd_avatars(args, cfg)
        cmd_render(args, cfg)
        qa_dirs = [odir_for(args)]
    elif args.command == "factory":
        qa_dirs = cmd_factory(args)
    elif args.command == "qa":
        from .qa import clip_dirs_for
        qa_dirs = clip_dirs_for(args) or [odir_for(args)]
    failed = 0
    if qa_dirs and (args.command == "qa" or not args.no_qa):
        from .qa import run_qa
        tq = time.time()
        reps = run_qa(args.run, qa_dirs, log=log, whisper=not args.no_whisper, names=args.names)
        failed = sum(r["counts"]["FAIL"] for r in reps)
        log(f"qa: {len(reps)} clip(s) in {time.time() - tq:.0f}s; {failed} FAIL")
    log(f"done in {time.time() - t0:.1f}s")
    return 1 if (failed and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
