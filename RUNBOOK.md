# Runbook — launch week

All commands run from the repo root. The live league is the run named `live`.
Keep the Mac awake during long steps: prefix with `caffeinate -i`.

## Sunday Sep 27

1. **Before Mon 06:00 MDT** — the drand commitment (the blog post + `DRAND_COMMITMENT.md`) is public. (Rounds 32576212
   (Sun noon) and 32587012 (Sun 21:00) were dropped: nothing public carried them before they were drawn.)
2. **Draft snapshot (after ~11:00 MDT):**
   `uv run python -m gmbench.data.snapshot --kind draft --as-of 2026-09-28`
   → note the printed `saved data/snapshots/draft-2026-09-28-<sha>.json` path.
3. **Freeze the prompts first.** Owner edits to the on-air style guide `prompts/ON_AIR.md` must be in before this step. The league records the prompt hash at creation, and a later edit would show up as a changed prompt mid-season.
   **Create the league:**
   `uv run gmbench init --run live --snapshot data/snapshots/draft-2026-09-28-<sha>.json`
4. **Mon 06:00 MDT or later — draft order from drand round 32597812** (ops/draft_day.sh does this, then prep):
   `uv run gmbench order --run live --drand-round 32597812`
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
6. **Media: the complete Draft Night (YouTube) + the shorts factory.** From the repo root, after
   `alias M='uv run --project media gmbench-media'`. Nothing is published by these commands.
   **Monday's plan (final):** the COMPLETE SHOW for YouTube, **ALL GREEN + A LAUGH**: EVERY PICK, each with its host
   cue. A sentence airs only if it makes sense, lands instantly, gets tonight right, has no fact wrong and isn't mean —
   and a joke-shaped sentence (a jab, a closer) also needs a laugh (judge funny ≥ 2). Pick calls are cut sentence by
   sentence: the failing jab goes, the model's plain pick sentence ("I'm taking X…") stays; the host announces the
   pick only when the pick sentence fails, `fact_check` is `unverified` / `off`, or nothing of the model's is left.
   Comebacks and table talk are all or nothing (green + a laugh; all of them in rounds 1–3, then the judge's top 3 per
   round). Then the **aired-context check**: every aired line is re-checked against what AIRS before it — a sentence
   that refers to a line or sentence the viewer never heard is cut (a call) or takes its line down (a comeback),
   repeated until nothing changes (`media/out/review/live/airs.md`). **Repeated bits**: a "that's not X, that's Y" /
   "you're not X, you're Y" or "you chirped X for Y, then did Y" sentence within 8 aired lines of the same shape, or two
   lines making the same point about the same pick or model, keep only the funnier (a call loses just that sentence).
   After cuts: a kept "you/your" sentence whose addressee was named only in a cut sentence goes too; a leading
   "And/But/So…" that leaned on a cut sentence is dropped (logged); a call left with only the player's name (or the
   name plus a stat fragment) goes to the host's energetic call. Gendered / identity insult words aimed at a GM
   ("queens", "ladies", …) count as mean. House rules (an appeal pass on the sentences the panel failed): age slang
   ("the teenager", "old man") is banter, not a fact; a model's guess about its OWN pick in the same line (the robot
   agrees, "fastest pick of the night") is a guess, not a contradiction — hard facts, earlier picks and who said what
   stay strict. A laugh that mocks a model's guess about its OWN pick, cut only for "no laugh", brings that guess back
   (setup + payoff air together); a line that corrects a claim the viewer never heard drops. Echo backstop: two aired
   lines aimed at the same model within 3 aired lines that share a key fact ("seventeen lookups", "the robot agreed")
   keep only the funnier.
   Relax the laugh rule with
   `--set script.require_laugh=false` (steps 3, 4, 5, 7). The show is "AI Fantasy Draft" · "Draft Night", presented by
   Game Day Suits (logo from media/assets/brand/gds-logo.png — QA WARNs while it's the placeholder wordmark); model
   names first everywhere on screen; the cold open: "13 AI models… and one robot, the control team they all have to
   beat" (the robot is opaque: no chips, no clock, no method on screen); AI pick cards show THOUGHT m:ss · N LOOKUPS and
   the model's suit fabric strip; model cards show CUP PICK.
   Report Card, Delusion Index, chapters. Plus the SHORTS from the factory (green and funny ≥ 2). Nothing not aired is
   lost: `media/out/live-complete/transcript.md` lists what airs (model-labelled), then every line and sentence that
   doesn't, with the reason.
   **Cutting a model** ("cut Grok"): add `--cut-models grok` (ids or table names, comma-separated) to steps 3, 4, 5,
   7, 9 and 10 — its lines never air; the host announces its picks. The numbers behind that call:
   `uv run --project media python -m gmbench_media.modelstats --runs party-10,party-11,party-12,party-13,live`
   → `media/out/review/model-comedy.md` (per model: lines, all-green %, shorts %, funny, instant, consistent, mean,
   slop, filler, and what actually gets ON AIR — calls whose joke airs, table lines aired, laughs aired; re-runs on
   any party-N).
   1. **Export** (grades + predictions for the Report Card and the Delusion Index): `uv run gmbench export --run live`
   2. **Comedy judge** (non-league panel minimax-m3 + mistral-medium-3.5, prompt v8: self-aware AIs vs the robot;
      ≈$5, ≈20–40 min for 14 rounds; it decides what airs — ALL GREEN — and gates the shorts: makes sense, lands
      instantly — no riddles, no AI jargon, no invented human life (family, jobs, trucks, hometowns) — consistent
      with the board and who said what, friendly not mean, no fact wrong; a stock internet phrase or a repeated bit
      caps funny at 1):
      `uv run --project media python -m gmbench_media.judge run --run live`
      → `media/out/review/live/judge.md` (check the last line: 0 unjudged; re-run to retry any).
   3. **Script + beat map** (≈$0.10 of labelling): `M rundown --run live --profile complete`
      → prints the lines per kind, **ALL GREEN: n/N GM calls air (the host announces picks …)**, and **the projected
      length and ElevenLabs characters** (target ≈55k), and writes `media/out/live-complete/transcript.md` (every
      line, model-labelled, then what doesn't air and why). The projection prints with it (see the rehearsal numbers
      in the coordinator's party-13 report). That is the plan: no flags needed. (Only if the live draft runs far longer: pass the same `--set`
      flags to steps 3, 4, 5 and 7, e.g. `--set script.comebacks_per_round=2` ≈−2 min, `--set script.meet=false`
      ≈−3 min.)
   4. **TTS plan:** `M tts --run live --profile complete --engine elevenlabs --plan` → the billable characters (the
      account has ≈205k), plus the same projection.
   5. **TTS** (≈10–15 min): `caffeinate -i M tts --run live --profile complete --engine elevenlabs --max-chars <N from 4>`
   6. **Names check / fix** (Whisper hears every player name; it reads the mix, ≈1 min):
      `M mix --run live --profile complete`, then
      `uv run --project media --with faster-whisper python -m gmbench_media.pronounce check --run live --profile complete`
      → if names are flagged:
      `uv run --project media --with faster-whisper python -m gmbench_media.pronounce fix --run live --profile complete --max-chars 3000`,
      then repeat step 5 (only the respelled lines are re-voiced).
   7. **Full render** (TTS is cached by now, so `--max-chars 0` refuses any new spend; ≈0.33 min of render per
      minute of show plus ≈5 min: ≈25–30 min for the ≈70-min show):
      `caffeinate -i M all --run live --profile complete --engine elevenlabs --max-chars 0`
      → `media/out/live-complete/draft-night-live-complete.mp4` + `.chapters.txt` (one per round + Report Card)
      + `.description.txt` (disclosure + chapters, paste into YouTube).
   8. **QA gates** run automatically at the end of step 7 (summary + sheets in `media/out/live-complete/qa/`).
      Re-run: `M qa --run live --profile complete --strict`. A FAIL blocks publishing until fixed.
   9. **Shortlist** (no voices): `M factory --run live` → `media/out/review/live/shortlist.md`: 10–15 reels of
      20–45 s + the Report Card roast reel (R1) + the Delusion Index reel (D1), each with its transcript (every line
      labelled "Qwen (Qwen3.8 Max, Alibaba)"), judge scores, length, characters and ledger lines, and an id next to
      every sentence.
      **Owner approval over chat** (the answer format is at the top of shortlist.md): he replies with ids to render
      (`S03 S07 R1`) and any vetoes (`veto 72.0` a sentence, `veto 72` a line, `veto "words"`, `keep 81.1` to air a
      sentence the judges cut). Write the ids to `media/out/review/live/approve.txt` and the vetoes to `veto.txt`, one
      per line, exactly as he said them. Short ids never change between re-runs; after a veto, re-run step 9 and
      send him the new list if a short he approved changed. A veto of a line that airs in the complete show reaches
      the show only if steps 3–7 are re-run (its replacement comeback may cost a few hundred characters).
   10. **Shorts render** (they cut from the show's takes: expect 0 characters):
       `M factory --run live --render --engine elevenlabs --plan`, then
       `M factory --run live --render --engine elevenlabs --max-chars <N from the plan> --montage`
       → `media/out/live-shorts/<id>-*/short-*.mp4` with titles / description / hashtags / thumbnail beside each,
       and `montage-live.mp4` ("Draft Night in 3 minutes", from the rendered reels only). QA runs after each reel.

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
