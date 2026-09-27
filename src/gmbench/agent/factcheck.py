"""Fact check for on-air lines: nothing false about a real player or team gets into the record.

The GMs' training memories are older than our data (Quinn Hughes was traded VAN→MIN in 2025-26; one GM still said
"Hughes drives Vancouver"). Before a pick call or a table line is accepted, a checker from a lab outside the league
compares its concrete claims with the snapshot: current team, past teams and trades, age, games played, playoff
results. Jokes, opinions, predictions and exaggeration pass. A contradiction goes back to the GM to fix. The checker
never judges from its own memory; it only compares the line with the facts we hand it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date

import httpx

from gmbench.data.models import Player, Snapshot

CHECKERS = ("cohere/command-a", "nvidia/nemotron-3-ultra-550b-a55b")  # neither lab is in the league
LEAGUE_FACTS = (
    "2026 Stanley Cup Final: the Carolina Hurricanes beat the Vegas Golden Knights 4-2. Vegas swept Colorado in the "
    "2026 Western Conference Final. Anaheim beat Edmonton in six games in the 2026 first round. The 2026-27 NHL "
    "regular season starts 2026-09-29."
)
SYSTEM = (
    "You fact-check one line of banter from a fantasy hockey draft party. Jokes, opinions, predictions, nicknames and "
    "exaggeration are fine. Flag ONLY concrete factual claims about real players or teams that CONTRADICT the facts "
    "given: current team, past teams and trades, age, games played, Stanley Cup and playoff results. If the facts "
    "don't cover a claim, don't flag it; never use your own memory of rosters. Reply with ONLY JSON: {\"ok\": true} or "
    "{\"ok\": false, \"claims\": [\"<the false claim, quoted exactly from the line>\"]}. Quote the claim only; don't "
    "write a correction."
)
JSON_OBJ = re.compile(r"\{.*\}", re.S)


def _age(p: Player, today: date) -> str:
    if p.birth_date:
        b = date.fromisoformat(str(p.birth_date)[:10])
        years = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        return f"age {years} (born {b.isoformat()})"
    return f"age {p.age:.0f}" if p.age else "age unknown"


def _line(s) -> str:
    stats = (f"{s.gp} gp, {s.wins or 0} W, {s.shutouts or 0} SO" if s.wins is not None or s.gs is not None
             else f"{s.gp} gp, {s.goals} G, {s.assists} A, {s.points} PTS")
    return f"{s.season[:4]}-{s.season[6:]}: {(s.teams or '?').replace(',', '→')} ({stats})"


def player_facts(p: Player, today: date) -> str:
    seasons = "; ".join(_line(s) for s in p.seasons)
    return f"{p.name}: {p.position}, {_age(p, today)}, current team {p.team}. Regular seasons: {seasons or 'none'}."


@dataclass
class FactChecker:
    snapshot: Snapshot
    today: date
    api_key: str | None = None
    timeout_s: float = 25.0
    max_rejections: int = 2  # per session; after that the line is accepted and marked unverified
    log: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY")
        self._by_last: dict[str, list[Player]] = {}
        for p in self.snapshot.players.values():
            self._by_last.setdefault(p.name.split()[-1].lower(), []).append(p)

    def mentioned(self, text: str, extra: list[int] | None = None) -> list[Player]:
        found: dict[int, Player] = {}
        low = text.lower()
        for pid in extra or []:
            if pid in self.snapshot.players:
                found[pid] = self.snapshot.players[pid]
        for p in self.snapshot.players.values():
            if p.name.lower() in low:
                found.setdefault(p.id, p)
        for word in set(re.findall(r"[A-Z][A-Za-z'\-]{3,}", text)):
            for p in self._by_last.get(re.sub(r"'s$", "", word).lower(), []):
                found.setdefault(p.id, p)
        return list(found.values())[:16]

    def facts_for(self, text: str, extra: list[int] | None = None) -> str:
        """Our authoritative facts for whatever the line mentions: the correction a GM gets is always our data,
        never a checker's prose (checkers hallucinate corrections too)."""
        rows = [player_facts(p, self.today) for p in self.mentioned(text, extra)]
        return "\n".join(rows + [LEAGUE_FACTS])

    def check(self, text: str, extra: list[int] | None = None) -> list[str]:
        """Problems (empty if fine). Fails open: if every checker is unreachable the line is accepted."""
        players = self.mentioned(text, extra)
        if not players or not self.api_key:
            return []
        facts = "\n".join(player_facts(p, self.today) for p in players)
        user = f"League facts: {LEAGUE_FACTS}\n\nPlayer facts:\n{facts}\n\nLine: {text}"
        for model in CHECKERS:
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
                    continue
                verdict = json.loads(match.group(0))
                raw = verdict.get("claims") or verdict.get("problems") or []
                problems = [] if verdict.get("ok") else [str(x).split("->")[0].strip() for x in raw][:4]
                self.log.append({"model": model, "line": text, "problems": problems,
                                 "cost": (data.get("usage") or {}).get("cost")})
                return problems
            except (httpx.HTTPError, ValueError, KeyError):
                continue
        self.log.append({"model": None, "line": text, "problems": [], "error": "no checker reachable"})
        return []
