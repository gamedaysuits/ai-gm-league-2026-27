"""Comedy judge: SELECTION is our quality control -- we never rewrite a word.

Every sentence of every GM line (calls, comebacks, reactions, table talk) is scored by a PANEL of non-league models
(never a model that plays in the league):

  means         what the sentence claims, in plain words (forces the judge to find the point; a log, never aired)
  makes_sense   yes / no   a hockey fan gets it instantly: the comparison maps onto the actual pick or situation.
                           Joke-SHAPED nonsense fails ("building from the back out -- my dad said that about the
                           outhouse too")
  funny         0-3
  factually_ok  yes / no / unsure   claims about players and NHL teams, checked against the league's data snapshot
                           (current team, age, the last three seasons)
  reason        one line (a log, never aired)

Panel: the stricter makes_sense and factually_ok win; funny is the panel mean. Verdicts are cached per line and
logged to media/out/review/<run>/judge.md.

GATE (shorts and the main cut): a PUNCH / BUTTON airs only with makes_sense = yes, factually_ok != no, funny >= 2 and
no owner veto (media/out/review/<run>/veto.txt). The complete show keeps everything.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import tags as T
from .paths import OUT, ROOT, cache_dir
from .tighten import split_parts

PROMPT_V = 2  # v3 (explicit pun / second-step rules) scored worse on the calibration set: 10/14
# The panel, from `judge calibrate` on the producer's read of party-4 (+ party-3's outhouse line), prompt v2:
#   minimax-m3 10/14 alone, nova-2-lite 10/14, mistral-medium-3.5 10/14, command-a-plus 5/14 (fails everything),
#   nemotron-3-ultra 8/14 (v1; slow); nova-premier is end-of-life. The pair minimax-m3 + nova-2-lite with the
#   stricter makes_sense agrees on 12/14 (86%): it misses 'Stettler still needs a jump' and the outhouse line.
PANEL = ["minimax/minimax-m3", "amazon/nova-2-lite-v1"]
FUNNY_MIN = 2.0
BATCH = 6

NHL = {"ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres", "CAR": "Carolina Hurricanes",
       "CBJ": "Columbus Blue Jackets", "CGY": "Calgary Flames", "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche",
       "DAL": "Dallas Stars", "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
       "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens", "NJD": "New Jersey Devils",
       "NSH": "Nashville Predators", "NYI": "New York Islanders", "NYR": "New York Rangers", "OTT": "Ottawa Senators",
       "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "SEA": "Seattle Kraken", "SJS": "San Jose Sharks",
       "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs", "UTA": "Utah Mammoth",
       "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights", "WPG": "Winnipeg Jets", "WSH": "Washington Capitals"}

PROMPT = """You are the COMEDY JUDGE for "Draft Night": AI models play small-town Alberta hockey GMs at a fantasy-hockey
draft party and chirp each other between picks. Nothing is ever rewritten -- we only choose which sentences air -- so
you are the strict editor who keeps joke-SHAPED sentences that don't land off the air.

Know your writers: they are language models, and they often produce sentences that SOUND like a chirp -- vivid rural
imagery, a callback, a pun -- but don't actually say anything a fan would get on first hearing. Expect about a third
of the jokes you see to fail. Do not rescue a line by inventing a clever reading for it: if YOU had to work out what
it means, the room won't get it.

For EVERY numbered sentence of every LINE return:
  "image": the picture or comparison the sentence uses, literally ("-" for a plain statement).
  "stands_for": what each part of that image stands for in the real situation (the player, the pick, the GM, a
      stat). Write "?" for any part that stands for nothing real.
  "makes_sense": "yes" only if every part of the image stands for something real AND a hockey fan would get the point
      instantly. "no" if any of these holds:
        - a part of the image stands for nothing real ("?" above);
        - the sentence re-uses someone else's image (a combine, a grain bin, a fence) but gives it a new meaning or a
          new detail that maps to nothing in the hockey situation;
        - it mixes its own metaphor (sets up an engine, pays off with something that has nothing to do with engines);
        - it is a pun or word association on someone's phrase that doesn't also make a pointed claim about the pick;
        - reasonable listeners would come away with different meanings.
      Plain statements (a pick announcement, a stat, a greeting) are "yes".
  "funny": 0 = not a joke (announcement, stat, filler) / 1 = joke-shaped but mild, strained or confusing / 2 = a real
      laugh from a room of hockey fans: a specific, surprising comparison that fits / 3 = big laugh. A sentence that
      makes no sense is at most 1.
  "factually_ok": "no" if a claim about a player or an NHL team is contradicted by DATA: a present-tense claim ties a
      player to any team but his CURRENT one in DATA (he "drives", "plays for", "runs" another team), an age or stat
      that is way off, a trophy or playoff run that did not happen. "unsure" if DATA can't confirm a specific factual
      claim. "yes" otherwise. Persona lore (hometowns, jobs, the GMs' own fandom and history), opinions and obvious
      hyperbole are fine.
  "reason": one short line.
A writer's note on a joke (JOKE LOGIC) is context only: judge what the audience hears, without the note.

EXAMPLES (not from this draft):
  after a GM drafts a 35-year-old: "That's buying a used truck because the radio still works." -> image: a used truck
  bought for its radio; stands_for: the truck = the old player, the radio = his one remaining skill; makes_sense yes;
  funny 2.
  "Your winger skates so fast the Zamboni files a noise complaint." -> image: a Zamboni complaining about noise;
  stands_for: the Zamboni = ?; makes_sense no; funny 1.

Return ONLY JSON: {"lines": {"<line id>": [{"i": 0, "image": "...", "stands_for": "...", "makes_sense": "yes",
"funny": 2, "factually_ok": "yes", "reason": "..."}, ...]}} -- one entry per sentence, in order.
"""


# ----------------------------------------------------------------------------------------------- the lines to judge
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", T.strip(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class Item:
    id: str  # "seq<N>"
    seq: int
    team: str
    kind: str  # on_air_call | comeback | reaction | table_talk
    text: str  # the ledger text (tags inline)
    parts: list[str]
    pick_no: int | None
    to: str | None
    player: str | None  # drafted on this line
    context: list[str] = field(default_factory=list)  # what it answers
    logic: str = ""
    players: set[int] = field(default_factory=set)


def _who(L, tid: str | None) -> str:
    t = L.teams.get(tid or "")
    if not t:
        return "?"
    if t.is_bot:
        return "the control bot (Autodraft)"
    return f"{t.gm_name} ({t.display})"


def items_for(L) -> list[Item]:
    """Every GM line of the run, in ledger order, with what it answers."""
    picks = sorted(L.picks, key=lambda p: p.seq)
    by_no = {p.pick_no: p for p in picks}
    events: list[tuple[int, str, object]] = [(p.seq, "pick", p) for p in picks] + [(s.seq, "say", s) for s in L.says]
    events.sort(key=lambda x: x[0])
    last_by: dict[str, tuple[int, str]] = {}  # team -> (seq, text) of its latest line
    out: list[Item] = []
    for seq, kind, obj in events:
        if kind == "pick":
            p = obj
            text = (p.on_air_call or "").strip()
            if text and not L.teams[p.team].is_bot:
                prev = by_no.get(p.pick_no - 1)
                ctx = []
                if prev is not None:
                    ctx.append(f"previous pick #{prev.pick_no}: {_who(L, prev.team)} took {prev.player_name} "
                               f"({prev.nhl_team})")
                    if prev.on_air_call:
                        ctx.append(f"{_who(L, prev.team)} said: \"{T.strip(prev.on_air_call)}\"")
                    between = [s for s in L.says if prev.seq < s.seq < p.seq]
                    for s in between[-2:]:
                        ctx.append(f"{_who(L, s.team)} said: \"{T.strip(s.line)}\"")
                to = prev.team if prev is not None and prev.team != p.team else None
                out.append(Item(f"seq{seq}", seq, p.team, "on_air_call", text, split_parts(text), p.pick_no, to,
                                f"{p.player_name} ({p.nhl_team})", ctx, p.joke_logic,
                                {x for x in (p.player_id, prev.player_id if prev else None) if x}))
            last_by[p.team] = (seq, text)
        else:
            s = obj
            pk = by_no.get(s.pick_no) if s.pick_no else None
            ctx = []
            if pk is not None:
                ctx.append(f"pick #{pk.pick_no}: {_who(L, pk.team)} took {pk.player_name} ({pk.nhl_team})")
                if pk.on_air_call and pk.seq < seq:
                    ctx.append(f"{_who(L, pk.team)} said: \"{T.strip(pk.on_air_call)}\"")
            if s.addressed_to and s.addressed_to in last_by and (not pk or s.addressed_to != pk.team):
                ctx.append(f"{_who(L, s.addressed_to)} said: \"{T.strip(last_by[s.addressed_to][1])}\"")
            out.append(Item(f"seq{seq}", seq, s.team, s.kind, s.line, split_parts(s.line), s.pick_no, s.addressed_to,
                            None, ctx, s.joke_logic, {pk.player_id} if pk and pk.player_id else set()))
            last_by[s.team] = (seq, s.line)
    for r in L.reports:  # post-draft grades: the roast and Delusion shorts air these comments
        for g in r.grades:
            text = str(g.get("comment") or "").strip()
            team = g.get("team")
            if not text or team not in L.teams or str(g.get("fact_check") or "").lower() not in ("", "ok"):
                continue
            tp = [p for p in L.picks if p.team == team][:4]
            ctx = [f"{_who(L, r.team)} grades the draft of {_who(L, team)}: {g.get('grade')}"]
            if tp:
                ctx.append("their first picks: " + ", ".join(f"{p.player_name} ({p.nhl_team})" for p in tp))
            out.append(Item(f"seq{r.seq}-{team}", r.seq, r.team, "grade_comment", text, split_parts(text), None, team,
                            None, ctx, "", {p.player_id for p in tp if p.player_id}))
    return out


# ----------------------------------------------------------------------------------------------- facts (the snapshot)
class Facts:
    def __init__(self, L):
        ref = str((L.snapshot or {}).get("ref") or "")
        path = ROOT / ref if ref else None
        self.players: dict[int, dict] = {}
        if path and path.exists():
            pool = json.loads(path.read_text()).get("players") or {}
            for pid, pp in (pool.items() if isinstance(pool, dict) else ((p.get("id"), p) for p in pool)):
                self.players[int(pid)] = pp
        self.full: dict[str, int] = {}
        self.last: dict[str, list[int]] = {}
        for pid, pp in self.players.items():
            self.full[_norm(pp.get("name") or "")] = pid
            self.last.setdefault(_norm(pp.get("last_name") or ""), []).append(pid)
        self.drafted = {p.player_id for p in L.picks if p.player_id}

    def mentioned(self, text: str) -> set[int]:
        t = " " + _norm(text) + " "
        found = {pid for name, pid in self.full.items() if name and f" {name} " in t}
        for w in set(t.split()):
            if len(w) < 4:
                continue
            ids = self.last.get(w) or []
            if not ids or any(ids_ in found for ids_ in ids):
                continue
            drafted = [i for i in ids if i in self.drafted]
            if len(drafted) == 1:
                found.add(drafted[0])
            elif len(ids) == 1:
                found.add(ids[0])
        return found

    def line(self, pid: int) -> str | None:
        pp = self.players.get(pid)
        if not pp:
            return None
        team = pp.get("team") or "?"
        out = [f"{pp.get('name')}: {pp.get('position')}, current NHL team {team} ({NHL.get(team, team)}), "
               f"age {float(pp.get('age') or 0):.0f}"]
        if pp.get("injury"):
            out.append(f"injury: {pp['injury']}")
        for s in (pp.get("seasons") or [])[:3]:
            yr = str(s.get("season") or "")
            yr = f"{yr[:4]}-{yr[6:]}" if len(yr) == 8 else yr
            if pp.get("group") == "G" or s.get("wins") is not None:
                out.append(f"{yr} {s.get('teams')}: {s.get('gp')} GP, {s.get('wins')} W, GAA {s.get('gaa')}, "
                           f"SV% {s.get('save_pct')}")
            else:
                out.append(f"{yr} {s.get('teams')}: {s.get('gp')} GP, {s.get('goals')} G, {s.get('assists')} A, "
                           f"{s.get('points')} P")
        return "; ".join(out)


def cast_block(L) -> str:
    rows = []
    for tid, t in L.teams.items():
        if t.is_bot:
            rows.append(f"- {tid}: the control bot (Autodraft), a dumb auto-drafter every GM has to beat")
            continue
        nick = re.findall(r"'([^']+)'", t.gm_name or "")
        nick_s = f' (called "{nick[0]}")' if nick else ""
        rows.append(f"- {tid}: {t.gm_name}{nick_s}, GM of the {t.franchise_name}; "
                    f"hometown {t.p('hometown') or t.city or '?'}; favourite NHL team {t.p('favorite_nhl_team') or '?'}; "
                    f"played by the AI model {t.display}")
    return "\n".join(rows)


# ----------------------------------------------------------------------------------------------- asking the panel
def _key(model: str, it: Item) -> str:
    return hashlib.sha1(json.dumps({"v": PROMPT_V, "m": model, "t": it.text, "c": it.context, "k": it.kind,
                                    "to": it.to, "p": it.player, "l": it.logic}, sort_keys=True).encode()).hexdigest()[:24]


def _block(L, it: Item) -> str:
    head = f"LINE {it.id}: {_who(L, it.team)}, {it.kind.replace('_', ' ')}"
    if it.to:
        head += f", aimed at {_who(L, it.to)}"
    if it.player:
        head += f"; drafting {it.player}"
    rows = [head]
    for c in it.context:
        rows.append(f"  context: {c}")
    if it.logic:
        rows.append(f"  JOKE LOGIC (writer's note, not aired): {it.logic}")
    for i, p in enumerate(it.parts):
        rows.append(f"  {i}. {T.strip(p)}")
    return "\n".join(rows)


def _ask(L, facts: Facts, batch: list[Item], model: str, log=print) -> dict:
    try:
        import httpx

        from .tts import load_env
    except Exception:  # noqa: BLE001
        return {}
    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        log("judge: no OPENROUTER_API_KEY")
        return {}
    pids: set[int] = set()
    for it in batch:
        pids |= it.players | facts.mentioned(it.text) | {x for c in it.context for x in facts.mentioned(c)}
    data = [x for x in (facts.line(pid) for pid in sorted(pids)) if x]
    prompt = (PROMPT + "\nCAST (the GMs at the party):\n" + cast_block(L) + "\n\nDATA (the league's player snapshot):\n"
              + ("\n".join(f"- {d}" for d in data) or "- (none)") + "\n\n" + "\n\n".join(_block(L, it) for it in batch))
    for attempt in range(4):
        if attempt:
            time.sleep(4 * attempt)
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=300,
                           json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                 "max_tokens": 12000, "temperature": 0},
                           headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
        except Exception as e:  # noqa: BLE001
            log(f"judge: {model}: {type(e).__name__}")
            continue
        if r.status_code != 200:
            log(f"judge: {model}: HTTP {r.status_code}")
            if r.status_code in (400, 401, 402, 403, 404):
                return {}
            continue
        body = r.json()
        txt = (body["choices"][0]["message"].get("content") or "").strip()
        got = _lenient_json(txt)
        if got is None:
            log(f"judge: {model}: bad JSON")
            continue
        got["_usage"] = body.get("usage") or {}
        return got
    return {}


def _lenient_json(txt: str) -> dict | None:
    """The answer's JSON object, forgiving code fences, trailing commas and chatter around it."""
    txt = re.sub(r"```(?:json)?", "", txt)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    raw = m.group(0)
    for cand in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            got = json.loads(cand)
            return got if isinstance(got, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def _pick(lines: dict, it: Item, n: int):
    """A line's answer, whatever the judge called it ("seq74", "74", "LINE seq74")."""
    if not isinstance(lines, dict):
        return None
    for k in (it.id, str(it.seq), f"LINE {it.id}", f"line {it.id}"):
        if k in lines:
            return lines[k]
    if n == 1 and len(lines) == 1:
        return next(iter(lines.values()))
    return None


def _clean(rows, it: Item) -> list[dict] | None:
    if not isinstance(rows, list):
        return None
    by_i = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        ms = str(r.get("makes_sense") or "").strip().lower()
        fo = str(r.get("factually_ok") or "").strip().lower()
        try:
            fu = max(0, min(3, int(round(float(r.get("funny"))))))
        except (TypeError, ValueError):
            continue
        means = r.get("means") or " / ".join(str(r.get(k) or "") for k in ("image", "stands_for") if r.get(k))
        by_i[i] = {"i": i, "means": str(means)[:220], "makes_sense": "yes" if ms.startswith("y") else "no",
                   "funny": fu, "factually_ok": fo if fo in ("yes", "no", "unsure") else "unsure",
                   "reason": str(r.get("reason") or "")[:200]}
    if len(by_i) < len(it.parts) or any(i not in by_i for i in range(len(it.parts))):
        return None
    return [by_i[i] for i in range(len(it.parts))]


def league_prefixes(L) -> set[str]:
    return {t.model.split("/")[0] for t in L.teams.values() if t.model}


def judge_items(L, items: list[Item], models: list[str] | None = None, log=print, use_llm: bool = True
                ) -> dict[str, dict[str, list[dict]]]:
    """{model: {item id: [verdict per sentence]}} from the cache, asking only for what's missing."""
    models = models or PANEL
    bad = [m for m in models if m.split("/")[0] in league_prefixes(L)]
    if bad:
        raise SystemExit(f"judge: {bad} play in this league; judges must be non-league models")
    from concurrent.futures import ThreadPoolExecutor
    facts = Facts(L)
    cdir = cache_dir("judge")
    out: dict[str, dict[str, list[dict]]] = {m: {} for m in models}
    jobs = []
    for model in models:
        todo = []
        for it in items:
            f = cdir / f"{_key(model, it)}.json"
            if f.exists():
                out[model][it.id] = json.loads(f.read_text())["verdicts"]
            else:
                todo.append(it)
        if todo and use_llm:
            jobs += [(model, todo[k:k + BATCH]) for k in range(0, len(todo), BATCH)]

    def run(job):
        model, batch = job
        t0 = time.time()
        ans = _ask(L, facts, batch, model, log)
        lines = (ans or {}).get("lines") or {}
        done = {}
        for it in batch:
            rows = _clean(_pick(lines, it, len(batch)), it)
            if rows is None and len(batch) > 1:  # a batch answer that skipped a line: ask for it alone
                ans1 = _ask(L, facts, [it], model, log)
                rows = _clean(_pick((ans1 or {}).get("lines") or {}, it, 1), it)
            if rows is None:
                continue
            done[it.id] = rows
            (cdir / f"{_key(model, it)}.json").write_text(json.dumps(
                {"model": model, "v": PROMPT_V, "id": it.id, "text": it.text, "verdicts": rows,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1, ensure_ascii=False))
        log(f"judge: {model}: {len(done)}/{len(batch)} lines judged ({time.time() - t0:.0f}s)")
        return model, done

    if jobs:
        with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
            for model, done in ex.map(run, jobs):
                out[model].update(done)
    return out


# ----------------------------------------------------------------------------------------------- the panel's verdict
def combine(votes: dict[str, dict]) -> dict:
    """Stricter makes_sense and factually_ok; funny = the panel mean."""
    ms = "no" if any(v["makes_sense"] == "no" for v in votes.values()) else "yes"
    fos = [v["factually_ok"] for v in votes.values()]
    fo = "no" if "no" in fos else ("unsure" if "unsure" in fos else "yes")
    fu = round(sum(v["funny"] for v in votes.values()) / max(1, len(votes)), 2)
    return {"makes_sense": ms, "factually_ok": fo, "funny": fu,
            "pass": ms == "yes" and fo != "no" and fu >= FUNNY_MIN}


def review_dir(run: str) -> Path:
    d = OUT / "review" / run
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_veto(run: str) -> tuple[set[str], set[int], list[str]]:
    """media/out/review/<run>/veto.txt: sentence ids ("72.0"), whole lines ("72") or quoted fragments ("outhouse")."""
    p = review_dir(run) / "veto.txt"
    if not p.exists():
        p.write_text("# Owner vetoes for this run: one per line. A sentence id from the shortlist (72.0), a whole\n"
                     "# line (72), or a quoted fragment (\"outhouse\"). Vetoed sentences never air; a vetoed call keeps\n"
                     "# only its pick. \"+72.0\" does the opposite: it airs a sentence the judges cut.\n")
    sids, lines, frags = set(), set(), []
    for raw in p.read_text().splitlines():
        s = raw.split("#", 1)[0].strip()
        if not s:
            continue
        if s.startswith("+"):
            continue  # an owner keep (load_keep)
        m = re.fullmatch(r"(?:seq)?(\d+(?:-[a-z0-9]+)?)\.(\d+)", s)
        if m:
            sids.add(f"{m.group(1)}.{int(m.group(2))}")
            continue
        m = re.fullmatch(r"(?:seq)?(\d+)", s)
        if m:
            lines.add(int(m.group(1)))
            continue
        frags.append(_norm(s.strip("\"'“”")))
    return sids, lines, [f for f in frags if f]


def load_keep(run: str) -> set[str]:
    """Owner overrides in veto.txt: "+72.0" airs sentence 72.0 even if the panel cut it (a fact the judges got wrong)."""
    p = review_dir(run) / "veto.txt"
    out = set()
    if p.exists():
        for raw in p.read_text().splitlines():
            s = raw.split("#", 1)[0].strip()
            m = re.fullmatch(r"\+\s*(?:seq)?(\d+(?:-[a-z0-9]+)?)\.(\d+)", s)
            if m:
                out.add(f"{m.group(1)}.{int(m.group(2))}")
    return out


class Judgement:
    """The run's verdicts: lookups by (ledger seq, sentence) for the edit."""

    def __init__(self, run: str, items: list[Item], votes: dict[str, dict[str, list[dict]]]):
        self.run = run
        self.items = {it.id: it for it in items}
        self.by_seq: dict[int, list[Item]] = {}
        for it in items:
            self.by_seq.setdefault(it.seq, []).append(it)
        self.models = list(votes)
        self.v: dict[str, dict] = {}  # "seq.i" (or "seq-team.i" for a grade comment) -> verdict
        vs, vl, vf = load_veto(run)
        self.keep_ids = load_keep(run)
        for it in items:
            for i, part in enumerate(it.parts):
                per = {m: votes[m][it.id][i] for m in votes if it.id in votes[m]}
                sid = f"{it.id[3:]}.{i}"
                vet = sid in vs or it.seq in vl or any(f in _norm(part) for f in vf)
                if not per:  # fail closed: a judge outage never lets a joke through unjudged (the owner can "+id" it)
                    forced = sid in self.keep_ids
                    self.v[sid] = {"sid": sid, "seq": it.seq, "i": i, "text": T.strip(part), "judged": False,
                                   "vetoed": vet, "forced": forced, "pass": forced and not vet}
                    continue
                c = combine(per)
                forced = sid in self.keep_ids  # the owner's call overrides the panel
                c["pass"] = (c["pass"] or forced) and not vet
                self.v[sid] = {"sid": sid, "seq": it.seq, "i": i, "text": T.strip(part), "judged": True,
                               "vetoed": vet, "forced": forced, **c, "votes": per}

    def find(self, seq: int, sentence: str) -> dict | None:
        """The verdict for a sentence of line `seq` (matched on its words; the short's text may carry respellings)."""
        n = _norm(sentence)
        its = self.by_seq.get(int(seq)) or []
        for it in its:
            for i, part in enumerate(it.parts):
                if _norm(part) == n:
                    return self.v.get(f"{it.id[3:]}.{i}")
        for it in its:
            for i, part in enumerate(it.parts):  # tolerant: one contains the other
                p = _norm(part)
                if p and (p in n or n in p):
                    return self.v.get(f"{it.id[3:]}.{i}")
        return None

    def comment_ok(self, seq: int, team: str) -> bool:
        """A grade comment airs whole: every sentence makes sense, no fact is off, nothing vetoed, a laugh in it."""
        it = self.items.get(f"seq{seq}-{team}")
        if not it:
            return True
        vs = [self.v.get(f"{it.id[3:]}.{i}") for i in range(len(it.parts))]
        vs = [v for v in vs if v and v.get("judged")]
        if not vs:
            return not any((self.v.get(f"{it.id[3:]}.{i}") or {}).get("vetoed") for i in range(len(it.parts)))
        if any(v.get("vetoed") for v in vs):
            return False
        if any(v.get("forced") for v in vs):
            return True
        return all(v["makes_sense"] == "yes" and v["factually_ok"] != "no" for v in vs) and \
            max(float(v["funny"]) for v in vs) >= FUNNY_MIN

    @property
    def unjudged(self) -> int:
        return sum(1 for v in self.v.values() if not v.get("judged"))

    def passes(self, seq: int, sentence: str) -> bool:
        v = self.find(seq, sentence)
        return True if v is None else bool(v["pass"])

    def score(self, seq: int, sentence: str) -> float:
        v = self.find(seq, sentence)
        return float(v.get("funny") or 0.0) if v and v.get("judged") else 0.0


def judge_run(L, log=print, use_llm: bool = True, models: list[str] | None = None) -> Judgement:
    items = items_for(L)
    votes = judge_items(L, items, models, log, use_llm)
    J = Judgement(L.run, items, votes)
    write_log(L, J)
    return J


def write_log(L, J: Judgement) -> Path:
    d = review_dir(L.run)
    rows = [f"# Comedy judge: {L.run}", "",
            f"Panel: {', '.join(J.models)} (non-league). Gate for the shorts and the main cut: makes_sense = yes (the "
            f"stricter judge), factually_ok != no, funny >= {FUNNY_MIN:g} (panel mean), no owner veto.", "",
            "| id | GM | sentence | sense | funny | facts | gate | why |", "|---|---|---|---|---|---|---|---|"]
    for it in sorted(J.items.values(), key=lambda x: (x.seq, x.id)):
        for i, _ in enumerate(it.parts):
            v = J.v[f"{it.id[3:]}.{i}"]
            if not v.get("judged"):
                continue
            why = " / ".join(f"{m.split('/')[-1]}: {x['reason']}" for m, x in v["votes"].items())
            gm = L.teams[it.team].gm_name if it.team in L.teams else it.team
            rows.append(f"| {v['sid']} | {gm} | {v['text'].replace('|', '/')} | {v['makes_sense']} | {v['funny']:g} | "
                        f"{v['factually_ok']} | {'VETO' if v['vetoed'] else ('PASS' if v['pass'] else 'fail')} | "
                        f"{why.replace('|', '/')} |")
    p = d / "judge.md"
    p.write_text("\n".join(rows) + "\n")
    (d / "judge.json").write_text(json.dumps({"run": L.run, "panel": J.models, "verdicts": J.v}, indent=1,
                                             ensure_ascii=False))
    return p


# ----------------------------------------------------------------------------------------------- calibration
CALIBRATION = [  # the producer's read of party-4 (+ party-3's outhouse line): (run, seq, fragment, expected)
    ("party-4", 39, "laminating a note that says the ice is cold", "work"),
    ("party-4", 62, "conversational as my toaster", "work"),
    ("party-4", 66, "fence before the cows found the highway", "work"),
    ("party-4", 68, "used combine with a cracked block", "work"),
    ("party-4", 79, "grain bin before you ve planted a crop", "work"),
    ("party-4", 91, "buddy waving from the fender", "work"),
    ("party-4", 97, "mackinnon still controls the radio", "work"),
    ("party-4", 54, "welding goggles", "fail"),
    ("party-4", 59, "stettler still needs a jump", "fail"),
    ("party-4", 85, "combine left the gate open", "fail"),
    ("party-4", 81, "grain bin s on fire in november", "fail"),
    ("party-3", 72, "my dad said that about the outhouse too", "fail"),
    ("party-4", 93, "necas drove carolina to a cup", "fact_no"),
    ("party-4", 93, "quinn hughes drives vancouver", "fact_no"),
]


def calibrate(models: list[str], log=print, use_llm: bool = True) -> dict:
    from .ledger import load_league
    res: dict[str, dict] = {}
    rows = []
    runs = sorted({r for r, *_ in CALIBRATION})
    per_run = {}
    for run in runs:
        L = load_league(run)
        items = items_for(L)
        want = {seq for r, seq, *_ in CALIBRATION if r == run}
        items = [it for it in items if run == "party-4" or it.seq in want]
        per_run[run] = (L, items, judge_items(L, items, models, log, use_llm))
    scores = {m: 0 for m in models + ["PANEL"]}
    for run, seq, frag, exp in CALIBRATION:
        L, items, votes = per_run[run]
        it = next(x for x in items if x.seq == seq)
        idx = next(i for i, p in enumerate(it.parts) if frag in _norm(p))
        per = {m: votes[m][it.id][idx] for m in models if it.id in votes[m]}
        row = [f"{run} {seq}.{idx}", frag, exp]
        for m in models + ["PANEL"]:
            if m == "PANEL":
                if len(per) < len(models):
                    row.append("?")
                    continue
                v = combine(per)
            else:
                if m not in per:
                    row.append("?")
                    continue
                v = {**per[m], **{"pass": per[m]["makes_sense"] == "yes" and per[m]["factually_ok"] != "no"
                                  and per[m]["funny"] >= FUNNY_MIN}}
            ok = (v["factually_ok"] == "no") if exp == "fact_no" else (v["pass"] == (exp == "work"))
            scores[m] += int(ok)
            row.append(("OK " if ok else "XX ") + f"s={v['makes_sense']} f={v['funny']:g} fact={v['factually_ok']}")
        rows.append(row)
    n = len(CALIBRATION)
    for r in rows:
        log(" | ".join(r))
    for m in models + ["PANEL"]:
        log(f"agreement {m}: {scores[m]}/{n} = {scores[m] / n:.0%}")
    return {"rows": rows, "scores": scores, "n": n}


def main(argv: list[str]) -> int:
    import argparse

    from .ledger import load_league
    ap = argparse.ArgumentParser(prog="gmbench_media.judge")
    ap.add_argument("cmd", choices=["run", "calibrate"])
    ap.add_argument("--run", default=None)
    ap.add_argument("--models", default=None, help="comma-separated OpenRouter ids (non-league)")
    ap.add_argument("--cached", action="store_true", help="calibrate: score what is cached, ask nothing")
    a = ap.parse_args(argv)
    models = a.models.split(",") if a.models else PANEL
    if a.cmd == "calibrate":
        calibrate(models, use_llm=not a.cached)
        return 0
    L = load_league(a.run)
    J = judge_run(L, models=models)
    n = sum(1 for v in J.v.values() if v.get("judged"))
    ok = sum(1 for v in J.v.values() if v.get("judged") and v["pass"])
    print(f"judge: {a.run}: {n} sentences judged, {ok} pass the gate -> {rel_path(review_dir(a.run) / 'judge.md')}")
    return 0


def rel_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
