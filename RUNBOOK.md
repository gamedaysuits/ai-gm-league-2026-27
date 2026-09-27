# Runbook — launch week

All commands run from the repo root. The live league is the run named `live`.
Keep the Mac awake during long steps: prefix with `caffeinate -i`.

## Sunday Sep 27

1. **Before 12:00 MDT** — the drand commitment (`DRAND_COMMITMENT.md`) is posted publicly.
2. **Draft snapshot (after ~11:00 MDT):**
   `uv run python -m gmbench.data.snapshot --kind draft --as-of 2026-09-28`
   → note the printed `saved data/snapshots/draft-2026-09-28-<sha>.json` path.
3. **Freeze the prompts first.** Owner edits to the on-air style guide `prompts/ON_AIR.md` must be in before this step. The league records the prompt hash at creation, and a later edit would show up as a changed prompt mid-season.
   **Create the league:**
   `uv run gmbench init --run live --snapshot data/snapshots/draft-2026-09-28-<sha>.json`
4. **12:00 MDT or later — draft order from drand round 32576212:**
   `uv run gmbench order --run live --drand-round 32576212`
5. **Media Day** (≈2 min, ≈$1): `uv run gmbench mediaday --run live --today 2026-09-27`
   → brand-check `runs/live/exports/personas.json` (names, no real people, PG-13).
   Any card that needs a redo: delete nothing — rerun for that team only after an `ADMIN_ACTION` note.
6. **Voices + avatars** from the persona cards. They're independent, so run them in two terminals.
   - **Voices** (≈45–60 min, ≈26k ElevenLabs characters for 12 GMs: each GM's top candidates are rendered for real to check the shout lift and judged for accent; the 7-GM party rehearsal on 2026-09-27 used 15.7k). Timbre and accent come from each card; energy comes from delivery tags at render time.
     **The host is FIXED:** `GMB-host` = voice_id `pExXCSeVFtFLETPgZoWn` (library "Darren", Canadian; the Commissioner speaks short cues, stability 0.0). Chosen 2026-09-27 for the accent; its over-limit similarity to two rehearsal voices (mimo, gemini) was accepted for tonight only. Always run with `--no-host`; recast only with `--recast-host`. Every new GM is checked for distinctness against the host (≤0.75).
     **Adam is reference only:** `GMB-ref-american-Adam` = voice_id `wBXNqKUATyqu0RtYt25i` (library "Adam – Radio Announcer", General American, the former host) is the American reference voice for the listening accent check. It is not a show voice. Never delete it.
     `uv run media/voices/design.py --personas runs/live/exports/personas.json --force --dry-run`
     `uv run media/voices/design.py --personas runs/live/exports/personas.json --force --delete-replaced --no-host --budget 26000`
     If a pair is still over its similarity limit:
     `… --force --teams X --delete-replaced --no-host` plus `--library X` or `--set-register "X=baritone"`
   - **Avatars and animation rigs** (≈$0.6/team base portrait + ≈$2.2/team rig; ≈$35 and ≈30 min for 12 GMs). New personas archive the old art automatically; host and autodraft are reused at $0.
     `uv run media/avatars/generate.py --personas runs/live/exports/personas.json --style pixel --budget 10`
     `uv run media/avatars/generate.py --personas runs/live/exports/personas.json --rig --budget 35`
     Each rig has 9 mouth shapes × 3 moods, 9 eye states and 12 gesture clips, with a pixel-identical head. The rig step is resumable: rerun it as is, `--teams X` limits it, and `--rig-redo body_c` remakes one sheet.
     → review each `media/assets/avatars/<team>/rig/preview.png` (face identical, human mouths on human faces, no text/logos/weapons), then `python media/avatars/rig.py validate`.
     → review `media/assets/avatars/index.html` and the printed flags (no text/logos, no weapons, same character in every pose).
7. **War-room prep** (≈10–20 min, ≈$5–10): `uv run gmbench prep --run live --today 2026-09-27`

## Monday Sep 28 — Draft Day

1. 07:30 — refresh the data (overnight injuries, roster moves); the draft always uses the run's latest snapshot:
   `uv run gmbench snapshot --run live --kind draft --as-of 2026-09-28`
2. 07:45 — `uv run gmbench preflight` (all 13 must pass; a failure pauses the start).
3. **08:00 — draft:** `caffeinate -i uv run gmbench draft --run live --today 2026-09-28 2>&1 | tee runs/live/draft.log`
   - No pick clock. If a GM is unreachable after retries the draft **pauses** (exit code 2) and prints why.
     Resume with the same command. To let the league autopick for that GM from its own queue instead:
     `uv run gmbench draft --run live --on-failure autopick` (logged in the ledger).
   - Crash / Ctrl-C at any point is safe: rerunning resumes from the ledger.
4. **Post-draft** (lineups, grades, predictions): `uv run gmbench postdraft --run live --today 2026-09-28`
5. `uv run gmbench verify --run live`
6. Media: build and render Draft Night overnight.

## Tuesday Sep 29 — Opening Night (first puck 15:00 MDT)

- Lineups must be in by **14:45 MDT** (they are, from post-draft; the bot fills any gap).
- Publish: site, blog post, YouTube (Draft Night), shorts.
- Enable the daily scoring workflow (GitHub Actions) or run by hand Wednesday morning:
  `uv run gmbench score --run live`

## Weekly (automated from Mon Oct 5)

- `weekly.yml` scores through Sunday, snapshots, runs the front office, locks lineups, commits.
- Trading opens Mon Oct 12 (Thanksgiving; first puck 11:00 MDT, lock 10:45 MDT).

## If something goes wrong

- `uv run gmbench verify --run live` pinpoints any ledger corruption by sequence number.
- A torn last line after a crash is repaired automatically on the next `draft` run.
- Never edit `runs/live/ledger/league.jsonl` by hand. Corrections are new events.
