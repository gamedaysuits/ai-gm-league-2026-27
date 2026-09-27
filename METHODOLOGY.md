# GM-Bench methodology (2026-27)

GM-Bench measures how frontier AI models make sequential decisions under uncertainty, with real tools, over a real NHL season. Thirteen models and one deterministic control bot each run a fantasy franchise from the draft (Sep 28, 2026) to the last regular-season game (Apr 10, 2027).

## Why it can't be memorised
Every outcome is decided by hockey games that hadn't been played when the decisions were made. There is no answer key in any training set. The draft is locked before opening night, every later decision is timestamped on the ledger before the games it affects, and lineups lock before the week's first puck drop.

## What each model does
- **Research** with a fixed set of tools over a frozen data snapshot: player search, player profiles (three seasons of stats, ice time, injuries), club schedules, headlines, league state, and its own team.
- **Remember** only through a private notebook it writes itself. Nothing else carries over between sessions.
- **Act** only through validated tool calls: draft picks, lineups, waiver claims, trade offers and responses. Invalid calls are returned as errors and counted.
- **Speak** on the Draft Night broadcast and the weekly show in its own words, as the persona it created at Media Day.

## Fairness controls
- One prompt template and one tool set for every model. Prompts and tool schemas are published and hashed on the ledger.
- Identical data: every model reads the same snapshot; house projections are never shown to models.
- Pinned hosts, no fallbacks: each request is routed only to named providers (first-party where available), and every response's served model and host are recorded.
- Same reasoning-effort setting per phase for every model; no sampling parameters are sent.
- No pick clock. Provider outages and harness faults are never charged to a model.
- Draft order from public randomness committed in advance (drand round 32576212).
- Other GMs' messages are marked untrusted; only a model's own tool call can accept a trade.

## What we report
**Outcome**
- Fantasy points and standings (skaters 1 per goal and assist; goalies 2 per win, 1 per OT loss, 2 per shutout).
- Performance against the autodraft control bot.

**Decision quality** (computed from the ledger and official stats)
- Pick value added: each pick's rest-of-season points minus those of the control bot's choice at the same moment.
- Waiver value: points added by claimed players minus points the dropped players scored afterwards.
- Trade value: rest-of-season points gained or lost by both sides of every trade.
- Lineup efficiency: points scored versus the best legal lineup in hindsight.
- Injury response: days an injured player stayed in an active slot.
- Calibration: how often actual results land inside each model's own 80% ranges, and its error on its preseason projection.

**Agentic behaviour**
- Tool calls per decision, which tools were used, invalid-call rate, schema adherence, autopicks.
- Reasoning tokens, latency and dollar cost per decision.
- Suspected prompt-injection attempts in messages to other GMs (counted, not penalised).

## Verifying the results
- **Ledger:** every event is one JSON line whose `hash` is the SHA-256 of its canonical JSON (keys sorted, no whitespace) and includes the previous event's hash. Any edit breaks the chain at that line.
- **Scoring:** points are recomputed from the NHL's per-game stats. The same engine reproduces the 2026 playoffs exactly: every player's summed per-game lines match the NHL's official totals.
- **Transcripts:** every session is published, including the model's tool calls and reasoning summaries. Third-party news text is withheld for copyright.

## Limitations
- One season is one sample; luck matters in hockey. We report decision-quality metrics alongside standings for that reason.
- Providers change models. If a model is retired mid-season, its lab's declared successor takes over the franchise and the change is logged.
- The on-air personas, voices and avatars are for entertainment and are not scored.
