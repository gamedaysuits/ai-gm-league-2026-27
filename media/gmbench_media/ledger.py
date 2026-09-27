"""Read-only view of a league run: ledger events + league.yaml + fabrics, shaped for the show.

Nothing here writes to runs/. The ledger is the source of truth; exports/ are only used for
optional extras (history markdown for the "previously on" segment).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ROOT, run_dir

# Neutral fallback colours for franchises without a Media Day card (never third-party marks).
FALLBACK_COLORS = {
    "autodraft": ("#2B3245", "#9AA6BF"),
}
FALLBACK_PALETTE = [("#1B2A6B", "#4770DB"), ("#243056", "#8FA7E8"), ("#1D2B4F", "#EFF0F5")]


@dataclass
class Team:
    id: str
    display: str  # model display name, e.g. "Claude Opus 5.5"
    lab: str
    model: str | None
    persona: dict | None = None
    persona_seq: int | None = None

    @property
    def is_bot(self) -> bool:
        return self.model is None or self.id == "autodraft"

    @property
    def has_persona(self) -> bool:
        return bool(self.persona)

    def p(self, key: str, default: Any = None) -> Any:
        return (self.persona or {}).get(key, default)

    @property
    def gm_name(self) -> str:
        if self.has_persona and self.p("gm_name"):
            return self.p("gm_name")
        if self.is_bot:
            return "Autodraft"
        return self.display

    @property
    def franchise_name(self) -> str:
        if self.has_persona and self.p("franchise_name"):
            return self.p("franchise_name")
        if self.is_bot:
            return "Autodraft (control bot)"
        return self.display

    @property
    def city(self) -> str | None:
        return self.p("franchise_city")

    @property
    def nickname(self) -> str:
        if self.has_persona and self.p("franchise_nickname"):
            return self.p("franchise_nickname")
        return self.gm_name if self.is_bot else self.display

    @property
    def abbrev(self) -> str:
        if self.has_persona and self.p("franchise_abbrev"):
            return str(self.p("franchise_abbrev")).upper()[:4]
        if self.is_bot:
            return "BOT"
        letters = "".join(ch for ch in self.display.upper() if ch.isalnum())
        return letters[:3] or self.id[:3].upper()

    @property
    def colors(self) -> tuple[str, str]:
        if self.has_persona and self.p("primary_color") and self.p("secondary_color"):
            return self.p("primary_color"), self.p("secondary_color")
        if self.id in FALLBACK_COLORS:
            return FALLBACK_COLORS[self.id]
        return FALLBACK_PALETTE[sum(map(ord, self.id)) % len(FALLBACK_PALETTE)]

    @property
    def plural_name(self) -> bool:
        """Persona franchises read as plural ("the Lanterns select"); models/bots as singular."""
        return self.has_persona

    def initials(self) -> str:
        name = self.gm_name if self.has_persona else self.display
        # drop quoted nicknames: Qi 'The Abacus' Wen -> Qi Wen
        name = re.sub(r"""["'“‘][^"'“”‘’]*["'”’]""", " ", name)
        words = [w for w in name.replace("-", " ").split() if w[:1].isalpha()]
        if not words:
            return self.id[:2].upper()
        if len(words) == 1:
            return words[0][:2].upper()
        return (words[0][0] + words[-1][0]).upper()

    @property
    def short_gm(self) -> str:
        """GM name without the quoted nickname, for tight name plates."""
        if not self.has_persona:
            return self.gm_name
        return re.sub(r"\s+", " ", re.sub(r"""["'“‘][^"'“”‘’]*["'”’]""", " ", self.gm_name)).strip()


@dataclass
class Pick:
    seq: int
    pick_no: int
    round: int
    team: str
    player_id: int | None
    player_name: str
    nhl_team: str
    position: str
    group: str
    rationale: str
    projected_points: float | None
    range_80: list | None
    auto: dict | None
    on_air_call: str = ""  # the GM's own hype podium line (inline tags); public_rationale is for the record
    joke_logic: str = ""  # the GM's own one-sentence explanation of its joke (party-5+): judge input, never aired
    fact_check: str = ""  # ok | unverified | off (checked against the snapshot at submit time; "" in older runs)

    @property
    def airable(self) -> bool:
        """A line whose facts are unverified (or off) never airs."""
        return self.fact_check in ("", "ok")


@dataclass
class Say:
    seq: int
    team: str
    kind: str  # reaction | table_talk
    round: int | None
    pick_no: int | None
    cue: str
    line: str
    addressed_to: str | None
    joke_logic: str = ""
    fact_check: str = ""

    @property
    def airable(self) -> bool:
        return self.fact_check in ("", "ok")


@dataclass
class Report:
    """REPORT_SUBMITTED (post-draft): a GM's grades of every other draft + predicted standings."""
    seq: int
    team: str  # the grader
    kind: str
    grades: list[dict]  # [{team, grade, comment}]
    predicted_standings: list[str]


# consensus letter <- GPA (the export's scale: A+ 4.3 ... F 0); thresholds at the midpoints
GRADE_POINTS = {"A+": 4.3, "A": 4.0, "A-": 3.7, "B+": 3.3, "B": 3.0, "B-": 2.7, "C+": 2.3, "C": 2.0, "C-": 1.7,
                "D+": 1.3, "D": 1.0, "D-": 0.7, "F": 0.0}


def gpa_letter(gpa: float) -> str:
    best = min(GRADE_POINTS.items(), key=lambda kv: (abs(kv[1] - gpa), -kv[1]))
    return best[0]


@dataclass
class League:
    run: str
    name: str
    season: str
    first_puck_drop_utc: str | None
    timezone: str | None
    teams: dict[str, Team]
    order: list[str]
    order_event: dict
    order_seq: int | None
    picks: list[Pick]
    says: list[Say]
    head_seq: int
    rounds: int
    snapshot: dict = field(default_factory=dict)
    fabrics: dict[str, dict] = field(default_factory=dict)
    history: list[tuple[str, str]] = field(default_factory=list)  # (filename, markdown)
    reports: list[Report] = field(default_factory=list)
    _flags: dict | None = None
    _card: dict | None = None

    def team(self, tid: str) -> Team:
        return self.teams[tid]

    # -- draft value: reach / steal against the house projection (position-aware)
    def value_flags(self) -> dict[int, str]:
        """pick_no -> "reach" | "steal". A pick is judged within its position group (F/D/G): the player's rank
        in the snapshot pool by house projection vs how many of that group were gone when he was taken. Falls
        back to in-draft projections when the snapshot file is not on disk."""
        if self._flags is not None:
            return self._flags
        flags: dict[int, str] = {}
        rank: dict[int, int] = {}
        ref = str(self.snapshot.get("ref") or "")
        snap = ROOT / ref if ref and not ref.startswith("synthetic") else None
        if snap and snap.exists():
            pool = json.loads(snap.read_text()).get("players") or {}
            by_group: dict[str, list[tuple[float, int]]] = {}
            for pid, pp in (pool.items() if isinstance(pool, dict) else ((p.get("id"), p) for p in pool)):
                fp = (pp.get("projection") or {}).get("fantasy_points")
                if fp is not None:
                    by_group.setdefault(pp.get("group") or "?", []).append((float(fp), int(pid)))
            for lst in by_group.values():
                lst.sort(reverse=True)
                for i, (_, pid) in enumerate(lst):
                    rank[pid] = i + 1
        taken: dict[str, int] = {}
        for p in self.picks:
            g = p.group or "?"
            taken[g] = taken.get(g, 0) + 1
            if rank and p.player_id in rank:
                ti, rk = taken[g], rank[p.player_id]
                k = max(6, round(0.3 * ti))
                if rk - ti >= k:
                    flags[p.pick_no] = "reach"
                elif ti - rk >= k:
                    flags[p.pick_no] = "steal"
            elif not rank and p.projected_points is not None:
                later = [q for q in self.picks if p.pick_no < q.pick_no <= p.pick_no + 14 and q.projected_points is not None]
                earlier = [q for q in self.picks if p.pick_no - 14 <= q.pick_no < p.pick_no and q.projected_points is not None]
                if sum(1 for q in later if q.projected_points > p.projected_points + 3) >= 5:
                    flags[p.pick_no] = "reach"
                elif sum(1 for q in earlier if q.projected_points < p.projected_points - 3) >= 5:
                    flags[p.pick_no] = "steal"
        self._flags = flags
        return flags

    # -- post-draft report card (exports/grades.json from `gmbench export`, cross-checked with the ledger)
    def report_card(self) -> dict | None:
        """{teams: [{team, gpa, letter, spread, self_prediction, avg_predicted_finish, grades: [{from, grade, comment,
        seq}]}], source}. Comments are GM words: each carries the seq of the grader's REPORT_SUBMITTED event, and a
        comment that is not verbatim in the ledger is dropped (never aired)."""
        if self._card is not None:
            return self._card or None
        by_grader = {r.team: r for r in self.reports}
        ledger_comments = {(r.team, g.get("team")): (str(g.get("comment") or ""), str(g.get("grade") or ""), r.seq)
                           for r in self.reports for g in r.grades
                           if str(g.get("fact_check") or "").lower() in ("", "ok")}  # unverified / off: never aired
        path = run_dir(self.run) / "exports" / "grades.json"
        teams: list[dict] = []
        source = None
        if path.exists():
            data = json.loads(path.read_text())
            source = "exports/grades.json"
            for t in data.get("teams") or []:
                grades = []
                for g in t.get("grades") or []:
                    src = ledger_comments.get((g.get("from"), t.get("team")))
                    if not src or src[0] != g.get("comment") or src[1] != g.get("grade"):
                        continue
                    grades.append({"from": g["from"], "grade": g["grade"], "comment": g["comment"], "seq": src[2]})
                teams.append({"team": t["team"], "gpa": t.get("gpa"), "spread": t.get("spread"),
                              "self_prediction": t.get("self_prediction"),
                              "avg_predicted_finish": t.get("avg_predicted_finish"), "grades": grades})
        elif self.reports:
            source = "ledger"
            for tid in self.teams:
                grades = [{"from": r.team, "grade": str(g.get("grade")),
                           "comment": (str(g.get("comment") or "") if str(g.get("fact_check") or "").lower()
                                       in ("", "ok") else ""),  # an unverified / off comment never airs
                           "seq": r.seq} for r in self.reports for g in r.grades if g.get("team") == tid]
                pts = [GRADE_POINTS[g["grade"]] for g in grades if g["grade"] in GRADE_POINTS]
                others = [r.predicted_standings.index(tid) + 1 for r in self.reports
                          if r.team != tid and tid in r.predicted_standings]
                own = by_grader.get(tid)
                teams.append({"team": tid, "gpa": round(sum(pts) / len(pts), 2) if pts else None,
                              "spread": round(max(pts) - min(pts), 2) if pts else None,
                              "self_prediction": (own.predicted_standings.index(tid) + 1
                                                  if own and tid in own.predicted_standings else None),
                              "avg_predicted_finish": round(sum(others) / len(others), 1) if others else None,
                              "grades": grades})
        teams = [t for t in teams if t["team"] in self.teams and t.get("gpa") is not None]
        for t in teams:
            t["letter"] = gpa_letter(float(t["gpa"]))
        self._card = {"teams": teams, "source": source} if teams else {}
        return self._card or None

    def picks_in_round(self, rnd: int) -> list[Pick]:
        return [p for p in self.picks if p.round == rnd]

    def reactions_for(self, pick_no: int) -> list[Say]:
        return [s for s in self.says if s.kind == "reaction" and s.pick_no == pick_no]

    def comebacks_for(self, pick_no: int) -> list[Say]:
        """Fire-backs at the call of pick `pick_no`: SAY kind 'comeback' (addressed to the GM who ribbed them), and --
        for runs before comebacks existed -- a reaction aimed at the GM who just made that call."""
        p = next((x for x in self.picks if x.pick_no == pick_no), None)
        out = [s for s in self.says if s.pick_no == pick_no and s.kind == "comeback"]
        if p is not None:
            out += [s for s in self.says if s.pick_no == pick_no and s.kind == "reaction" and s.addressed_to == p.team
                    and s.team != p.team]
        return sorted(out, key=lambda s: s.seq)

    def pick_for_say(self, s: Say) -> int | None:
        """The pick a SAY belongs to: its own pick_no, or (table talk logged per round) the latest pick before it."""
        if s.pick_no is not None:
            return int(s.pick_no)
        before = [p for p in self.picks if p.seq < s.seq]
        return max(before, key=lambda p: p.seq).pick_no if before else None

    def mentioned_team(self, text: str, exclude: set[str] | None = None) -> str | None:
        """The GM a line names by nickname ('Moon', 'Booksie'), first mention wins."""
        best = None
        low = " " + re.sub(r"[^a-z0-9' ]+", " ", text.lower()) + " "
        for tid, t in self.teams.items():
            if exclude and tid in exclude:
                continue
            for nick in re.findall(r"""['"“‘]([^'"”’]+)['"”’]""", t.gm_name or ""):
                i = low.find(" " + nick.lower() + " ")
                if i >= 0 and (best is None or i < best[0]):
                    best = (i, tid)
        return best[1] if best else None

    def table_talk(self, rnd: int) -> list[Say]:
        return [s for s in self.says if s.kind == "table_talk" and s.round == rnd]

    def is_snake(self) -> bool:
        r1 = [p.team for p in self.picks_in_round(1)]
        r2 = [p.team for p in self.picks_in_round(2)]
        return bool(r1) and bool(r2) and r2 == list(reversed(r1))


def _read_events(path: Path) -> list[dict]:
    events = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e["seq"])
    return events


def load_history(run: str) -> list[tuple[str, str]]:
    found = []
    for base in (run_dir(run) / "exports" / "history", ROOT / "exports" / "history"):
        for name in ("playoffs-2026.md", "regular-2025-26.md"):
            f = base / name
            if f.exists() and name not in {n for n, _ in found}:
                found.append((name, f.read_text()))
    return found


def load_league(run: str) -> League:
    rdir = run_dir(run)
    ledger = rdir / "ledger" / "league.jsonl"
    if not ledger.exists():
        raise SystemExit(f"ledger not found: {ledger}")
    events = _read_events(ledger)

    cfg_yaml = yaml.safe_load((ROOT / "league.yaml").read_text())
    created = next((e for e in events if e["type"] == "LEAGUE_CREATED"), None)
    team_cfgs = (created or {}).get("payload", {}).get("teams") or cfg_yaml.get("teams", [])
    league_meta = (created or {}).get("payload", {}).get("league") or cfg_yaml.get("league", {})
    rules = (created or {}).get("payload", {}).get("rules") or cfg_yaml.get("rules", {})

    teams: dict[str, Team] = {}
    for t in team_cfgs:
        teams[t["id"]] = Team(id=t["id"], display=t.get("display") or t["id"], lab=t.get("lab") or "",
                              model=t.get("model"))

    order_event: dict = {}
    order_seq = None
    picks: list[Pick] = []
    says: list[Say] = []
    reports: list[Report] = []
    snapshot: dict = {}
    for e in events:
        et, pl = e["type"], e.get("payload") or {}
        if et == "REPORT_SUBMITTED":
            grader = pl.get("team") or str(e.get("actor") or "").split(":")[-1]
            if grader in teams:
                reports.append(Report(seq=e["seq"], team=grader, kind=str(pl.get("kind") or ""),
                                      grades=list(pl.get("grades") or []),
                                      predicted_standings=list(pl.get("predicted_standings") or [])))
            continue
        if et == "PERSONA_CREATED":
            tid = pl.get("team")
            if tid in teams and pl.get("persona"):
                teams[tid].persona = pl["persona"]
                teams[tid].persona_seq = e["seq"]
        elif et == "DRAFT_ORDER_SET":
            order_event, order_seq = pl, e["seq"]
        elif et == "SNAPSHOT_TAKEN":
            snapshot = pl
        elif et == "DRAFT_PICK":
            picks.append(Pick(
                seq=e["seq"], pick_no=int(pl["pick_no"]), round=int(pl["round"]), team=pl["team"],
                player_id=pl.get("player_id"), player_name=str(pl.get("player_name") or "").strip(),
                nhl_team=str(pl.get("nhl_team") or "").upper(), position=str(pl.get("position") or ""),
                group=str(pl.get("group") or ""), rationale=str(pl.get("public_rationale") or "").strip(),
                projected_points=pl.get("projected_points"), range_80=pl.get("range_80"), auto=pl.get("auto"),
                on_air_call=str(pl.get("on_air_call") or "").strip(),
                joke_logic=str(pl.get("joke_logic") or "").strip(),
                fact_check=str(pl.get("fact_check") or "").strip().lower(),
            ))
        elif et == "SAY":
            line = str(pl.get("line") or "").strip()
            if not line:
                continue
            says.append(Say(
                seq=e["seq"], team=pl.get("team"), kind=pl.get("kind") or "reaction", round=pl.get("round"),
                pick_no=pl.get("pick_no"), cue=str(pl.get("cue") or ""), line=line,
                addressed_to=pl.get("addressed_to"), joke_logic=str(pl.get("joke_logic") or "").strip(),
                fact_check=str(pl.get("fact_check") or "").strip().lower(),
            ))
    picks.sort(key=lambda p: p.pick_no)
    order = list(order_event.get("order") or [p.team for p in picks if p.round == 1] or list(teams))

    fabrics: dict[str, dict] = {}
    fpath = ROOT / "data" / "fabrics.json"
    if fpath.exists():
        for f in json.loads(fpath.read_text()):
            fabrics[f["id"]] = f

    return League(
        run=run, name=league_meta.get("name") or "GM-Bench", season=str(league_meta.get("season") or ""),
        first_puck_drop_utc=league_meta.get("first_puck_drop_utc"), timezone=league_meta.get("timezone"), teams=teams, order=order,
        order_event=order_event, order_seq=order_seq, picks=picks, says=says,
        head_seq=events[-1]["seq"] if events else 0, rounds=int(rules.get("rounds") or 14),
        snapshot=snapshot, fabrics=fabrics, history=load_history(run), reports=reports,
    )
