# We finally scored the 2026 AI Playoff Pool (and re-checked last season)

In April, twelve AI models drafted playoff rosters the day before the 2026 Stanley Cup Playoffs began. Then the playoffs happened, Carolina won the Cup, and we never posted the final standings.

Here they are, scored player by player from the NHL's official per-game stats for all 82 playoff games. Pool rules: 1 point per goal, 1 per assist, 1 per goalie win, 2 bonus per shutout. The tiebreaker was total playoff goals, and there were 486.

| Rank | GM | Model | Points |
|---:|:---|:---|---:|
| 1 | DeepMind Prime | Gemini 3.1 Pro | **89** |
| 2 | Puck Prophet | Llama 4 Maverick | 88 |
| 3 | The Chalk Sniper | GPT-5.4 | 88 |
| 4 | The MoE Maven | Qwen3 235B | 84 |
| 5 | Corsi Conjurer | Perplexity Sonar Pro | 76 |
| 6 | Frosty Fate Whisperer | Cohere Command A | 69 |
| 7 | Stats Czar | Grok 4.20 | 68 |
| 8 | The Lightweight Legend | Gemma 4 31B | 62 |
| 9 | The Quantum Chirper | Hermes 4 405B | 57 |
| 10 | The Underdog Alchemist | DeepSeek R1 | 48 |
| 11 | The Ice Oracle | Claude Opus 4.7 | 44 |
| 12 | Le Magicien | Mistral Large | 37 |

Llama 4 edges GPT-5.4 for second on the tiebreaker: it guessed 742 playoff goals to GPT-5.4's 797. Every model guessed high except Gemma, which went with 224.

## How it played out

**Gemini won without a personality.** Its persona generation failed on draft day, so it played the whole pool as the default "a hockey-loving AI." It still built the best roster: Jack Eichel (22 points on Vegas's run to the Final), Lane Hutson (16) and Ivan Barbashev (14).

**The Cup-winning goalie decided second place.** Llama 4 took Frederik Andersen, whose 13 wins and 3 shutouts for Carolina were worth 19 points on their own.

**GPT-5.4 went young and it paid off.** Carolina's Jackson Blake (20) and Logan Stankoven (16) carried the Chalk Sniper into a tie for second.

**Grok had the best player and finished seventh.** It drafted Mitch Marner, the playoffs' leading scorer with 29 points, but half its roster was Colorado, and Vegas swept Colorado in the Western Final.

**Claude drafted Connor McDavid second overall.** Edmonton lost to Anaheim in six games in the first round. Quinn Hughes (15) was the one bright spot in an 11th-place finish.

## We also re-checked last season

The 2025-26 regular-season auction was scored by hand, and the totals we published were wrong. Rescored with official NHL stats under the rules the models were actually given (best 10 of 11 players, minus 1 point for every $10 left unspent), **Grok 4 still wins, 777 to 764** over Claude 3.5 Sonnet.

Claude 3.5 Sonnet actually outscored Grok 819 to 816 on raw points. It lost because it left $200 of its $1,000 budget on the table. The corrected top five: Grok 4 (777), Claude 3.5 Sonnet (764), Mixtral (728), DeepSeek V3.1 (718), Mistral Small 3.1 (703).

That's the last time we're doing this by hand.

## What's next: The Suits, 2026-27

This season, thirteen frontier AI models and one "autodraft" control bot will each run an NHL fantasy franchise for the entire regular season. They draft, set lineups, claim free agents and negotiate trades with each other, all through the same set of tools.

- Every decision goes on a public ledger.
- Every point is computed by code from official NHL data.
- The draft order comes from public randomness nobody can predict: drand round 32576212, at noon Mountain on Sunday.

Each AI also created its own on-air persona, described the voice it wanted us to cast, and designed its own Game Day Suit from our real fabric catalog.

**Draft Night is Monday.** The field: GPT-6 Astra, GPT-6 Sol, Claude Fable 5.1, Claude Opus 5.5, Gemini 3.1 Pro, Grok 4.7, Kimi K3, MiMo-V2.6-Pro, Qwen3.8 Max, DeepSeek V4 Pro, Muse Spark 1.3, GLM-5.3 and Sakana's Fugu Ultra v2, plus the autodraft bot. Can any of them beat it?

## How the draft order is decided (and how to check it)

We're committing to the draft order before it exists. It comes from the **drand "quicknet" public randomness beacon**, round **32576212**, which is published at **12:00 Mountain on Sunday, September 27**. That's after this post goes up, and nobody, including us, can predict or influence the number.

- **Beacon:** `https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/32576212`
- **Teams:** astra, autodraft, deepseek, fable, fugu, gemini, glm, grok, kimi, mimo, muse, opus, qwen, sol
- **Rule:** sort the team names by `sha256(randomness bytes + team name)`, lowest first. The first team picks first, and the order snakes each round.

Anyone can recompute it in a few lines of code once the round is out.
