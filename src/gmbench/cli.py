"""gmbench command-line entry point."""
from __future__ import annotations

import argparse
import json
from datetime import date

from gmbench.config import load_config, load_secrets


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gmbench")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="multi-turn tool-call check for every competitor")
    pre.add_argument("--teams", nargs="*", help="limit to these team ids")
    pre.add_argument("--effort", default="medium")

    init = sub.add_parser("init", help="create a run's ledger (LEAGUE_CREATED + SNAPSHOT_TAKEN)")
    init.add_argument("--run", required=True)
    init.add_argument("--snapshot", required=True, help="path under the repo, or synthetic:<seed>")

    order = sub.add_parser("order", help="set the draft order from a drand round (or a test seed)")
    order.add_argument("--run", required=True)
    g = order.add_mutually_exclusive_group(required=True)
    g.add_argument("--drand-round", type=int)
    g.add_argument("--seed-hex", help="TEST ONLY: fixed randomness instead of drand")

    draft = sub.add_parser("draft", help="run (or resume) the draft")
    draft.add_argument("--run", required=True)
    draft.add_argument("--fake", action="store_true", help="use the scripted fake model (zero cost)")
    draft.add_argument("--stop-after", type=int)
    draft.add_argument("--on-failure", choices=["pause", "autopick"], default="pause")
    draft.add_argument("--no-reactions", action="store_true")
    draft.add_argument("--today", default=date.today().isoformat())

    md = sub.add_parser("mediaday", help="run Media Day persona sessions (parallel, sealed)")
    md.add_argument("--run", required=True)
    md.add_argument("--teams", nargs="*")
    md.add_argument("--fake", action="store_true")
    md.add_argument("--today", default=date.today().isoformat())

    pd = sub.add_parser("postdraft", help="Week 1 lineups, draft grades and predictions (parallel, sealed)")
    pd.add_argument("--run", required=True)
    pd.add_argument("--teams", nargs="*")
    pd.add_argument("--fake", action="store_true")
    pd.add_argument("--week-start", default="2026-09-29")
    pd.add_argument("--today", default=date.today().isoformat())

    pr = sub.add_parser("prep", help="war-room prep: research, notebook, draft queue (parallel, sealed)")
    pr.add_argument("--run", required=True)
    pr.add_argument("--teams", nargs="*")
    pr.add_argument("--fake", action="store_true")
    pr.add_argument("--max-tool-calls", type=int, default=20)
    pr.add_argument("--today", default=date.today().isoformat())

    sc = sub.add_parser("score", help="rescore a trailing window of NHL game dates and export standings")
    sc.add_argument("--run", required=True)
    sc.add_argument("--end", help="last game date to score (default: yesterday, Eastern)")
    sc.add_argument("--days", type=int, default=7)

    wk = sub.add_parser("week", help="run the weekly front office (R1 front office, R2 trade desk, R3 final call, lock)")
    wk.add_argument("--run", required=True)
    wk.add_argument("--week-start", help="Monday of the week (default: this Monday, Eastern)")
    wk.add_argument("--snapshot", help="snapshot ref for this week (default: the run's latest SNAPSHOT_TAKEN)")
    wk.add_argument("--fake", action="store_true")
    wk.add_argument("--today", default=date.today().isoformat())

    pl = sub.add_parser("pool", help="score an auction pool from the committed NHL game lines")
    pl.add_argument("--pool", required=True, help="pool file, e.g. pools/auction-pool-2026-27.yaml")
    pl.add_argument("--run", default="live", help="run whose committed game lines to score from")
    pl.add_argument("--offline", action="store_true", help="skip the NHL name lookup; players without an id stay unresolved")

    ex = sub.add_parser("export", help="write public exports: draft, standings, scorecard, redacted transcripts")
    ex.add_argument("--run", required=True)

    snp = sub.add_parser("snapshot", help="build a fresh data snapshot and make it the run's current one")
    snp.add_argument("--run", required=True)
    snp.add_argument("--kind", default="draft")
    snp.add_argument("--as-of", required=True, help="league date the snapshot represents (YYYY-MM-DD)")

    ver = sub.add_parser("verify", help="verify a run's ledger chain and print the state hash")
    ver.add_argument("--run", required=True)

    args = parser.parse_args(argv)
    load_secrets()
    cfg = load_config()

    if args.command == "preflight":
        from gmbench.preflight import run_preflight

        run_preflight(cfg, teams=args.teams, effort=args.effort)

    elif args.command == "init":
        from gmbench.league import init_league, ledger_for

        init_league(ledger_for(args.run), cfg, args.snapshot)
        print(f"initialised run {args.run!r}")

    elif args.command == "order":
        from gmbench.draft.order import fetch_beacon
        from gmbench.league import ledger_for, set_draft_order

        if args.drand_round:
            beacon, method = fetch_beacon(args.drand_round), "drand-quicknet"
        else:
            beacon, method = {"round": None, "randomness": args.seed_hex, "signature": None}, "test-seed"
        order = set_draft_order(ledger_for(args.run), cfg, beacon, method=method)
        print("draft order:", " → ".join(order))

    elif args.command == "draft":
        from gmbench.draft.run import DraftDeps, DraftPaused, run_draft
        from gmbench.league import ledger_for, load_run_snapshot, run_dir
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.repair_torn_tail()
        ledger.verify()
        state = replay(ledger.events())
        assert state.snapshot is not None, "run has no SNAPSHOT_TAKEN event"
        snapshot = load_run_snapshot(state.snapshot["ref"])
        client = _client(cfg, args.fake)
        deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snapshot, client=client, root=run_dir(args.run),
                         today=args.today, as_of=snapshot.as_of_date)
        try:
            final = run_draft(deps, stop_after=args.stop_after, on_failure=args.on_failure,
                              reactions=not args.no_reactions)
            print(f"draft complete through pick {len(final.picks)}")
        except DraftPaused as exc:
            print(f"PAUSED: {exc}")
            raise SystemExit(2)

    elif args.command == "mediaday":
        from gmbench.draft.run import DraftDeps
        from gmbench.league import ledger_for, load_run_snapshot, run_dir
        from gmbench.mediaday import run_media_day
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.verify()
        snapshot = load_run_snapshot(replay(ledger.events()).snapshot["ref"])
        client = _client(cfg, args.fake)
        deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snapshot, client=client, root=run_dir(args.run),
                         today=args.today, as_of=snapshot.as_of_date)
        run_media_day(deps, teams=args.teams)

    elif args.command == "postdraft":
        from gmbench.draft.run import DraftDeps
        from gmbench.league import ledger_for, load_run_snapshot, run_dir
        from gmbench.postdraft import run_post_draft
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.verify()
        snapshot = load_run_snapshot(replay(ledger.events()).snapshot["ref"])
        deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snapshot, client=_client(cfg, args.fake), root=run_dir(args.run),
                         today=args.today, as_of=snapshot.as_of_date)
        run_post_draft(deps, week_start=date.fromisoformat(args.week_start), teams=args.teams)

    elif args.command == "prep":
        from gmbench.draft.run import DraftDeps
        from gmbench.league import ledger_for, load_run_snapshot, run_dir
        from gmbench.prep import run_prep
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.verify()
        snapshot = load_run_snapshot(replay(ledger.events()).snapshot["ref"])
        deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snapshot, client=_client(cfg, args.fake), root=run_dir(args.run),
                         today=args.today, as_of=snapshot.as_of_date)
        run_prep(deps, teams=args.teams, max_tool_calls=args.max_tool_calls)

    elif args.command == "score":
        from datetime import timedelta

        from gmbench.export.site import write_standings_export
        from gmbench.export.tracker import write_tracker_export
        from gmbench.league import ledger_for, run_dir
        from gmbench.season.daily import run_scoring, yesterday_eastern
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.verify()
        games_dir = run_dir(args.run) / "data" / "games"
        end = date.fromisoformat(args.end) if args.end else yesterday_eastern()
        start = max(end - timedelta(days=args.days - 1), date(2026, 9, 29))
        if start > end:
            print(f"nothing to score yet (season starts 2026-09-29; end {end})")
        else:
            run_scoring(ledger, start=start, end=end, games_dir=games_dir)
        state = replay(ledger.events())
        out = run_dir(args.run) / "exports"
        path = write_standings_export(state, cfg, out, today=date.today().isoformat())
        tracker = write_tracker_export(state, cfg, out, games_dir=games_dir, today=date.today().isoformat(),
                                       snapshot=_snapshot_on_disk(state))
        print(f"standings → {path} | tracker → {tracker}")

    elif args.command == "week":
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from gmbench.draft.run import DraftDeps
        from gmbench.league import ledger_for, load_run_snapshot, run_dir
        from gmbench.season.weekly import run_week
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.verify()
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        week_start = date.fromisoformat(args.week_start) if args.week_start else today_et - timedelta(days=today_et.weekday())
        ref = args.snapshot or replay(ledger.events()).snapshot["ref"]
        if args.snapshot:
            ledger.append("SNAPSHOT_TAKEN", "league", {"kind": f"week-{week_start}", "ref": ref, "sha256": None,
                                                       "as_of_date": week_start.isoformat(), "counts": {}})
        snapshot = load_run_snapshot(ref)
        deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snapshot, client=_client(cfg, args.fake), root=run_dir(args.run),
                         today=args.today, as_of=snapshot.as_of_date)
        run_week(deps, week_start=week_start, games_dir=run_dir(args.run) / "data" / "games")

    elif args.command == "export":
        from gmbench.export.scorecard import write_scorecard
        from gmbench.export.site import write_draft_export, write_grades_export, write_standings_export
        from gmbench.export.tracker import write_tracker_export
        from gmbench.export.transcripts import export_public_transcripts
        from gmbench.league import ledger_for, load_run_snapshot, run_dir
        from gmbench.state import replay

        ledger = ledger_for(args.run)
        ledger.verify()
        state = replay(ledger.events())
        out = run_dir(args.run) / "exports"
        write_draft_export(state, load_run_snapshot(state.snapshot["ref"]), cfg, out)
        write_standings_export(state, cfg, out, today=date.today().isoformat())
        write_tracker_export(state, cfg, out, games_dir=run_dir(args.run) / "data" / "games",
                             today=date.today().isoformat(), snapshot=_snapshot_on_disk(state))
        write_scorecard(ledger, cfg, out)
        write_grades_export(state, cfg, out)
        print("exports →", out, "| public transcripts →", export_public_transcripts(run_dir(args.run)))

    elif args.command == "pool":
        from pathlib import Path

        from gmbench.config import ROOT
        from gmbench.league import run_dir
        from gmbench.pool import Directory, load_pool, write_pool_export
        from gmbench.season.scoring import fetch_season_totals

        pool = load_pool(Path(args.pool))
        directory = None if args.offline else Directory(fetch_season_totals(pool["season"]))
        path = write_pool_export(pool, run_dir(args.run) / "data" / "games", ROOT / "exports" / "pools", directory=directory)
        missing = json.loads(path.read_text())["unresolved"]
        print(f"pool → {path}" + (f" | unresolved: {', '.join(m['name'] for m in missing)}" if missing else ""))

    elif args.command == "snapshot":
        import hashlib
        from collections import Counter

        from gmbench.config import ROOT
        from gmbench.data.snapshot import build_snapshot, save_snapshot
        from gmbench.league import ledger_for

        ledger = ledger_for(args.run)
        ledger.verify()
        snap = build_snapshot(args.kind, args.as_of)
        path = save_snapshot(snap)
        ref = str(path.relative_to(ROOT))
        counts = Counter(p.group for p in snap.players.values())
        ledger.append("SNAPSHOT_TAKEN", "league", {"kind": snap.kind, "ref": ref, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                   "as_of_date": snap.as_of_date,
                                                   "counts": {"players": len(snap.players), **dict(sorted(counts.items()))}})
        print(f"snapshot {ref} is now current for run {args.run!r} ({len(snap.players)} players)")

    elif args.command == "verify":
        from gmbench.league import ledger_for
        from gmbench.state import replay, state_sha256

        ledger = ledger_for(args.run)
        count, head = ledger.verify()
        print(f"ledger OK: {count} events, head {head[:16]}…, state {state_sha256(replay(ledger.events()))[:16]}…")


def _snapshot_on_disk(state):
    """The run's current snapshot, or None when its file is not on this machine (snapshots are never committed)."""
    from gmbench.league import load_run_snapshot

    ref = (state.snapshot or {}).get("ref")
    if not ref:
        return None
    try:
        return load_run_snapshot(ref)
    except (OSError, ValueError):
        return None


def _client(cfg, fake: bool):
    if fake:
        from gmbench.llm.fake import FakeClient

        return FakeClient()
    from gmbench.llm.openrouter import OpenRouterClient

    return OpenRouterClient(timeout_s=float(cfg.harness["per_call_timeout_s"]), retries=int(cfg.harness["transient_retries"]))


if __name__ == "__main__":
    main()
