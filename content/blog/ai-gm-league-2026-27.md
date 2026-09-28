# Gemini won our AI playoff pool. Now 13 AIs get a whole NHL season.

Last spring we had twelve AI models draft teams for the 2026 Stanley Cup Playoffs. Carolina won the Cup. **Gemini 3.1 Pro won the pool** with 89 points, scored straight off the NHL's official stats.

| Rank | Model | Points |
|---:|:---|---:|
| 1 | **Gemini 3.1 Pro** (Google) | **89** |
| 2 | Llama 4 Maverick (Meta) | 88 |
| 3 | GPT-5.4 (OpenAI) | 88 |
| 4 | Qwen3 235B (Alibaba) | 84 |
| 5 | Perplexity Sonar Pro | 76 |

Llama 4 took second on the tiebreaker: closest guess to the 486 goals scored in those playoffs. Claude Opus 4.7 drafted Connor McDavid second overall and finished 11th, because Edmonton went out in the first round. Nobody around here needs that one explained.

This season it's the **AI Fantasy Draft, presented by Game Day Suits**, and it's the whole regular season. Here's how it's built.

## The field: 13 AI models and one robot

GPT-6 Astra and GPT-6 Sol (OpenAI), Claude Fable 5.1 and Claude Opus 5.5 (Anthropic), Gemini 3.1 Pro (Google), Grok 4.7 (SpaceXAI), Kimi K3 (Moonshot), MiMo-V2.6-Pro (Xiaomi), Qwen3.8 Max (Alibaba), DeepSeek V4 Pro (DeepSeek), Muse Spark 1.3 (Meta), GLM-5.3 (Z.ai) and Fugu Ultra v2 (Sakana).

The fourteenth team is **Autodraft**, the control bot. It doesn't talk and it doesn't trade. It's the number every AI has to beat, and none of them know how it picks. You can know; it's further down.

## The league

- **The draft:** 14 teams, a 14-round snake, Monday, September 28 at 8:00 am Mountain. That's 196 picks.
- **Rosters:** 14 players. 6 forwards, 4 defencemen and 2 goalies in the lineup, 2 on the bench, never more than 3 goalies. A pick only counts if the team can still fill a legal lineup with the picks it has left.
- **Scoring:** skaters get 1 point per goal and 1 per assist. Goalies get 2 per win, 1 per overtime or shootout loss and 2 per shutout. Only the lineup scores; the bench watches.
- **Every week:** each AI sets its lineup and can claim up to 2 free agents, worst team first.
- **Trades:** AI to AI, from October 12 until the NHL deadline on March 1. Nobody trades with the robot.
- **The season:** September 29 to April 10. The standings update every day from the NHL's official stats.

## How a pick actually happens

- **One session per pick.** The model on the clock gets the same system prompt as everybody else and a briefing: the board so far, its roster and what it still needs, the best players left by last season's points, and its own notes.
- **Research tools, no internet.** Player search, three seasons of stats, schedules, injuries and headlines, the league standings, and a private notebook it keeps all season. Everybody works off the same frozen data snapshot, refreshed the morning of the draft. Nobody gets to look anything up on the side.
- **The pick is a tool call.** It has to end with `make_pick` and a real player ID, plus its own point projection and an 80% range. The league checks the player is available and the pick is legal; a bad call gets bounced back with the reason.
- **No pick clock.** If a model can't be reached for 15 minutes, the league takes the top player left in that model's own draft queue (the robot's choice if the queue is empty) and writes it down as an autopick.
- **Nobody gets swapped out.** Every model is called through OpenRouter, pinned to its own provider, and we check which model and provider actually answered on every single response. Nobody gets replaced by a cheaper cousin halfway through.

## Everything is on the record

- Every decision is an event in an append-only, hash-chained ledger. Each entry carries the fingerprint of the one before it, so changing anything after the fact breaks the chain at that exact spot.
- The league's standings are rebuilt by replaying the ledger from the start, and anybody with the ledger can do the same.
- The prompts, the data snapshot and the code version are all fingerprinted the day the league is created, so a quiet mid-season tweak would show.
- We're publishing the ledger and the code, so you don't have to take our word for any of it.

## The robot

Autodraft is the baseline. It projects every player's 2026-27 fantasy points from his last three seasons, with the newest season counting most. That rate is pulled toward an average replacement player for anybody with a short track record, adjusted for age (up for guys under 26, down from 30 on), and multiplied by how many games he usually plays. Then it takes the best player that still fits a legal lineup. That's the whole thing, and it takes about a millisecond.

The AIs aren't told any of that. Seems fair; nobody told us how they work either.

## What gets scored

- **Fantasy points**, all season, calculated by code from the NHL's official stats.
- **Stanley Cup picks.** Every AI called its 2027 winner before the draft. We'll check in June.
- **Projections.** Every pick came with the model's own projection and range, so we'll find out who actually knew something.
- **The robot.** Whether any of them beat it, and by how much.

## Draft Night

Each AI picked a voice, an avatar, a Stanley Cup pick and a Game Day Suit it designed from our real fabric catalog. On air they go by their model names, because that's who's competing, and they chirp each other's picks like a basement full of hockey guys. Every word is their own. Anything a model says about a real player or team gets fact-checked against current NHL data before it airs, and a line that isn't funny gets cut, not rewritten.

The full Draft Night goes up on YouTube this week, with the best bits as shorts.

## How the draft order is decided, and how to check it

We're committing to the draft order before it exists. It comes from the **drand "quicknet" public randomness beacon**, round **32597812**, published at **6:00 am Mountain on Monday, September 28**. That's after this post goes up, and nobody, us included, can predict it or change it.

- **Beacon:** `https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/32597812`
- **Teams:** astra, autodraft, deepseek, fable, fugu, gemini, glm, grok, kimi, mimo, muse, opus, qwen, sol
- **Rule:** sort the team names by `sha256(randomness bytes + team name)`, lowest first. The first team picks first, and the order snakes each round.

*Voices and avatars are AI-generated. The AI Fantasy Draft is run by Game Day Suits, and the league's software was built with Anthropic's Claude. Two of the competitors, Claude Fable 5.1 and Claude Opus 5.5, are Anthropic models; they get exactly the same prompts, tools and data as everybody else.*
