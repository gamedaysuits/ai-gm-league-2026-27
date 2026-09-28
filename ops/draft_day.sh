#!/usr/bin/env bash
# Draft Day, Mon Sep 28 2026 (MDT): drand draw + order 06:01, prep, snapshot 07:30, preflight 07:45, draft 08:00, post-draft.
# Unattended and safe to re-run: every step resumes from the ledger. Log: runs/live/draft-day.log
#   caffeinate -i ops/draft_day.sh &
set -uo pipefail
cd "$(dirname "$0")/.."
LOG=runs/live/draft-day.log
DAY=2026-09-28

wait_until() {  # local time HH:MM on draft day
  local target
  target=$(date -j -f "%Y-%m-%d %H:%M" "$DAY $1" +%s)
  while [ "$(date +%s)" -lt "$target" ]; do sleep 20; done
}

{
  echo "== armed $(date)"
  wait_until 06:01  # drand round 32597812 is published at 06:00:00 MDT
  echo "== draft order $(date)"
  until uv run gmbench order --run live --drand-round 32597812; do echo "!! beacon not ready; retrying"; sleep 30; done
  echo "== war-room prep $(date)"
  uv run gmbench prep --run live --today "$DAY" || echo "!! prep incomplete (see above): the draft still starts"

  wait_until 07:30
  echo "== snapshot $(date)"
  uv run gmbench snapshot --run live --kind draft --as-of "$DAY" || echo "!! snapshot failed: drafting on last night's data"

  wait_until 07:45
  echo "== preflight $(date)"
  uv run gmbench preflight || echo "!! preflight failed (see above): the draft still starts; a GM that can't be reached pauses it"

  wait_until 08:00
  echo "== draft $(date)"
  # A GM that stays unreachable pauses the draft (exit 2). Resume every 5 minutes; after 15 minutes stuck, that one
  # pick comes from the GM's own queue (logged in the ledger as an autopick), per the league rules.
  next_pick() { echo $(( $(grep -c '"type":"DRAFT_PICK"' runs/live/ledger/league.jsonl) + 1 )); }
  stuck_on=0; paused=0
  until uv run gmbench draft --run live --today "$DAY"; do
    n=$(next_pick)
    if [ "$n" = "$stuck_on" ]; then paused=$((paused + 1)); else stuck_on=$n; paused=1; fi
    echo "!! draft paused at pick $n ($paused) $(date)"
    if [ "$paused" -ge 3 ]; then
      echo "!! pick $n: GM unreachable 15+ min; autopick from its own queue (logged)"
      uv run gmbench draft --run live --today "$DAY" --stop-after "$n" --on-failure autopick
      paused=0
    else
      sleep 300
    fi
  done
  echo "== draft complete $(date)"

  echo "== postdraft $(date)"
  uv run gmbench postdraft --run live --today "$DAY"
  uv run gmbench verify --run live
  uv run gmbench export --run live
  echo "== done $(date)"
} >> "$LOG" 2>&1
