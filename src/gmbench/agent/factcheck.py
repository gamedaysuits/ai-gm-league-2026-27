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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import httpx

from gmbench.data.models import Player, Snapshot


def think_time(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} milliseconds" if seconds >= 0.001 else "under a millisecond"
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def notable_think(pk: dict, picks: list[dict]) -> str | None:
    """How long a GM thought, but only when it stands out (so every chirp isn't the same stopwatch joke)."""
    think = pk.get("think") or {}
    if think.get("seconds") is None or (pk.get("auto") or {}).get("reason"):
        return None
    ai = [x for x in picks if (x.get("think") or {}).get("seconds") is not None and not (x.get("auto") or {}).get("reason")
          and int(x["pick_no"]) <= int(pk["pick_no"])]
    secs, lookups = float(think["seconds"]), int(think.get("lookups") or 0)
    tags = []
    if len(ai) >= 3 and secs >= max(float(x["think"]["seconds"]) for x in ai):
        tags.append("the longest think of the night so far")
    elif len(ai) >= 3 and secs <= min(float(x["think"]["seconds"]) for x in ai):
        tags.append("the quickest AI pick so far")
    if lookups == 0:
        tags.append("zero research lookups")
    elif lookups >= 15:
        tags.append(f"{lookups} research lookups")
    if not tags:
        return None
    return f"thought for {think_time(secs)}: " + ", ".join(tags)


def pick_facts(pk: dict, picks: list[dict] | None = None, full: bool = False) -> str:
    """What the room knows about a pick besides the player: when it stands out, how long the GM thought."""
    think = pk.get("think") or {}
    if (pk.get("auto") or {}).get("reason") == "control_bot":
        return ""  # the robot is opaque: nobody at the table sees how or how fast it picks
    bits = []
    note = notable_think(pk, picks if picks is not None else [pk])
    if full and think.get("seconds") is not None:  # checkers see every clock, so they can check any claim about one
        note = (f"thought for {think_time(float(think['seconds']))}, {int(think.get('lookups') or 0)} research lookups"
                + (f" ({note.split(': ', 1)[1]})" if note and ": " in note else ""))
    if note:
        bits.append(note)
    return "; ".join(bits)


def draft_board(state, snapshot: Snapshot, me: str | None = None, last: int = 16, full: bool = False) -> str:
    """Tonight's picks so far, numbered, so nobody says "two picks ago" about the wrong pick."""
    if not state.picks:
        return "No picks yet."
    rows = []
    for pk in state.picks[-last:]:
        who = state.team_label(pk["team"])
        pl = snapshot.players.get(int(pk["player_id"]))
        what = f"{pl.name} ({pl.team} {pl.position})" if pl else str(pk["player_id"])
        facts = pick_facts(pk, state.picks, full=full)
        rows.append(f"#{pk['pick_no']} {who}{' (you)' if pk['team'] == me else ''} took {what}"
                    + (f" · {facts}" if facts else ""))
    return f"Latest pick: #{state.picks[-1]['pick_no']}.\n" + "\n".join(rows)


def league_names(state) -> list[str]:
    """Every name a GM might say that belongs to the league itself: GM names and nicknames, franchises, hometowns."""
    out = []
    for t in state.teams.values():
        per = t.persona or {}
        out += [x for x in (state.team_label(t.id), per.get("gm_name"), per.get("franchise_name"), per.get("hometown")) if x]
    return out

CHECKERS = ("nvidia/nemotron-3-ultra-550b-a55b", "cohere/command-a")  # neither lab is in the league; both run, either flags
LEAGUE_FACTS = (
    "2026 Stanley Cup Final: the Carolina Hurricanes beat the Vegas Golden Knights 4-2. Vegas swept Colorado in the "
    "2026 Western Conference Final. Anaheim beat Edmonton in six games in the 2026 first round. The 2026-27 NHL "
    "regular season starts 2026-09-29."
)
SYSTEM = (
    "You fact-check one line of banter from a fantasy hockey draft party. Jokes, opinions, predictions, nicknames and "
    "exaggeration are fine. Flag ONLY concrete factual claims about real players or teams that CONTRADICT the facts "
    "given: current team, past teams and trades, age, games played, stats, Stanley Cup and playoff results. Stat "
    "comparisons between players are claims too: 'more points than', 'outscored', 'the guy with more goals' (right "
    "after another player was picked, that compares with him), 'led the league'. With no season named, a stat means "
    "2025-26. 'Rookie' means no earlier NHL regular season in the facts. 'On the board', 'left' or 'available' "
    "means the BEST STILL AVAILABLE lists given; check superlatives like 'best rate on the board' against them. Only "
    "flag a total, sum or average if it is clearly wrong after you add it up carefully. ALSO flag concrete claims "
    "the data can't support at all, because they may be out of date: captaincies ('Sharks captain'), linemates, "
    "contracts and salaries, awards and trophies (other than the league facts), injury history, trade rumours. "
    "Otherwise, if the facts don't cover a claim, don't flag it; never use your own memory of rosters or stats. NEVER flag: jokes, similes and figurative "
    "comparisons, judgement words (elite, great, a beauty, underrated), "
    "loose age and experience slang (kid, teenager, youngster, young gun, old man, graybeard, veteran), "
    "hyperbole, opinions, predictions, anything about the draft party itself, or references to the league's own GMs "
    "and fantasy franchises (listed below; they are not NHL teams or players, so 'Quinn Hughes for Rimbey' means the "
    "GM's fantasy team and is fine). The speaker may be announcing his own pick right now, so 'I'm taking X', 'give me "
    "X' and 'X for me' are never claims. Only check facts about real players and teams. Reply with ONLY JSON: "
    "{\"ok\": true} or "
    "{\"ok\": false, \"claims\": [{\"claim\": \"<the false claim, quoted exactly from the line>\", \"evidence\": "
    "\"<the fact it contradicts, copied word for word from the data above, or UNSUPPORTED for a claim the data can't "
    "support at all>\"}]}. If you can't copy a contradicting fact from the data, the claim is not contradicted: don't "
    "flag it. Don't write a correction."
)
JSON_OBJ = re.compile(r"\{.*\}", re.S)


def _age(p: Player, today: date) -> str:
    if p.birth_date:
        b = date.fromisoformat(str(p.birth_date)[:10])
        years = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        return f"age {years} (born {b.isoformat()})"
    return f"age {p.age:.0f}" if p.age else "age unknown"


def _line(s) -> str:
    extra = "".join(f", {v} {k}" for k, v in (("shots", s.shots), ("PP PTS", s.pp_points)) if v is not None)
    if s.toi_per_game_s:
        extra += f", {int(s.toi_per_game_s // 60)}:{int(s.toi_per_game_s % 60):02d} TOI/game"
    if s.pp_toi_per_game_s:
        extra += f", {int(s.pp_toi_per_game_s // 60)}:{int(s.pp_toi_per_game_s % 60):02d} PP TOI/game"
    stats = (f"{s.gp} gp, {s.wins or 0} W, {s.shutouts or 0} SO" if s.wins is not None or s.gs is not None
             else f"{s.gp} gp, {s.goals} G, {s.assists} A, {s.points} PTS{extra}")
    return f"{s.season[:4]}-{s.season[6:]}: {(s.teams or '?').replace(',', '→')} ({stats})"


def board_leaders(snapshot: Snapshot, taken, n: int = 5) -> str:
    """Who's best among players still available, by the 2025-26 numbers a GM might brag about ('best left on the
    board'), so the checker can verify superlatives about the board."""
    pool = [p for p in snapshot.players.values() if p.id not in taken and p.seasons and p.seasons[0].season == "20252026"]
    skaters = [p for p in pool if p.group != "G"]

    def top(rows, key, fmt, k=n):
        return ", ".join(fmt(p) for p in sorted(rows, key=key)[:k])
    s0 = lambda p: p.seasons[0]
    lines = [
        "points: " + top(skaters, lambda p: -s0(p).points, lambda p: f"{p.name} {s0(p).points}"),
        "points per game (40+ GP): " + top([p for p in skaters if s0(p).gp >= 40], lambda p: -s0(p).points / s0(p).gp,
                                           lambda p: f"{p.name} {s0(p).points / s0(p).gp:.2f}"),
        "goals: " + top(skaters, lambda p: -s0(p).goals, lambda p: f"{p.name} {s0(p).goals}"),
        "centres by points: " + top([p for p in skaters if p.position == "C"], lambda p: -s0(p).points,
                                    lambda p: f"{p.name} {s0(p).points}", 3),
        "wingers by points: " + top([p for p in skaters if p.position in ("L", "R")], lambda p: -s0(p).points,
                                    lambda p: f"{p.name} {s0(p).points}", 3),
        "defencemen by points: " + top([p for p in skaters if p.group == "D"], lambda p: -s0(p).points,
                                       lambda p: f"{p.name} {s0(p).points}", 3),
        "goalies by wins: " + top([p for p in pool if p.group == "G"], lambda p: -(s0(p).wins or 0),
                                  lambda p: f"{p.name} {s0(p).wins or 0}", 3),
    ]
    return "\n".join(lines)


def player_facts(p: Player, today: date) -> str:
    seasons = "; ".join(_line(s) for s in p.seasons)
    return f"{p.name}: {p.position}, {_age(p, today)}, current team {p.team}. Regular seasons: {seasons or 'none'}."


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9.\-→ ]", " ", s.lower()).split())


def grounded(item, context: str) -> str | None:
    """A flag counts only with evidence: a fact copied from the data it was given (checkers misread and remember
    stale rosters), or UNSUPPORTED for the kinds of claim the data can't back at all."""
    if isinstance(item, str):
        return item.split("->")[0].strip() or None
    if not isinstance(item, dict):
        return None
    claim = str(item.get("claim") or "").strip()
    evidence = str(item.get("evidence") or "").strip()
    if not claim:
        return None
    if evidence.upper().startswith("UNSUPPORTED"):
        return claim
    ev, ctx = _norm(evidence), _norm(context)
    if len(ev) >= 6 and ev in ctx:
        return claim
    numbers = re.findall(r"\d+(?:\.\d+)?", ev)
    words = [w for w in re.findall(r"[a-z]{3,}", ev)]
    if numbers and all(n in ctx for n in numbers) and words and sum(w in ctx for w in words) >= 0.6 * len(words):
        return claim
    return None


@dataclass
class FactChecker:
    snapshot: Snapshot
    today: date
    api_key: str | None = None
    league_names: list[str] = field(default_factory=list)  # GMs, franchises, hometowns: never NHL claims
    timeout_s: float = 25.0
    max_rejections: int = 3  # per session; after that the line is accepted and marked unverified
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
        ignore = {w.lower() for name in self.league_names for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", name)}
        for word in set(re.findall(r"[A-Z][A-Za-z'\-]{3,}", text)):
            key = re.sub(r"'s$", "", word).lower()
            if key in ignore:  # "Ellis" is the GM at the table, not NHL goalie Colten Ellis
                continue
            for p in self._by_last.get(key, []):
                found.setdefault(p.id, p)
        return list(found.values())[:16]

    def facts_for(self, text: str, extra: list[int] | None = None) -> str:
        """Our authoritative facts for whatever the line mentions: the correction a GM gets is always our data,
        never a checker's prose (checkers hallucinate corrections too)."""
        rows = [player_facts(p, self.today) for p in self.mentioned(text, extra)]
        return "\n".join(rows + [LEAGUE_FACTS])

    def check(self, text: str, extra: list[int] | None = None, board: str = "",
              recent: list[str] | None = None, leaders: str = "") -> list[str]:
        """Problems (empty if fine). Fails open: if every checker is unreachable the line is accepted."""
        players = self.mentioned(text, extra)
        rookie = re.search(r"\brookies?\b", text, re.I)
        if rookie and players and all(sum(s.gp for s in p.seasons) > 25 for p in players):
            return [rookie.group(0)]  # everyone this line can mean has real NHL seasons in our data: not a rookie
        if not (players or board) or not self.api_key:
            return []
        facts = "\n".join(player_facts(p, self.today) for p in players)
        league = "; ".join(self.league_names) or "(none)"
        user = (f"League facts: {LEAGUE_FACTS}\n\nThis league's own GMs and fantasy franchises (not NHL): {league}\n\n"
                f"Tonight's draft board:\n{board or '(not given)'}\n\nWhat was just said at the table (speaker: words):\n"
                + ("\n".join(recent or []) or "(nothing)") + f"\n\nPlayer facts:\n{facts or '(none)'}\n\n"
                + (f"BEST STILL AVAILABLE (2025-26 regular season):\n{leaders}\n\n" if leaders else "")
                + f"Line: {text}")
        def ask(model: str) -> list[str] | None:
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
                    return None
                verdict = json.loads(match.group(0))
                raw = verdict.get("claims") or verdict.get("problems") or []
                claims = [] if verdict.get("ok") else [c for c in (grounded(x, user) for x in raw) if c][:4]
                self.log.append({"model": model, "line": text, "problems": claims,
                                 "cost": (data.get("usage") or {}).get("cost")})
                return claims
            except (httpx.HTTPError, ValueError, KeyError):
                return None

        # Correctness first: both checkers run in parallel and either one can send the line back.
        with ThreadPoolExecutor(len(CHECKERS)) as pool:
            verdicts = [v for v in pool.map(ask, CHECKERS) if v is not None]
        if verdicts:
            seen, merged = set(), []
            for claims in verdicts:
                for c in claims:
                    if c.lower() not in seen:
                        seen.add(c.lower())
                        merged.append(c)
            return merged[:4]
        self.log.append({"model": None, "line": text, "problems": [], "error": "no checker reachable"})
        return []
