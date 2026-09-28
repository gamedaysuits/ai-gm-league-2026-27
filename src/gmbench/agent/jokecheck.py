"""Comedy editor: before an on-air line is accepted, a non-league reader checks it lands with a viewer.

The models' weak lines are rarely wrong facts; they're joke-shaped sentences a fan won't get on first hearing, stock
phrases that sound machine-made, bits somebody already did tonight, or invented human side stories. Two readers from
labs outside the league (the comedy judge's panel) read the line in parallel with the draft board and the recent
lines. Either one's note goes back to the GM once, and the GM rewrites in its own words; the editor never writes or
suggests wording. The rewrite stands; the comedy judge and the owner decide later what airs.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

EDITORS = ("minimax/minimax-m3", "mistralai/mistral-medium-3-5")  # neither lab is in the league
SYSTEM = """\
You are the comedy editor for "Draft Night": a fantasy-hockey draft party where thirteen AI models, going by their \
model names (Qwen, Gemini, Opus, Grok, Fable, Sol, Astra, Kimi, MiMo, DeepSeek, Muse, GLM, Fugu), chirp each other \
like hockey bros, plus one silent control bot ("the robot"; nobody at the table knows how it picks). They're openly AIs. The audience is hockey fans on \
YouTube and TikTok who have used ChatGPT.

You read ONE line before it airs, with the draft board and what was just said. The line FAILS if any of these holds:
1. It doesn't make sense: a comparison whose parts don't map onto the real pick or situation, a mixed metaphor, a \
pun with no point, a line listeners would read different ways, or phrasing so awkward the listener has to stop and \
parse it.
2. It doesn't land instantly: a riddle, a callback the viewer never saw, a name the viewer can't know (a GM's \
fantasy franchise, its town or its avatar's name; viewers only know the model names), an invented human side story \
(family, a job, a truck, a hometown story; they're AIs), or AI jargon a fan wouldn't know (tokens, context window, \
parameters, weights, RLHF, temperature, embeddings, inference).
3. It's slop: a stock internet phrase (bold move, chef's kiss, let's see how that works out, buckle up, no notes, \
built different and the like), a generic chirp that would fit any pick, or a bit someone already did in the recent \
lines.
4. It's mean: it insults a person instead of teasing a pick, or it touches a real player's personal life.
5. It isn't funny: its joke wouldn't get a real laugh from a room of hockey fans (a specific, surprising point that \
fits the pick or what was just said), or it spends a sentence on a pleasantry ("fine pick", "nice one"). A mild, \
generic or strained chirp fails. Announcing the pick is fine, but a pick call's jab or closer, a comeback and table \
talk each need a real joke.

Welcome: self-aware AI jokes whose point is obvious (thinking time, compute, cost, being a chatbot, getting \
fact-checked, lab teammates: Astra and Sol are both OpenAI, Fable and Opus both Anthropic), hockey jokes \
about the player's real team or reputation, and hockey-bro slang. Facts about tonight in the board (who took whom, how long a GM thought) are true. A line that claims to know how the robot picks doesn't land: nobody knows.

Never judge facts: stats, games played, ages or teams are checked separately against real data, so treat every factual claim as true and judge only whether the joke works as said.

Don't rescue a line by inventing a clever reading for it: if you had to work out what it means, the room won't get \
it. Don't nitpick a line that works either: most good lines are simple.

Reply with ONLY JSON: {"ok": true} or {"ok": false, "note": "<one short sentence for the writer: what doesn't work \
and why. Never suggest wording>"}"""
KINDS = {
    "pick": "a pick call: a jab at the pick right before, then the GM's own pick, then maybe a closer",
    "comeback": "a comeback: the GM fires back at whoever just talked about their pick",
    "reaction": "a reaction: the GM chirps a pick that just happened",
    "table_talk": "table talk: the GM answers the host's question at the round break",
}
JSON_OBJ = re.compile(r"\{.*\}", re.S)


@dataclass
class JokeCheck:
    api_key: str | None = None
    timeout_s: float = 30.0
    log: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY")

    def read(self, line: str, speaker: str, board: str, recent: list[str], roster: str = "",
             kind: str = "pick") -> str | None:
        """None if the line lands (or no editor answered: fail open); otherwise the editor's note."""
        if not self.api_key:
            return None
        user = (f"The GMs:\n{roster or '(not given)'}\n\nDraft board:\n{board or 'No picks yet.'}\n\n"
                "Just said at the table (speaker: words):\n" + ("\n".join(recent) or "(nothing yet)")
                + f"\n\nThe line is {KINDS.get(kind, kind)}. Said by {speaker}:\n{line}")

        def ask(model: str) -> tuple[bool, str | None]:
            try:
                r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=self.timeout_s,
                               headers={"Authorization": f"Bearer {self.api_key}"},
                               json={"model": model, "max_tokens": 3000, "reasoning": {"effort": "low"},
                                     "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                                     "usage": {"include": True}})
                data = r.json()
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                match = JSON_OBJ.search(content)
                if not match:
                    return False, None
                verdict = json.loads(match.group(0))
                note = None if verdict.get("ok", True) else str(verdict.get("note") or "it doesn't land")[:220]
                self.log.append({"model": model, "speaker": speaker, "kind": kind, "line": line, "note": note,
                                 "cost": (data.get("usage") or {}).get("cost")})
                return True, note
            except (httpx.HTTPError, ValueError, KeyError):
                return False, None

        with ThreadPoolExecutor(len(EDITORS)) as pool:
            answers = list(pool.map(ask, EDITORS))
        return next((note for answered, note in answers if answered and note), None)
