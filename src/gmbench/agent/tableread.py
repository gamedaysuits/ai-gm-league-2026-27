"""Table read: before an on-air line is accepted, check it doesn't contradict what happened tonight.

The models' misses are rarely false facts; they're lines that sound like jokes but don't make sense in the room:
"all the good ones went two picks ago" at pick 3, pinning someone else's words on the guy you're ribbing, a folksy
comparison that maps to nothing. A reader from a lab outside the league gets the draft board and the exact recent
lines, and says whether the line lands. A miss goes back to the GM once, with the reason, and the GM rewrites it in
its own words. The reader never writes or suggests wording.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

READERS = ("minimax/minimax-m3", "nvidia/nemotron-3-ultra-550b-a55b")  # both read in parallel; either can note it
SYSTEM = """\
You check ONE line of banter from a fantasy hockey draft party between AI models for CONSISTENCY with what \
actually happened tonight. You get the league's GMs (with the NHL teams they cheer for), the numbered draft \
board, and the exact recent lines with speakers.

Flag the line ONLY if it clearly contradicts them: it says someone took a player they didn't, gets the pick number or \
"how many picks ago" wrong, or claims someone said something they didn't say out loud (for example "you said X" when \
nobody said X, or pinning the words of one GM on another). Jokes, opinions, exaggeration, comparisons and predictions \
are NOT contradictions. Don't judge whether it's funny or check player stats. Announcing a pick is fine: the player the speaker says he's taking is his own pick right now, not something anyone else did. When a line counts picks ("two \
picks ago", "last pick", "first goalie"), check the count against the numbered board: the line is said right after \
the latest pick shown. The speaker's guesses about his OWN pick in this line (how fast he was, how it'll turn out) are guesses, not contradictions. Claims about EARLIER picks and who said what are checked against the board and the lines.

Reply with ONLY JSON: {"ok": true} or {"ok": false, "reason": "<one short sentence naming the contradiction>"}."""
JSON_OBJ = re.compile(r"\{.*\}", re.S)


@dataclass
class TableRead:
    api_key: str | None = None
    timeout_s: float = 25.0
    max_rejections: int = 1  # one note per line; then the GM's rewrite stands
    log: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY")

    def read(self, line: str, speaker: str, board: str, recent: list[str], roster: str = "") -> str | None:
        """None if the line lands (or no reader answered: fail open); otherwise the reason it doesn't."""
        if not self.api_key:
            return None
        user = (f"The GMs:\n{roster or '(not given)'}\n\nDraft board:\n{board or 'No picks yet.'}\n\n"
                "Just said at the table (speaker: words):\n"
                + ("\n".join(recent) or "(nothing yet)") + f"\n\nThe line, said by {speaker}:\n{line}")
        def ask(model: str) -> tuple[bool, str | None]:
            try:
                r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=self.timeout_s,
                               headers={"Authorization": f"Bearer {self.api_key}"},
                               json={"model": model, "max_tokens": 2000, "reasoning": {"effort": "low"},
                                     "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                                     "usage": {"include": True}})
                data = r.json()
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                match = JSON_OBJ.search(content)
                if not match:
                    return False, None
                verdict = json.loads(match.group(0))
                reason = None if verdict.get("ok", True) else str(verdict.get("reason") or "it contradicts tonight")[:200]
                self.log.append({"model": model, "line": line, "reason": reason,
                                 "cost": (data.get("usage") or {}).get("cost")})
                return True, reason
            except (httpx.HTTPError, ValueError, KeyError):
                return False, None

        with ThreadPoolExecutor(len(READERS)) as pool:
            answers = list(pool.map(ask, READERS))
        return next((reason for answered, reason in answers if answered and reason), None)


def roster_text(state) -> str:
    """Public facts about the GMs at the table: what a line may take for granted."""
    rows = []
    if state.order:
        n = len(state.order)
        slots = {t: [r * n + (i + 1 if r % 2 == 0 else n - i) for r in range(3)] for i, t in enumerate(state.order)}
        rows.append("Draft order (snake), with each GM's first three pick numbers: " + "; ".join(
            f"{state.team_label(t)} #{', #'.join(str(x) for x in slots[t])}" for t in state.order))
    for t in state.teams.values():
        per = t.persona or {}
        if t.is_bot:
            rows.append("the robot (Autodraft): the league's control bot; it never talks, and nobody knows how it picks")
        else:
            rows.append(f"{state.team_label(t.id)} ({t.lab}): {per.get('franchise_name') or 'no franchise yet'}; "
                        f"picked the {per.get('cup_pick') or '?'} to win the Cup")
    from gmbench.agent.briefing import cup_counts
    picks = cup_counts(state)
    if picks:
        rows.append("Stanley Cup picks at the table (13 AI GMs): " + picks)
    return "\n".join(rows)
