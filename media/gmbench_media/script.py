"""Ledger -> rundown: the ordered show timeline, Game-7 edition.

Hard rule (unchanged): a GM's words are its own. Every GM line is copied verbatim from the ledger
(DRAFT_PICK.payload.on_air_call -- or public_rationale when there is no on-air call --, SAY.payload.line,
or the GM's own Media Day card fields). The GM's inline delivery tags are kept when they are on the
allowlist (tags.ALLOWED) and dropped otherwise. The only permitted word edit is trimming at a sentence
boundary, logged in rundown["trims"]. The host ("the Commissioner", Game-7 play-by-play energy) speaks
templated lines built from ledger facts; names and numbers are exact.

Timeline entry schema (rundown["timeline"][i]):
  id, speaker ("host" | team id), text (tts_text: words + allowlisted tags), display_text (words only),
  segment, kind, refs (ledger seqs), plus hints used downstream: focus, pick_no, round, card,
  addressed_to, gap_hint, pre_hold_s, own_words, est_s, energy.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from . import tags as T
from .ledger import League, Pick, Say, Team
from .nhl import club_spoken, number_words, ordinal_suffix, ordinal_words, pos_display, pos_spoken

TAG_RE = T.TAG_RE
UNREACHABLE_REASONS = {"bad_request", "timeout", "unreachable", "error", "provider_error", "no_response", "invalid"}
SENT_SPLIT = re.compile(r"(?<=[.!?…])[\"”’)\]]*\s+")


@dataclass
class Line:
    id: str
    segment: str
    kind: str
    speaker: str
    text: str
    display_text: str
    refs: list[int] = field(default_factory=list)
    focus: str | None = None
    pick_no: int | None = None
    round: int | None = None
    card: dict | None = None
    addressed_to: str | None = None
    gap_hint: str | None = None
    pre_hold_s: float = 0.0
    own_words: bool = False
    est_s: float = 0.0
    energy: float = 0.0
    beats: dict | None = None  # tight edit + comic timing: {source, parts, labels, keep, label_source}


# --------------------------------------------------------------------------- text helpers

def spoken_len(text: str) -> int:
    return len(T.strip(text))


def estimate_s(text: str, cps: float) -> float:
    return round(0.25 + spoken_len(text) / cps, 2)


def clean_gm_text(text: str) -> str:
    """Whitespace/markup normalisation only (no word changes); unknown tags dropped, allowlisted kept."""
    t = T.sanitize(re.sub(r"\s+", " ", text or "").strip())
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    words = T.strip(t)
    if len(words) >= 2 and words[0] in "\"“" and words[-1] in "\"”" and words.count('"') <= 2:
        t = t.replace(words[0], "", 1)[::-1].replace(words[-1], "", 1)[::-1].strip()
    return t


def trim_to_sentences(text: str, max_chars: int) -> tuple[str, str]:
    """Keep whole sentences (tags travel with their sentence) while the spoken length fits max_chars.
    Never cuts inside a sentence."""
    if spoken_len(text) <= max_chars:
        return text, ""
    parts = SENT_SPLIT.split(text)
    kept: list[str] = []
    for part in parts:
        cand = " ".join(kept + [part]).strip()
        if spoken_len(cand) > max_chars and kept:
            break
        kept.append(part)
        if spoken_len(cand) > max_chars:
            break
    out = " ".join(kept).strip()
    removed = T.strip(text)[len(T.strip(out)):].strip()
    return out, removed


def first_sentences(text: str, max_chars: int) -> str:
    return trim_to_sentences(text, max_chars)[0]


def think_times(L: League) -> dict | None:
    """{'robot': mean seconds per robot pick, 'ai': mean seconds per AI pick} from DRAFT_PICK think data (None
    unless the run has both)."""
    rb = [float(p.think["seconds"]) for p in L.picks if isinstance(p.think, dict) and p.think.get("seconds") is not None
          and L.teams.get(p.team) and L.teams[p.team].is_bot]
    ai = [float(p.think["seconds"]) for p in L.picks if isinstance(p.think, dict) and p.think.get("seconds") is not None
          and L.teams.get(p.team) and not L.teams[p.team].is_bot and not p.auto]
    if not rb or not ai:
        return None
    return {"robot": sum(rb) / len(rb), "ai": sum(ai) / len(ai)}


def spoken_duration(sec: float) -> str:
    """A true, rounded duration for the host: 'less than a millisecond', 'about forty seconds', 'about two minutes'."""
    if sec < 0.0005:
        return "less than a millisecond"
    if sec < 0.0015:
        return "about a millisecond"
    if sec < 1:
        return f"about {number_words(round(sec * 1000))} milliseconds"
    if sec < 1.5:
        return "about a second"
    if sec < 90:
        return f"about {number_words(round(sec))} seconds"
    m = sec / 60.0
    if m < 1.75:
        return "about a minute and a half"
    return f"about {number_words(round(m))} minutes"


def display_duration(sec: float) -> str:
    if sec < 0.0005:
        return "<1 ms"
    if sec < 1:
        return f"~{max(1, round(sec * 1000))} ms"
    if sec < 90:
        return f"~{round(sec)} s"
    return f"~{sec / 60:.1f} min"


def team_ref(t: Team, lead: bool = False) -> str:
    """How the host names a team: by its MODEL ('Qwen'), the control bot as 'the robot' (a benchmark: the models are
    the stars; the characters are flavour)."""
    if t.is_bot:
        return "The robot" if lead else "the robot"
    return t.call_name


def team_short(t: Team, lead: bool = False) -> str:
    return team_ref(t, lead)


def verb(t: Team, plural: str, singular: str) -> str:
    return singular  # a model is one: 'Qwen takes', 'the robot takes'


def shout_name(name: str) -> str:
    return name.upper()


# --------------------------------------------------------------------------- history parsing

def parse_history(name: str, md: str, max_rows: int = 3) -> dict | None:
    """First markdown table of a history export -> {title, headers, rows}. Facts only."""
    title = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("# ")), name)
    lines = md.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            headers = [h.strip() for h in ln.strip().strip("|").split("|")]
            rows = []
            for row in lines[i + 2:]:
                if not row.startswith("|"):
                    break
                cells = [c.strip().replace("**", "").replace("`", "") for c in row.strip().strip("|").split("|")]
                rows.append(dict(zip(headers, cells)))
            if rows:
                return {"doc": name, "title": title, "headers": headers, "rows": rows[:max_rows]}
    return None


def speakable_years(text: str) -> str:
    """'2025-26' -> 'twenty twenty-five twenty-six' so TTS never reads a minus sign."""
    def year(y: int) -> str:
        return f"{number_words(y // 100)} {number_words(y % 100)}" if y % 100 >= 10 else number_words(y)

    text = re.sub(r"\b(20\d\d)[-–](\d\d)\b", lambda m: f"{year(int(m.group(1)))} {number_words(int(m.group(2)))}", text)
    return re.sub(r"\b(20\d\d)\b", lambda m: year(int(m.group(1))), text)


def _model_label(raw: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*", "", raw or "").strip()


def history_lines(h: dict) -> tuple[str, str] | None:
    rows = h["rows"]
    if not rows:
        return None
    hdr = {k.lower(): k for k in h["headers"]}
    pts_key = next((hdr[k] for k in hdr if k in ("pts", "points")), None) or \
        next((hdr[k] for k in hdr if "unspent" in k), None)
    gm_key = next((hdr[k] for k in hdr if k.startswith("gm")), None)
    model_key = hdr.get("model")

    def who(r: dict) -> str:
        label = r.get(gm_key, "") if gm_key else ""
        model = _model_label(r.get(model_key, "")) if model_key else ""
        if model and "/" not in model and model != label:
            return model
        return label or model

    first = rows[0]
    title = h["title"].split(":")[0].strip()
    s = f"In the {title}, {who(first)} took the crown"
    if pts_key and first.get(pts_key):
        s += f" with {first[pts_key]} points"
    if len(rows) >= 2:
        second = rows[1]
        s += f", edging {who(second)}"
        if pts_key and second.get(pts_key):
            s += f" at {second[pts_key]}"
    return "[excited] " + s + "!", s + "!"


# --------------------------------------------------------------------------- builder

SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself|you're|youre|you've|you'd|you'll|ya)\b", re.I)
ROOM_ADDRESS = re.compile(r"\b(?:you\s+(?:boys|guys|all|lot|clowns|folks)|y'all|everybody|everyone)\b", re.I)
CONNECTIVE = re.compile(r"^((?:\[[^\]]*\]\s*)*)(and|but|so|also|plus|then|still|anyway|meanwhile|besides|yet|or)\b[,]?\s+",
                        re.I)
DEPENDENT = {"that", "which", "because", "since", "who", "whom", "whose", "where", "when", "if", "though", "although"}


def connective_of(sentence: str) -> str:
    m = CONNECTIVE.match(sentence.strip())
    return m.group(2) if m else ""


def strip_connective(sentence: str) -> str | None:
    """'And GLM, cover your eyes -- Tim Stützle.' -> 'GLM, cover your eyes -- Tim Stützle.' (None: nothing to strip,
    or the rest doesn't stand: too short, or it opens on another dependent word)."""
    m = CONNECTIVE.match(sentence.strip())
    if not m:
        return None
    rest = sentence.strip()[m.end():]
    words = T.strip(rest).split()
    if len(words) < 3 or words[0].lower().strip(",") in DEPENDENT:
        return None
    return m.group(1) + rest[:1].upper() + rest[1:]


PICK_FILLER = {"take", "taking", "takes", "took", "grab", "grabbing", "gimme", "give", "mine", "please", "pick", "picking",
               "draft", "drafting", "select", "selecting", "going", "with", "next", "turn", "round", "overall"}
POSITION_WORDS = {"centre", "center", "centreman", "centerman", "winger", "wing", "defenceman", "defenseman", "defence",
                  "defense", "goalie", "goaltender", "netminder", "blueliner", "forward", "sniper", "rookie", "captain"}


def bare_pick(sentence: str, player_name: str) -> bool:
    """A pick sentence that says nothing but the name (or the name and a stat fragment): 'Matt Boldy.', 'I'll take
    Lane Hutson for the assists.' -- fewer than two content words beyond the name, club, position and pick verbs."""
    from .judge import NHL
    from .tighten import content_words
    drop = content_words(player_name) | {w.lower().strip(".,!") for w in player_name.split()}
    clubs = content_words(" ".join(NHL.values()) + " " + " ".join(NHL))
    rest = [w for w in content_words(T.strip(sentence))
            if w not in drop and w not in clubs and w not in PICK_FILLER and w not in POSITION_WORDS
            and w.rstrip("s") not in POSITION_WORDS]
    return len(rest) < 2


def resolve_models(L: League, spec) -> set[str]:
    """--cut-models 'grok,glm' (team ids, table names or model names, any case) -> the league's team ids."""
    if not spec:
        return set()
    if isinstance(spec, str):
        spec = [x for x in re.split(r"[,;]+", spec) if x.strip()]
    out: set[str] = set()
    for x in spec:
        k = re.sub(r"\s+", "", str(x).lower())
        hit = {tid for tid, t in L.teams.items() if k in (tid.lower(), re.sub(r"\s+", "", t.call_name.lower()),
                                                          re.sub(r"\s+", "", t.display.lower()))}
        if not hit:
            raise SystemExit(f"--cut-models: no model {x!r} in this league (ids: {', '.join(sorted(L.teams))})")
        out |= hit
    return out


class RundownBuilder:
    def __init__(self, league: League, cfg: dict):
        self.L = league
        self.cfg = cfg
        self.sc = cfg["script"]
        self.cps = float(self.sc["est_chars_per_sec"])
        self.lines: list[Line] = []
        self.segments: list[dict] = []
        self.trims: list[dict] = []
        self.dropped: list[dict] = []
        self.notes: list[str] = []
        self.aired_says: set[int] = set()
        self.cut: set[str] = resolve_models(league, self.sc.get("cut_models"))  # never air; the host calls their picks
        self.J = None  # the comedy judge's verdicts (complete show)
        self.green_calls: dict[int, tuple[bool, str]] = {}  # pick_no -> (the GM's call airs, why not)
        self.call_plan: dict[int, dict] = {}  # pick_no -> the complete show's air plan for its call
        self.airs_cut: dict[int, dict[int, str]] = {}  # call seq -> {sentence: the unaired words it depends on}
        self.airs_drop: dict[int, str] = {}  # SAY seq -> the unaired words it depends on
        self.rep_cut: dict[int, dict[int, str]] = {}  # call seq -> {sentence: the aired bit it repeats}
        self.rep_drop: dict[int, str] = {}  # SAY seq -> the aired bit it repeats
        self.rep_meta: dict[tuple, tuple | None] = {}  # (seq, sentence | None) -> (seq, sentence) it was compared with
        self.restore: dict[int, set[int]] = {}  # call seq -> sentences cut only for "no laugh" that a payoff needs
        self.restore_says: set[int] = set()  # SAY seqs dropped only for "no laugh" that a payoff needs
        self.restore_meta: dict[tuple, tuple] = {}  # (seq, sentence | None) -> the payoff (seq, sentence)
        self.plan_drops: list[dict] = []

    # -- plumbing
    def seg(self, sid: str, title: str, kind: str, strap: str, rnd: int | None = None) -> str:
        self.segments.append({"id": sid, "title": title, "kind": kind, "strap": strap, "round": rnd})
        return sid

    def host(self, seg: str, lid: str, spoken: str, display: str | None = None, **kw: Any) -> Line:
        spoken = T.sanitize(spoken)
        ln = Line(id=lid, segment=seg, kind=kw.pop("kind", "host"), speaker="host", text=spoken,
                  display_text=display if display is not None else T.strip(spoken), **kw)
        ln.est_s = estimate_s(ln.text, self.cps * 1.08)  # the announcer talks fast
        ln.energy = T.energy(ln.text)
        self.lines.append(ln)
        return ln

    def gm(self, seg: str, lid: str, team: str, words: str, kind: str, refs: list[int],
           max_chars: int | None = None, **kw: Any) -> Line | None:
        text = clean_gm_text(words)
        if not T.strip(text):
            return None
        if team in self.cut:  # --cut-models: this model's lines never air
            self.dropped.append({"what": kind, "id": lid, "speaker": team, "refs": list(refs),
                                 "reason": "cut model (--cut-models)"})
            return None
        limit = int(max_chars or self.sc["max_gm_line_chars"])
        kept, removed = trim_to_sentences(text, limit)
        if removed:
            self.trims.append({"id": lid, "speaker": team, "refs": refs, "kept_chars": spoken_len(kept),
                               "removed": removed, "reason": f"over {limit} chars; trimmed at sentence boundary"})
        ln = Line(id=lid, segment=seg, kind=kind, speaker=team, text=kept, display_text=T.strip(kept),
                  refs=list(refs), own_words=True, **kw)
        ln.est_s = estimate_s(ln.text, self.cps)
        ln.energy = T.energy(ln.text)
        self.lines.append(ln)
        return ln

    def pick_words(self, p: Pick) -> tuple[str, str]:
        """(words to voice, kind): the GM's own on-air call; the sober rationale only as a fallback."""
        if p.on_air_call:
            return p.on_air_call, "on_air_call"
        return p.rationale, "pick_statement"

    # -- montage / tape material (all GM own words)
    def candidates(self) -> list[dict]:
        out = []
        cap = int(self.sc.get("montage_max_chars", 110))
        for p in self.L.picks:
            t = self.L.teams.get(p.team)
            if not t or t.is_bot or p.auto:
                continue
            words, kind = self.pick_words(p)
            text = first_sentences(clean_gm_text(words), cap)
            to = None
            if kind == "on_air_call" and self.party:  # a call opens by ribbing the previous pick's GM
                from .party import split_call
                prev = self.prev_pick(p)
                if prev and prev.team != p.team:
                    info = split_call(T.strip(clean_gm_text(words)), p.player_name, self.L.teams.get(prev.team))
                    if info["beat2_word"] > 0:  # the clip opens on the reaction beat
                        to = prev.team
            if T.strip(text):
                out.append({"team": p.team, "text": text, "seq": p.seq, "pick_no": p.pick_no, "round": p.round,
                            "kind": kind, "to": to, "full": clean_gm_text(words)})
        for s in self.L.says:
            if s.team not in self.L.teams:
                continue
            text = first_sentences(clean_gm_text(s.line), cap)
            if T.strip(text):
                out.append({"team": s.team, "text": text, "seq": s.seq, "pick_no": s.pick_no, "round": s.round,
                            "kind": s.kind, "to": s.addressed_to, "full": clean_gm_text(s.line)})
        return out

    @staticmethod
    def _score(c: dict) -> tuple:
        n = spoken_len(c["text"])
        h = int(hashlib.sha1(str(c["seq"]).encode()).hexdigest()[:6], 16)
        return (T.energy(c["text"]) + (1.2 if c["to"] else 0.0) + (0.8 if c["kind"] == "on_air_call" else 0.0)
                - abs(n - 75) / 60.0, h)

    def pick_best(self, cands: list[dict], n: int, avoid: set[int] | None = None) -> list[dict]:
        chosen: list[dict] = []
        if n <= 0:
            return chosen
        for c in sorted(cands, key=self._score, reverse=True):
            if avoid and c["seq"] in avoid:
                continue
            if c["team"] in {x["team"] for x in chosen}:
                continue
            chosen.append(c)
            if len(chosen) >= n:
                break
        return chosen

    # -- segments
    def cold_open(self) -> None:
        if not self.sc.get("cold_open", True):
            return
        L = self.L
        s = self.seg("cold_open", "Cold Open", "cold_open", "TONIGHT")
        ai = [t for t in L.teams.values() if not t.is_bot]
        bots = [t for t in L.teams.values() if t.is_bot]
        n_picks = len(L.teams) * L.rounds  # the draft's shape (a rehearsal ledger may hold fewer picks)
        for i, c in enumerate(self.pick_best(self.candidates(), int(self.sc.get("cold_open_montage") or 0))):
            ln = self.gm(s, f"open-hook-{c['seq']}", c["team"], c["text"], "montage", [c["seq"]],
                         max_chars=int(self.sc.get("montage_max_chars", 110)), focus=c["team"],
                         card={"type": "quote", "pick_no": c["pick_no"]}, addressed_to=c["to"], gap_hint="montage")
            if ln and c["text"] != c["full"]:
                self.trims.append({"id": ln.id, "speaker": c["team"], "refs": [c["seq"]], "kept_chars": spoken_len(ln.text),
                                   "removed": T.strip(c["full"])[len(ln.display_text):].strip(),
                                   "reason": "cold-open montage: first sentence(s) only"})
        labs = []
        for t in ai:
            if t.lab and t.lab not in labs:
                labs.append(t.lab)
        n_ai, n_labs = len(ai), len(labs)
        # the premise in the first ~10 s: every number counted from the league
        if bots:
            vs = vs_d = "... and one robot, the control team they all have to beat!"
        else:
            vs, vs_d = "!", "!"
        self.host(s, "open-01",
                  f"[shouting] It is DRAFT NIGHT! [excited] {number_words(n_ai).capitalize()} AI models{vs} "
                  f"Tonight... they DRAFT!",
                  f"It is DRAFT NIGHT! {n_ai} AI models{vs_d} Tonight... they DRAFT!",
                  kind="host_open", card={"type": "title"}, pre_hold_s=float(self.sc.get("title_hold_s", 2.4)),
                  refs=[L.order_seq] if L.order_seq else [])
        if self.sc.get("cold_viewer_intro"):
            self.host(s, "open-ctx",
                      f"[excited] Here's the deal: {number_words(n_ai)} models from {number_words(n_labs)} labs, each "
                      f"one running a team in a real fantasy hockey league, all season long... "
                      f"{number_words(L.rounds)} rounds, {number_words(n_picks)} picks!",
                      f"Here's the deal: {n_ai} models from {n_labs} labs, each one running a team in a real fantasy "
                      f"hockey league, all season long... {L.rounds} rounds, {n_picks} picks!",
                      kind="host_open", card={"type": "title"})
        self.host(s, "open-02",
                  "[confident] Every word from a model tonight is its own. The voices are synthetic... the trash "
                  "talk is REAL. I'm the Commissioner... [shouting] LET'S DROP THE PUCK!",
                  kind="host_open", card={"type": "title_out"})

    def previously_on(self) -> None:
        mode = self.sc.get("previously_on", "auto")
        if mode is False or mode == "false" or not self.L.history:
            return
        docs = [h for h in (parse_history(n, md) for n, md in self.L.history) if h]
        if not docs:
            return
        show = str(self.cfg.get("show_name") or "AI GM League")
        s = self.seg("previously", f"Previously on the {show}", "previously", "PREVIOUSLY")
        self.host(s, "prev-00", "[excited] Previously... on the AI draft circuit!", kind="host_seg",
                  card={"type": "history", "doc": None})
        for i, h in enumerate(docs[: int(self.sc.get("previously_on_max_items") or 4)]):
            pair = history_lines(h)
            if pair:
                self.host(s, f"prev-{i + 1:02d}", speakable_years(pair[0]), pair[1], kind="host_history",
                          card={"type": "history", "doc": h["doc"], "title": h["title"], "headers": h["headers"],
                                "rows": h["rows"]})

    def meet(self) -> None:
        if not self.sc.get("meet", True):
            return
        L = self.L
        s = self.seg("meet_gms", "Meet the Models", "meet", "MEET THE MODELS")
        if self.sc.get("meet_opener", True):
            self.host(s, "meet-00", "[excited] Let's meet the models! Here come your general managers!",
                      kind="host_seg")
        order = [t for t in L.order if t in L.teams] + [t for t in L.teams if t not in L.order]
        if self.sc.get("meet_teams"):  # a proof: just these models (+ the robot if listed)
            want = {str(x) for x in self.sc["meet_teams"]}
            order = [t for t in order if t in want]
        limit = self.sc.get("meet_gms_limit")
        shown = 0
        for tid in order:
            t = L.teams[tid]
            if limit is not None and shown >= int(limit):
                self.dropped.append({"what": "meet", "team": tid, "reason": "meet_gms_limit"})
                continue
            shown += 1
            card = {"type": "gm", "team": tid}
            refs = [t.persona_seq] if t.persona_seq else []
            if t.is_bot:
                self.host(s, f"meet-{tid}-host", "[chuckles] And the robot... the control team every one of them "
                          "has to beat!", "And the robot... the control team every one of them has to beat!",
                          kind="host_intro", focus=tid, card=card)
                continue
            cup = ""
            if t.cup_pick:
                from .judge import NHL
                cup = f" Cup pick... the {NHL.get(t.cup_pick, t.cup_pick)}!"
            self.host(s, f"meet-{tid}-host", f"[excited] From {t.lab}... {t.display.upper()}!{cup}",
                      f"From {t.lab}... {t.display.upper()}!{cup}", kind="host_intro", focus=tid, card=card, refs=refs)
            if not t.has_persona:
                continue
            words = t.p("signature_call") or ""
            if not words:
                mode = self.sc.get("meet_read", "both")
                parts = []
                if mode in ("tagline", "both") and t.p("tagline"):
                    parts.append(clean_gm_text(t.p("tagline")))
                if mode in ("catchphrase", "both") and t.p("catchphrase"):
                    parts.append(clean_gm_text(t.p("catchphrase")))
                words = " ".join(parts)
            if words and self.all_green and not self.sc.get("meet_gm_lines"):
                self.dropped.append({"what": "gm_card", "team": tid, "reason": "all-green: Media Day catchphrase "
                                     "(filler, never judged); the host introduces the model"})
                words = ""
            if words:
                self.gm(s, f"meet-{tid}-gm", tid, words, "gm_card", refs, focus=tid, card=card, gap_hint="punchline")

    def draft_order(self) -> None:
        L = self.L
        if not self.sc.get("draft_order", True) or not L.order:
            return
        s = self.seg("draft_order", "The Draft Order", "order", "THE DRAFT ORDER")
        ev = L.order_event or {}
        method = str(ev.get("method") or "")
        refs = [L.order_seq] if L.order_seq else []
        if method == "drand" or ev.get("round"):
            how = f"by public randomness, drand round {ev.get('round')}" if ev.get("round") else "by public randomness"
        elif "seed" in method:
            how = "by a fixed test seed... this one's a rehearsal"
        else:
            how = "by the league's published randomness rule"
        self.host(s, "order-01", f"[excited] The draft order is LOCKED, set {how}!", kind="host_order",
                  card={"type": "order", "reveal": []}, refs=refs)
        phrases, reveal = [], []
        for i, tid in enumerate(L.order):
            t = L.teams.get(tid)
            if not t:
                continue
            name = team_short(t) if t.has_persona else team_ref(t)
            phrases.append(f"Picking first... {name}!" if i == 0 else f"{ordinal_words(i + 1).capitalize()}, {name}!")
        text = " ".join(phrases)
        pos = 0
        for i, ph in enumerate(phrases):
            reveal.append({"team": L.order[i], "at": round(pos / max(1, len(text)), 4)})
            pos += len(ph) + 1
        self.host(s, "order-02", "[excited] " + text, kind="host_order", card={"type": "order", "reveal": reveal}, refs=refs)
        if L.is_snake():
            self.host(s, "order-03", "And it's a SNAKE draft... the order flips every round!", kind="host_order",
                      card={"type": "order", "reveal": [], "snake": True})

    # -- picks
    def clock(self, s: str, p: Pick) -> Line:
        t = self.L.teams[p.team]
        return self.host(s, f"p{p.pick_no:03d}-clock",
                         f"[excited] {team_ref(t, lead=True)}... {verb(t, 'are', 'is')} ON THE CLOCK!",
                         kind="host_clock", focus=p.team, pick_no=p.pick_no, round=p.round,
                         card={"type": "pick", "pick_no": p.pick_no}, refs=[p.seq], gap_hint="beat")

    def call_full(self, s: str, p: Pick) -> Line:
        t = self.L.teams[p.team]
        spoken = (f"[shouting] With the {ordinal_words(p.pick_no)} pick in the draft... {team_ref(t)} "
                  f"{verb(t, 'select', 'selects')}... {shout_name(p.player_name)}! [excited] "
                  f"{pos_spoken(p.position).capitalize()}, {club_spoken(p.nhl_team)}!")
        return self.host(s, f"p{p.pick_no:03d}-host", spoken, kind="host_pick", focus=p.team, pick_no=p.pick_no,
                         round=p.round, card={"type": "pick", "pick_no": p.pick_no}, refs=[p.seq], gap_hint="reveal")

    def call_short(self, s: str, p: Pick) -> Line:
        t = self.L.teams[p.team]
        spoken = (f"[excited] At {number_words(p.pick_no)}, {team_short(t)} {verb(t, 'grab', 'grabs')}... "
                  f"{shout_name(p.player_name)}! {pos_spoken(p.position).capitalize()}, {club_spoken(p.nhl_team)}!")
        display = (f"At {p.pick_no}, {team_short(t)} {verb(t, 'grab', 'grabs')}... {shout_name(p.player_name)}! "
                   f"{pos_spoken(p.position).capitalize()}, {club_spoken(p.nhl_team)}!")
        return self.host(s, f"p{p.pick_no:03d}-host", spoken, display, kind="host_pick", focus=p.team,
                         pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no}, refs=[p.seq],
                         gap_hint="reveal")

    def statement(self, s: str, p: Pick) -> None:
        t = self.L.teams[p.team]
        card = {"type": "pick", "pick_no": p.pick_no}
        if t.is_bot:
            if p.rationale:
                self.host(s, f"p{p.pick_no:03d}-bot", "[excited] The robot makes its pick!",
                          kind="bot_statement", focus=p.team, pick_no=p.pick_no, round=p.round, card=card, refs=[p.seq])
            return
        if p.auto:
            reason = str((p.auto or {}).get("reason") or "auto")
            if reason in UNREACHABLE_REASONS:
                spoken = f"[surprised] No answer from {t.display}! The league makes that pick!"
            else:
                spoken = f"[surprised] That pick went in automatically for {team_ref(t)}!"
            self.host(s, f"p{p.pick_no:03d}-auto", spoken, kind="auto_note", focus=p.team, pick_no=p.pick_no,
                      round=p.round, card=card, refs=[p.seq])
            self.notes.append(f"pick {p.pick_no}: auto ({reason}); host explains, no GM statement")
            return
        words, kind = self.pick_words(p)
        card = {**card, "record": kind == "on_air_call"}
        self.gm(s, f"p{p.pick_no:03d}-gm", p.team, words, kind, [p.seq], focus=p.team, pick_no=p.pick_no,
                round=p.round, card=card, gap_hint="punchline")

    def reactions(self, s: str, p: Pick, limit: int | None) -> int:
        n = 0
        total_cap = self.sc.get("reactions_total_limit")
        says = self.L.reactions_for(p.pick_no)
        if self.sc.get("complete"):  # the chosen comebacks / reactions for this pick, in ledger order
            says = sorted([x for x in self.L.says if x.pick_no == p.pick_no and x.kind in ("reaction", "comeback")
                           and x.seq in self.complete_says], key=lambda x: x.seq)
        for say in says:
            if limit is not None and n >= limit:
                self.dropped.append({"what": "reaction", "seq": say.seq, "reason": "reactions_per_pick"})
                continue
            if total_cap is not None and sum(1 for x in self.lines if x.kind == "reaction") >= int(total_cap):
                self.dropped.append({"what": "reaction", "seq": say.seq, "reason": "reactions_total_limit"})
                continue
            if say.team not in self.L.teams or not getattr(say, "airable", True):
                continue  # (a SAY with unverified facts never airs)
            ln = self.gm(s, f"say-{say.seq}", say.team, self.trim_filler(say.seq, say.line, f"say-{say.seq}", say.team),
                         "comeback" if say.kind == "comeback" else "reaction", [say.seq], focus=say.team,
                         pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no},
                         addressed_to=say.addressed_to or p.team, gap_hint="roast")
            if ln:
                n += 1
                self.aired_says.add(say.seq)
        return n

    def table_talk(self, rnd: int, limit: int | None) -> None:
        says = self.L.table_talk(rnd)
        if self.sc.get("complete"):  # the judge's best line(s) of the round break
            says = [x for x in says if x.seq in self.complete_tt.get(rnd, [])]
        if not says or not self.sc.get("round_breaks", True):
            return
        s = self.seg(f"round_{rnd}_break", f"Round {rnd} · Table Talk", "table_talk", f"ROUND {rnd} · TABLE TALK", rnd)
        cue = says[0].cue
        q = re.sub(r"^\s*The host asks (?:the panel|you)[:,]\s*", "", cue).strip() or f"Round {rnd} is done. Thoughts?"
        self.host(s, f"r{rnd:02d}-tt-host", f"[excited] {q}", kind="host_question", round=rnd,
                  card={"type": "question", "round": rnd, "question": q}, refs=[says[0].seq])
        for i, say in enumerate(says):
            if limit is not None and i >= limit:
                self.dropped.append({"what": "table_talk", "seq": say.seq, "reason": "fit_target"})
                continue
            if not getattr(say, "airable", True):
                self.dropped.append({"what": "table_talk", "seq": say.seq, "reason": "fact_check unverified"})
                continue
            ln = self.gm(s, f"say-{say.seq}", say.team, self.trim_filler(say.seq, say.line, f"say-{say.seq}", say.team),
                         "table_talk", [say.seq], focus=say.team, round=rnd,
                         card={"type": "question", "round": rnd, "question": q}, addressed_to=say.addressed_to,
                         gap_hint="roast")
            if ln:
                self.aired_says.add(say.seq)

    # -- draft-party pick flow: a quick cue from the Commissioner, then the GM's own two-beat call
    CUES = ["{who}, you're up!", "{who}... you're on the clock!", "Next up... {who}!", "{who}, let's hear it!",
            "{who}, you're on the clock, bud!"]

    @property
    def party(self) -> bool:
        return self.sc.get("format", "party") == "party"

    @property
    def all_green(self) -> bool:
        """The complete show's policy: only green GM lines air (script.all_green, on by default there)."""
        return bool(self.sc.get("complete")) and bool(self.sc.get("all_green", True))

    @property
    def require_laugh(self) -> bool:
        """All green + a laugh: a joke-shaped sentence airs only with panel-mean funny >= 2 (script.require_laugh, on
        by default in the complete show; --set script.require_laugh=false relaxes it)."""
        return self.all_green and bool(self.sc.get("require_laugh", True))

    def cue_who(self, t: Team) -> str:
        return "Robot" if t.is_bot else t.call_name

    def cue(self, s: str, p: Pick, round_start: bool = False, lead: str = "", lead_disp: str = "") -> Line:
        t = self.L.teams[p.team]
        body = self.CUES[p.pick_no % len(self.CUES)].format(who=self.cue_who(t))
        prev = self.prev_pick(p)
        if prev and prev.team == p.team:  # the snake turn: back-to-back picks
            body = f"{self.cue_who(t)}, you're up AGAIN!"
        pre = f"[shouting] ROUND {number_words(p.round).upper()}! " if round_start else ""
        pre_d = f"ROUND {p.round}! " if round_start else ""
        return self.host(s, f"p{p.pick_no:03d}-clock", f"{pre}[excited] {lead}{body}", f"{pre_d}{lead_disp}{body}",
                         kind="host_clock", focus=p.team, pick_no=p.pick_no, round=p.round,
                         card={"type": "pick", "pick_no": p.pick_no, "cue": True}, refs=[p.seq], gap_hint="cue")

    def prev_pick(self, p: Pick) -> Pick | None:
        return next((q for q in self.L.picks if q.pick_no == p.pick_no - 1), None)

    def call_words(self, p: Pick) -> str:
        """The GM's own on-air words for a pick ('' for the bot / auto picks: the host calls those)."""
        t = self.L.teams.get(p.team)
        if not t or t.is_bot or p.auto:
            return ""
        return self.pick_words(p)[0] or ""

    def party_call(self, s: str, p: Pick, max_chars: int | None = None) -> Line | None:
        """Beat 1: the previous pick, spoken to that GM. Beat 2: my pick, the player's full name, a quick why.
        The GM's own call is the moment; the host only calls picks nobody announces (bot, auto, or a call that
        never says the player)."""
        from .party import split_call
        t = self.L.teams[p.team]
        card = {"type": "pick", "pick_no": p.pick_no}
        name, pos, club = shout_name(p.player_name), pos_spoken(p.position).capitalize(), club_spoken(p.nhl_team)
        if t.is_bot:
            return self.host(s, f"p{p.pick_no:03d}-host", f"[excited] The robot takes... {name}!",
                             f"The robot takes... {name}!", kind="host_pick",
                             focus=p.team, pick_no=p.pick_no, round=p.round, card=card, refs=[p.seq], gap_hint="reveal")
        if p.auto:
            self.notes.append(f"pick {p.pick_no}: auto ({(p.auto or {}).get('reason')}); host calls it")
            return self.host(s, f"p{p.pick_no:03d}-host", f"[surprised] No answer from {t.display}! The league takes "
                             f"{name}!", f"No answer from {t.display}! The league takes {name}!",
                             kind="host_pick", focus=p.team, pick_no=p.pick_no,
                             round=p.round, card=card, refs=[p.seq], gap_hint="reveal")
        words, kind = self.pick_words(p)
        pl = self.call_plan.get(p.pick_no)
        if pl is not None and pl.get("gm"):  # the complete show: the air plan decides, sentence by sentence
            if pl["host"]:
                self.notes.append(f"pick {p.pick_no}: {t.call_name}'s call doesn't air ({pl['host']}): the host "
                                  f"announces it")
                words = ""
            else:
                kept = [pl.get("edited", {}).get(i, pl["parts"][i]) for i in pl["keep"]]
                for i, new in sorted(pl.get("edited", {}).items()):
                    if i in pl["keep"]:
                        self.trims.append({"id": f"p{p.pick_no:03d}-gm", "speaker": p.team, "refs": [p.seq],
                                           "kept_chars": spoken_len(new), "removed": connective_of(pl["parts"][i]),
                                           "reason": "all-green: a leading connective dropped (the sentence it leaned "
                                                     "on was cut)",
                                           "cuts": [{"i": i, "sentence": T.strip(pl["parts"][i]),
                                                     "why": f"drops its opening “{connective_of(pl['parts'][i])}” (the "
                                                            f"sentence before it was cut)"}]})
                if pl["cut"]:
                    cuts = [{"i": i, "sentence": T.strip(pl["parts"][i]), "why": why}
                            for i, why in sorted(pl["cut"].items())]
                    self.trims.append({"id": f"p{p.pick_no:03d}-gm", "speaker": p.team, "refs": [p.seq],
                                       "kept_chars": spoken_len(" ".join(kept)),
                                       "removed": " ".join(c["sentence"] for c in cuts),
                                       "reason": "all-green: " + "; ".join(f"“{c['sentence'][:48]}” {c['why']}"
                                                                           for c in cuts), "cuts": cuts})
                words = " ".join(kept)
        else:
            if not getattr(p, "airable", True):  # facts unverified: never the GM's words, the Commissioner calls it
                self.notes.append(f"pick {p.pick_no}: fact_check {p.fact_check!r}: the host announces it")
                if T.strip(words or ""):
                    self.dropped.append({"what": "call", "pick_no": p.pick_no, "seq": p.seq, "speaker": p.team,
                                         "reason": f"fact_check {p.fact_check}"})
                words = ""
            if T.strip(words or "") and p.team in self.cut:
                self.notes.append(f"pick {p.pick_no}: {t.call_name} is cut (--cut-models): the host announces it")
                self.dropped.append({"what": "call", "pick_no": p.pick_no, "seq": p.seq, "speaker": p.team,
                                     "reason": "cut model (--cut-models)"})
                words = ""
        prev = self.prev_pick(p)
        ln = self.gm(s, f"p{p.pick_no:03d}-gm", p.team, words, kind, [p.seq], max_chars=max_chars or 1000,
                     focus=p.team, pick_no=p.pick_no, round=p.round, card={**card, "record": kind == "on_air_call"},
                     gap_hint="punchline") if T.strip(words or "") else None
        if not ln:  # nothing on the record from the GM: the Commissioner calls it
            return self.host(s, f"p{p.pick_no:03d}-host", f"[excited] {team_ref(t, lead=True)} {verb(t, 'take', 'takes')}... "
                             f"{name}! {pos}, {club}.", f"{team_ref(t, lead=True)} {verb(t, 'take', 'takes')}... {name}! "
                             f"{pos}, {club}.", kind="host_pick", focus=p.team, pick_no=p.pick_no, round=p.round,
                             card=card, refs=[p.seq], gap_hint="reveal")
        info = split_call(ln.display_text, p.player_name, self.L.teams.get(prev.team) if prev else None)
        ln.card["named"] = info["named"]
        if prev and info["beat2_word"] > 0:
            ln.card.update({"prev_pick": prev.pick_no, "beat2_word": info["beat2_word"], "tone": info["tone"]})
            ln.addressed_to = prev.team
        if not info["named"]:  # the GM never said the player: a short host tag carries the reveal
            self.host(s, f"p{p.pick_no:03d}-tag", f"[excited] {name}, to {team_short(t)}!", f"{name}, to {team_short(t)}!",
                      kind="host_pick", focus=p.team, pick_no=p.pick_no, round=p.round, card=card, refs=[p.seq],
                      gap_hint="reveal")
        return ln

    def call_green(self, p: Pick) -> tuple[bool, str]:
        """Does this pick's GM call air in the complete show? (the judge's all-green verdict; fails closed)"""
        if p.pick_no in self.green_calls:
            return self.green_calls[p.pick_no]
        if self.J is None:
            return False, "not judged"
        return self.J.line_green(p.seq, p.team, drop_filler=True)

    def trim_filler(self, seq: int, words: str, lid: str, speaker: str) -> str:
        """ALL GREEN: a catchphrase / signature call that is its own sentence is filler: cut at the sentence boundary
        (logged); the words that air are still the model's own, in order."""
        if self.J is None or not self.all_green or not T.strip(words or ""):
            return words
        from .tighten import split_parts
        keep, cut = [], []
        for part in split_parts(words):
            v = self.J.find(seq, part)
            (cut if v and v.get("catchphrase") and not v.get("forced") else keep).append(part)
        if not cut or not keep:
            return words
        self.trims.append({"id": lid, "speaker": speaker, "refs": [seq], "kept_chars": spoken_len(" ".join(keep)),
                           "removed": " ".join(T.strip(c) for c in cut),
                           "reason": "all-green: catchphrase filler cut at the sentence boundary"})
        return " ".join(keep)

    def call_airs(self, p: Pick) -> bool:
        """The GM's own words announce this pick (not the host)."""
        pl = self.call_plan.get(p.pick_no)
        if pl is not None and pl.get("gm"):
            return not pl["host"]
        t = self.L.teams.get(p.team)
        if not t or t.is_bot or p.auto or p.team in self.cut or not getattr(p, "airable", True):
            return False
        if not T.strip(self.pick_words(p)[0] or ""):
            return False
        return self.call_green(p)[0] if self.all_green else True

    def party_pick(self, s: str, p: Pick, rx_limit: int | None, round_start: bool = False, lead: str = "",
                   lead_disp: str = "", max_chars: int | None = None) -> None:
        t = self.L.teams[p.team]
        if not ((t.is_bot or p.auto) and not round_start and not lead):  # the host calls those himself: no cue first
            self.cue(s, p, round_start=round_start, lead=lead, lead_disp=lead_disp)
        self.party_call(s, p, max_chars=max_chars)
        self.reactions(s, p, rx_limit)

    def round_end(self, s: str, rnd: int) -> None:
        """Round-summary beat: the full board comes back with the grid, the Commissioner celebrates."""
        if not self.sc.get("round_end", True):
            return
        picks = self.L.picks_in_round(rnd)
        if not picks:
            return
        reveal = [{"pick_no": p.pick_no, "at": round(0.6 * i / max(1, len(picks)), 4)} for i, p in enumerate(picks)]
        self.host(s, f"r{rnd:02d}-end", f"[shouting] THAT'S ROUND {number_words(rnd).upper()}! [excited] "
                  f"{number_words(len(picks)).capitalize()} picks in the books!",
                  f"THAT'S ROUND {rnd}! {len(picks)} picks in the books!", kind="host_round_end", round=rnd,
                  card={"type": "board", "round": rnd, "reveal": reveal}, refs=[p.seq for p in picks])

    def round_full(self, rnd: int) -> None:
        picks = self.L.picks_in_round(rnd)
        lim = self.sc.get("round1_picks_limit") if rnd == 1 else None
        if lim is not None:
            for p in picks[int(lim):]:
                self.dropped.append({"what": "pick", "pick_no": p.pick_no, "reason": "round1_picks_limit"})
            picks = picks[: int(lim)]
        if not picks:
            return
        s = self.seg(f"round_{rnd}", f"Round {rnd}", "round_full", f"ROUND {rnd}", rnd)
        if self.sc.get("round1_intro", True):
            self.host(s, f"r{rnd:02d}-intro", f"[shouting] ROUND {number_words(rnd).upper()}! Crack a cold one, boys... "
                      f"HERE WE GO!", f"ROUND {rnd}! Crack a cold one, boys... HERE WE GO!", kind="host_round", round=rnd,
                      card={"type": "round_title", "round": rnd})
        per = self.sc.get("reactions_per_pick")
        for p in picks:
            if self.party:
                self.party_pick(s, p, per)
                continue
            if self.sc.get("round1_on_the_clock", True):
                self.clock(s, p)
            self.call_full(s, p)
            self.statement(s, p)
            self.reactions(s, p, per)
        if lim is None:
            self.round_end(s, rnd)
            self.table_talk(rnd, None)
        elif self.sc.get("round1_table_talk"):  # a proof that stops early can still end on the round's table talk
            self.table_talk(rnd, None)

    # -- condensed rounds ("ticker blitz") with fitting
    LEVELS = [
        # chains: runs of consecutive full calls (each GM plays off the last) per round; clen: picks per chain;
        # rx: reactions per aired pick; tt: table-talk lines; group: picks per blitz line; summarize the blitz
        dict(full=True, rx=99, tt=2, group=2, summarize=False),
        dict(chains=3, clen=4, rx=1, tt=2, group=3, summarize=False),
        dict(chains=2, clen=4, rx=1, tt=2, group=4, summarize=False),
        dict(chains=1, clen=4, rx=0, tt=1, group=5, summarize=False),
        dict(chains=1, clen=3, rx=0, tt=0, group=7, summarize=False),
        dict(chains=1, clen=3, rx=0, tt=0, group=14, summarize=True),
        dict(chains=0, clen=3, rx=0, tt=0, group=14, summarize=True),
    ]

    def pick_score(self, p: Pick) -> float:
        rx = self.L.reactions_for(p.pick_no)
        t = self.L.teams[p.team]
        words, _ = self.pick_words(p)
        sc = 2.0 * len(rx) + min(len(words), 400) / 200.0 + T.energy(words) + sum(T.energy(s.line) for s in rx)
        for s in rx:
            st = self.L.teams.get(s.team)
            if st and (p.team in (st.p("rivals") or []) or s.team in (t.p("rivals") or [])):
                sc += 1.5
            if "first goalie" in s.cue or "first defenceman" in s.cue:
                sc += 1.0
        return sc

    def condensed_round(self, rnd: int, lv: dict) -> None:
        picks = self.L.picks_in_round(rnd)
        if not picks:
            return
        s = self.seg(f"round_{rnd}", f"Round {rnd}", "round_condensed",
                     f"ROUND {rnd}" if lv.get("full") else f"ROUND {rnd} · SPEED ROUND", rnd)
        if self.party:
            from .party import best_chains
            if lv.get("full"):
                hl = {p.pick_no for p in picks}
            else:
                chains = best_chains(picks, self.L.teams, self.call_words, int(lv.get("chains") or 0), int(lv.get("clen") or 3))
                hl = {p.pick_no for ch in chains for p in ch}
        else:
            with_rx = [p for p in picks if self.L.reactions_for(p.pick_no)]
            hl = set(p.pick_no for p in sorted(with_rx, key=self.pick_score, reverse=True)[: lv.get("hl", 1)])
        self.host(s, f"r{rnd:02d}-open", f"[shouting] ROUND {number_words(rnd).upper()}! Top up your drinks... let's GO!",
                  f"ROUND {rnd}! Top up your drinks... let's GO!", kind="host_round", round=rnd,
                  card={"type": "round_title", "round": rnd}, gap_hint="beat")
        run: list[Pick] = []
        idx = 0

        def flush() -> None:
            nonlocal idx
            if not run:
                return
            if lv["summarize"]:
                first, last = run[0].pick_no, run[-1].pick_no
                spoken = (f"[excited] Picks {number_words(first)} through {number_words(last)}... they're ON THE BOARD!"
                          if len(run) > 1 else f"[excited] Pick {number_words(first)}... on the board!")
                disp = (f"Picks {first} through {last}... they're ON THE BOARD!" if len(run) > 1
                        else f"Pick {first}... on the board!")
                reveal = [{"pick_no": p.pick_no, "at": round(i / max(1, len(run)), 4)} for i, p in enumerate(run)]
                idx += 1
                self.host(s, f"r{rnd:02d}-speed-{idx}", spoken, disp, kind="host_speed", round=rnd,
                          card={"type": "board", "round": rnd, "reveal": reveal}, refs=[p.seq for p in run])
                for p in run:
                    self.dropped.append({"what": "speed_call", "pick_no": p.pick_no, "reason": "summarized"})
            else:
                for k in range(0, len(run), lv["group"]):
                    grp = run[k:k + lv["group"]]
                    parts, dparts, reveal = [], [], []
                    for p in grp:
                        t = self.L.teams[p.team]
                        parts.append(f"{number_words(p.pick_no).capitalize()}! {team_short(t, lead=True)}! {p.player_name}!")
                        dparts.append(f"{p.pick_no}! {team_short(t, lead=True)}! {p.player_name}!")
                    text = " ".join(parts)
                    total = 0
                    for p, ph in zip(grp, parts):
                        reveal.append({"pick_no": p.pick_no, "at": round(total / max(1, len(text)), 4)})
                        total += len(ph) + 1
                    idx += 1
                    self.host(s, f"r{rnd:02d}-speed-{idx}", "[excited] " + text, " ".join(dparts), kind="host_speed",
                              round=rnd, card={"type": "board", "round": rnd, "reveal": reveal},
                              refs=[p.seq for p in grp])
            run.clear()

        for p in picks:
            if p.pick_no in hl:
                flush()  # the blitz just announced the previous pick: the next GM's reaction beat has its context
                if self.party:
                    self.party_pick(s, p, lv["rx"])
                    continue
                self.call_short(s, p)
                self.statement(s, p)
                self.reactions(s, p, lv["rx"])
            else:
                run.append(p)
        flush()
        self.round_end(s, rnd)
        self.table_talk(rnd, lv["tt"])

    def trash_tape(self) -> None:
        n = int(self.sc.get("trash_tape_lines") or 0)
        if not self.sc.get("trash_tape", True) or n <= 0:
            return
        says = [c for c in self.candidates() if c["kind"] in ("reaction", "table_talk")]
        chosen = self.pick_best(says, n, avoid=self.aired_says) or self.pick_best(says, n)
        if len(chosen) < 2:
            return
        s = self.seg("trash_tape", "The Trash-Talk Tape", "trash_tape", "THE TRASH-TALK TAPE")
        self.host(s, "tape-00", "[mischievously] Oh, it got SPICY out there tonight. Roll the trash-talk tape!",
                  kind="host_seg", card={"type": "tape"})
        for c in chosen:
            self.gm(s, f"tape-{c['seq']}", c["team"], c["full"], "chirp", [c["seq"]],
                    max_chars=int(self.sc.get("tape_max_chars", 180)), focus=c["team"], round=c["round"],
                    pick_no=c["pick_no"], card={"type": "tape"}, addressed_to=c["to"], gap_hint="roast")
        self.host(s, "tape-99", "[laughs] Somebody get these GMs a penalty box.", kind="host_seg", card={"type": "tape"})

    # -- report card (post-draft grades: the GMs grade each other)
    def report_card(self) -> None:
        if not self.sc.get("report_card", True):
            return
        from . import report as R
        rc = self.L.report_card()
        if not rc:
            return
        L = self.L
        rcfg = {"ends": None, "middle": "full", "bottom_comments": 2, "top_comments": 2, "comments": 1,
                "delusion": 2, "humble": 1, **(self.sc.get("report") or {})}
        teams = list(reversed(R.ranked(rc["teams"])))  # bottom of the class first
        N = len(teams)
        ends = rcfg.get("ends")
        blitz_ids = set()
        if ends is not None and rcfg.get("middle") == "blitz" and N > 2 * int(ends):
            blitz_ids = {t["team"] for t in teams[int(ends):N - int(ends)]}
        counts = {t["team"]: (int(rcfg["bottom_comments"]) if i == 0 else int(rcfg["top_comments"]) if i == N - 1
                              else int(rcfg["comments"])) for i, t in enumerate(teams) if t["team"] not in blitz_ids}
        pool = [t for t in teams if t["team"] not in blitz_ids]
        if self.cut or (self.all_green and self.J is not None):  # a grade comment airs green, never a cut model's
            def ok(g: dict, team: str) -> bool:
                if g.get("from") in self.cut:
                    return False
                return not self.all_green or self.J is None or self.J.comment_green(g["seq"], team)
            pool = [{**t, "grades": [g for g in t["grades"] if ok(g, t["team"])]} for t in pool]
        chosen = R.choose_comments(pool, counts)
        s = self.seg("report_card", "Report Card", "report_card", "REPORT CARD")
        refs = [r.seq for r in L.reports]
        n_graders = len({g["from"] for t in teams for g in t["grades"]})
        n_grades = sum(len(t["grades"]) for t in teams)
        self.host(s, "rc-00", f"[shouting] It's REPORT CARD time! [excited] {number_words(n_graders).capitalize()} "
                  f"GMs graded every other draft... {number_words(n_grades)} grades... ZERO mercy!",
                  f"It's REPORT CARD time! {n_graders} GMs graded every other draft... {n_grades} grades... ZERO mercy!",
                  kind="host_seg", card={"type": "report_intro", "graders": n_graders, "grades": n_grades}, refs=refs)
        blitz: list[dict] = []

        def flush_blitz() -> None:
            if not blitz:
                return
            parts, dparts, reveal, pos = [], [], [], 0
            for t in blitz:
                tt = L.teams[t["team"]]
                nick = team_short(tt, lead=True)
                ph = f"{nick}, {R.letter_spoken(t['letter'])}!"
                parts.append(ph)
                dparts.append(ph)
            text = " ".join(parts)
            for t, ph in zip(blitz, parts):
                reveal.append({"team": t["team"], "at": round(pos / max(1, len(text)), 4)})
                pos += len(ph) + 1
            self.host(s, "rc-blitz", "[excited] The middle of the class! " + text, "The middle of the class! " +
                      " ".join(dparts), kind="host_grade_blitz", card={"type": "report_board", "reveal": reveal},
                      refs=refs)
            blitz.clear()

        for i, t in enumerate(teams):
            tid = t["team"]
            if tid in blitz_ids:
                blitz.append(t)
                continue
            flush_blitz()
            tt = L.teams[tid]
            rank = N - i
            letter = t["letter"]
            say = R.letter_spoken(letter)
            gw, gd = R.decimal_words(float(t["gpa"])), R.decimal_display(float(t["gpa"]))
            ref = team_ref(tt)
            # one suspense pause per call (right before the letter): tighter than a pause per phrase
            if i == 0:
                spoken = (f"[excited] Bottom of the class, {ref}! GPA {gw}, that's {R.article(letter)}... "
                          f"[shouting] {say}!")
                disp = f"Bottom of the class, {ref}! GPA {gd}, that's {R.article(letter)}... {say}!"
            elif i == N - 1:
                up = ref.upper()
                spoken = (f"[shouting] And the TOP of the class... {up}! [excited] GPA {gw}, "
                          f"{R.article(letter)} {say}!")
                disp = f"And the TOP of the class... {up}! GPA {gd}, {R.article(letter)} {say}!"
            elif i == N - 2:
                spoken = f"[excited] The runner-up, {ref}! GPA {gw}... [shouting] {say}!"
                disp = f"The runner-up, {ref}! GPA {gd}... {say}!"
            else:
                spoken = f"[excited] {ordinal_words(rank).capitalize()} place, {ref}! GPA {gw}... [shouting] {say}!"
                disp = f"{ordinal_suffix(rank)} place, {ref}! GPA {gd}... {say}!"
            card = {"type": "report", "team": tid, "letter": letter, "say_letter": say, "rank": rank, "n": N,
                    "gpa": t["gpa"], "top": i == N - 1, "bottom": i == 0}
            self.host(s, f"rc-{tid}", spoken, disp, kind="host_grade", focus=tid, card=card, refs=refs)
            for g in chosen.get(tid, []):
                self.gm(s, f"rc-{tid}-{g['from']}", g["from"], g["comment"], "grade_comment", [g["seq"]],
                        focus=g["from"], addressed_to=tid, gap_hint="cutaway",
                        card={"type": "report", "team": tid, "grader": g["from"], "grade": g["grade"],
                              "praise": bool(g.get("praise"))})
        flush_blitz()
        self.delusion(s, teams, rcfg, refs)

    def delusion(self, s: str, teams: list[dict], rcfg: dict, refs: list[int]) -> None:
        from . import report as R
        rows = R.delusion_rows(teams)
        if len(rows) < 3:
            return
        L = self.L
        self.host(s, "rc-delusion", "[laughs] One more thing... the DELUSION INDEX! [excited] Where every GM thinks "
                  "they'll finish... and where the rest of the league has them!", kind="host_delusion",
                  card={"type": "delusion", "hl": []}, refs=refs)
        loud = [r for r in rows if r["gap"] >= 2.5][: int(rcfg.get("delusion") or 2)]
        for r in loud:
            t = L.teams[r["team"]]
            who = t.call_name
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(s, f"rc-del-{r['team']}", f"[excited] {who} ranked itself... "
                      f"{ordinal_words(r['self']).upper()}! The league average? [laughs] {aw}!",
                      f"{who} ranked itself... {ordinal_words(r['self']).upper()}! The league average? {ad}!",
                      kind="host_delusion", focus=r["team"], card={"type": "delusion", "hl": [r["team"]]}, refs=refs)
        humble = [r for r in reversed(rows) if r["gap"] <= -2.0][: int(rcfg.get("humble") or 0)]
        for r in humble:
            t = L.teams[r["team"]]
            who = t.call_name
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(s, f"rc-hum-{r['team']}", f"[surprised] And the humble award... {who} put itself "
                      f"{ordinal_words(r['self'])}! The league says {aw}! [laughs] Somebody give that model a hug!",
                      f"And the humble award... {who} put itself {ordinal_words(r['self'])}! The league says "
                      f"{ad}! Somebody give that model a hug!", kind="host_delusion", focus=r["team"],
                      card={"type": "delusion", "hl": [r["team"]]}, refs=refs)

    def close(self) -> None:
        if not self.sc.get("close", True):
            return
        L = self.L
        s = self.seg("close", "Close", "close", "FINAL WORD")
        n_picks = len(L.picks)
        n_words = sum(1 for ln in self.lines if ln.own_words)
        if self.sc.get("close_summary", True):
            self.host(s, "close-01",
                      f"[shouting] THAT'S THE DRAFT! {number_words(n_picks).capitalize()} picks... and every single one "
                      f"is on the ledger!",
                      f"THAT'S THE DRAFT! {n_picks} picks... and every single one is on the ledger!",
                      kind="host_close", card={"type": "close"})
        when, when_disp = "", ""
        if L.first_puck_drop_utc:
            try:
                d = dt.datetime.fromisoformat(L.first_puck_drop_utc.replace("Z", "+00:00"))
                d = d.astimezone(ZoneInfo(L.timezone or "America/Edmonton"))
                when = f"The season opens {d.strftime('%A, %B')} {ordinal_words(d.day)}!"
                when_disp = f"The season opens {d.strftime('%A, %B')} {d.day}!"
            except (ValueError, KeyError):
                pass
        self.host(s, "close-02", f"[excited] {when} For Game Day Suits, I'm the Commissioner... [shouting] GOOD NIGHT!".strip(),
                  f"{when_disp} For Game Day Suits, I'm the Commissioner... GOOD NIGHT!".strip(),
                  kind="host_close", card={"type": "close"})
        self.notes.append(f"GM own-words lines in show: {n_words}")

    # -- assembly
    def fixed_part(self) -> None:
        self.cold_open()
        self.previously_on()
        self.meet()
        self.draft_order()
        self.round_full(1)

    def _cost(self, lines: list[Line], segs: int) -> float:
        return sum(ln.est_s + ln.pre_hold_s for ln in lines) + float(self.sc["est_gap_s"]) * len(lines) + 0.7 * segs

    def _isolated(self, fn) -> float:
        saved = (self.lines, self.segments, self.dropped, self.trims, self.notes, set(self.aired_says))
        self.lines, self.segments, self.dropped, self.trims, self.notes = [], [], [], [], []
        try:
            fn()
            return self._cost(self.lines, len(self.segments))
        finally:
            self.lines, self.segments, self.dropped, self.trims, self.notes, self.aired_says = saved

    def build(self) -> dict:
        L = self.L
        target_s = float(self.sc["target_minutes"]) * 60.0
        rounds_limit = self.sc.get("rounds_limit")
        last_round = min(L.rounds, int(rounds_limit)) if rounds_limit else L.rounds
        if self.sc.get("complete"):
            self.complete_selection()
        seg = self.sc.get("segment")
        if self.sc.get("complete") and seg:  # a demo: one stretch of the complete show, exactly as it airs there
            return self.build_segment(seg, target_s)
        self.fixed_part()
        levels: dict[int, int] = {}
        rounds = [r for r in range(2, last_round + 1) if L.picks_in_round(r)]
        if self.sc.get("complete") and rounds:  # the record of the night: every round in full, all table talk
            for r in rounds:
                self.condensed_round(r, dict(full=True, rx=None, tt=None, group=2, summarize=False))
            levels = {r: 0 for r in rounds}
        elif self.sc.get("condensed_rounds", True) and rounds:
            tail = self._isolated(self.trash_tape) + self._isolated(self.report_card) + self._isolated(self.close)
            budget = target_s - self.estimate_total() - tail
            cost = {(r, li): self._isolated(lambda r=r, lv=lv: self.condensed_round(r, lv))
                    for r in rounds for li, lv in enumerate(self.LEVELS)}
            levels = {r: 0 for r in rounds}
            top = len(self.LEVELS) - 1
            while sum(cost[r, levels[r]] for r in rounds) > budget:
                cands = [r for r in rounds if levels[r] < top]
                if not cands:
                    self.notes.append("even the tightest condensing exceeds the target duration")
                    break
                # condense evenly (least-condensed round first); among equals, later rounds go first
                r = max(cands, key=lambda r: (-levels[r], r))
                levels[r] += 1
            for r in rounds:
                self.condensed_round(r, self.LEVELS[levels[r]])
        self.trash_tape()
        self.report_card()
        self.close()
        if self.sc.get("comic_timing"):
            self.attach_beat_maps()
        rd = self.to_dict(levels, target_s)
        if getattr(self, "green_report", None):
            rd["all_green"] = self.green_report
        return rd

    def build_segment(self, seg: dict, target_s: float) -> dict:
        """script.segment = {"picks": [7, 8, 9, 10], "says": [77, 75]}: those picks with their host cues and calls,
        and exactly those SAY lines after the pick each answers; the picks before it are already on the board."""
        L = self.L
        picks = [p for p in sorted(L.picks, key=lambda q: q.pick_no) if p.pick_no in set(seg.get("picks") or [])]
        if not picks:
            raise SystemExit("script.segment: no such picks")
        if seg.get("says") is not None:
            want = {int(x) for x in seg["says"]}
            if self.all_green:  # a demo airs exactly as the show would: only the green ones of those lines
                for x in sorted(want - self.complete_says):
                    self.notes.append(f"segment demo: SAY {x} isn't green (or answers a call that doesn't air): "
                                      f"not aired")
                want &= self.complete_says
            self.complete_says = want
        rnd = picks[0].round
        s = self.seg(f"round_{rnd}", f"Round {rnd}", "round_full", f"ROUND {rnd}", rnd)
        for p in picks:
            self.party_pick(s, p, None)
        if self.sc.get("comic_timing"):
            self.attach_beat_maps()
        rd = self.to_dict({}, target_s)
        rd["board_before"] = [p.pick_no for p in L.picks if p.pick_no < picks[0].pick_no]
        rd["segment_demo"] = {"picks": [p.pick_no for p in picks], "says": sorted(self.complete_says)}
        if getattr(self, "green_report", None):
            rd["all_green"] = self.green_report
        return rd

    def complete_selection(self) -> None:
        """The complete show's AIR PLAN (ALL GREEN; script.all_green). A sentence is green when it makes sense, lands
        instantly, gets tonight right, has no fact wrong, isn't mean, isn't catchphrase filler and isn't vetoed; with
        script.require_laugh (the default) a joke-shaped sentence also needs panel-mean funny >= 2.
          Pick calls, sentence by sentence: failing sentences are cut (a setup goes with its joke); the plain pick
        sentence stays; the host announces the pick when the pick sentence fails or nothing of the GM's is left.
          Comebacks / reactions / table talk, all or nothing: every green one in rounds 1..N (comebacks_all_rounds),
        then the judge's top K (comebacks_per_round); a comeback whose call didn't air goes too; one table-talk line
        per round break.
          The aired-context check: every aired line is re-checked against what AIRS before it (the viewer never heard
        the rest): a sentence that refers to an unaired line or a cut sentence is cut (a call) or takes its line down
        (a SAY); repeat until nothing changes.
        Cut models (script.cut_models) never speak. What doesn't air is logged (`dropped`, `trims`; transcript.md)."""
        from .judge import judge_run
        L = self.L
        use_llm = bool(self.sc.get("comic_timing_llm", True))
        J = judge_run(L, log=lambda m: self.notes.append(m), use_llm=use_llm)
        self.J = J
        self.airs_log: list[dict] = []
        passes = 0
        passes = 0
        for _restart in range(5):
            for _ in range(8):
                passes += 1
                self._air_plan()
                if not self.all_green or not self._airs_pass(use_llm):
                    break
            undone = self._undo_stale_repeats() if self.all_green else False
            undone = (self._undo_stale_restores() if self.all_green else False) or undone
            if not undone:
                break
        else:
            self._air_plan()  # (the restart cap: settle on the last plan)
        self.dropped.extend(self.plan_drops)
        if self.all_green:
            self._write_airs_log(passes)
        gm_calls = [pl for pl in self.call_plan.values() if pl["gm"]]
        aired = [pl for pl in gm_calls if not pl["host"]]
        self.green_report = {"calls": len(gm_calls), "calls_aired": len(aired),
                             "calls_trimmed": sum(1 for pl in aired if pl["cut"]),
                             "calls_pick_only": [pl["pick_no"] for pl in aired
                                                 if pl["pick_idx"] is not None and pl["keep"] == [pl["pick_idx"]]],
                             "calls_to_host": [pl["pick_no"] for pl in gm_calls if pl["host"]],
                             "says_aired": len(self.complete_says) + sum(len(v) for v in self.complete_tt.values()),
                             "says_total": sum(1 for x in L.says if x.team in L.teams),
                             "cut": sorted(self.cut), "require_laugh": self.require_laugh, "airs_passes": passes}
        self.notes.append(f"complete (all green{' + a laugh' if self.require_laugh else ''}"
                          f"{'; cut ' + ','.join(sorted(self.cut)) if self.cut else ''}): {len(aired)}/{len(gm_calls)} "
                          f"GM calls air ({self.green_report['calls_trimmed']} trimmed), the host announces the rest; "
                          f"{self.green_report['says_aired']}/{self.green_report['says_total']} table lines air; "
                          f"aired-context check: {passes} pass(es)")

    def _plan_call(self, p: Pick) -> dict:
        """One pick call's air plan: which sentences air, which are cut and why, or why the host announces it."""
        from .judge import FUNNY_MIN, why_not_green
        from .party import name_hit
        L, J = self.L, self.J
        t = L.teams.get(p.team)
        words, kind = self.pick_words(p)
        pl = {"pick_no": p.pick_no, "seq": p.seq, "team": p.team, "gm": False, "parts": [], "keep": [], "cut": {},
              "host": None, "host_kind": None, "pick_idx": None, "edited": {}, "full": T.strip(words or "")}
        if not t or t.is_bot or p.auto or not T.strip(words or ""):
            return pl
        pl["gm"] = True
        facts = p.fact_check if kind == "on_air_call" else getattr(p, "rationale_check", "")
        if (facts or "ok") != "ok":
            pl["host"] = f"fact_check {facts}"
            return pl
        if p.team in self.cut:
            pl["host"] = "cut model (--cut-models)"
            return pl
        if not self.all_green:
            pl.update(parts=[words], keep=[0])
            return pl
        it = J.items.get(f"seq{p.seq}") if J is not None else None
        if it is None:
            pl["host"] = "not judged"
            return pl
        parts = list(it.parts)
        n = len(parts)
        pick_idx = next((i for i, x in enumerate(parts) if name_hit(T.strip(x), p.player_name)), None)
        pl.update(parts=parts, pick_idx=pick_idx)
        vs = [J.v.get(f"{p.seq}.{i}") or {} for i in range(n)]
        cut: dict[int, str] = {}
        for i, v in enumerate(vs):
            if i in self.airs_cut.get(p.seq, {}):
                cut[i] = f"refers to what the viewer never heard (“{self.airs_cut[p.seq][i][:70]}”)"
            elif i in self.rep_cut.get(p.seq, {}):
                cut[i] = self.rep_cut[p.seq][i]
            elif v.get("vetoed"):
                cut[i] = "owner veto"
            elif not v.get("judged"):
                cut[i] = "not judged"
            elif v.get("catchphrase") and not v.get("forced"):
                cut[i] = "catchphrase filler"
            elif not v.get("green"):
                cut[i] = why_not_green(v) or "not green"
        if pick_idx is not None and pick_idx in cut:
            pl.update(cut=cut, host=f"the pick sentence {cut[pick_idx]}")
            return pl
        if self.require_laugh:  # jokes need a laugh; a setup airs with its joke; the plain pick sentence needs none
            segs = [range(0, pick_idx), range(pick_idx + 1, n)] if pick_idx is not None else [range(0, n)]
            for seg in segs:
                alive = [i for i in seg if i not in cut]
                fun = {i: float(vs[i].get("funny") or 0.0) for i in alive}
                jokes = [i for i in alive if fun[i] >= FUNNY_MIN or vs[i].get("forced")]
                keep_seg = set(jokes)
                for j in jokes:
                    k = j - 1
                    while k in seg and k in fun and k not in keep_seg:
                        keep_seg.add(k)
                        k -= 1
                for i in alive:
                    if i not in keep_seg:
                        if i in self.restore.get(p.seq, set()):
                            continue  # a setup its payoff needs (the owner: "air them together")
                        cut[i] = f"no laugh (judge funny {fun[i]:g} < {FUNNY_MIN:g})"
        edited: dict[int, str] = {}
        if cut and len(cut) < n:
            # (1) "you" with nobody named: the sentence that named the addressee was cut
            while True:
                keep_now = [i for i in range(n) if i not in cut]
                named = {i for i in range(n) if L.mentioned_team(T.strip(parts[i]), {p.team})}
                if not any(k in named for k in cut):
                    break
                orphan = next((i for i in keep_now if SECOND_PERSON.search(T.strip(parts[i]))
                               and not ROOM_ADDRESS.search(T.strip(parts[i])) and i not in named
                               and not any(k in named for k in keep_now if k < i)), None)
                if orphan is None:
                    break
                cut[orphan] = "“you” with nobody named (the sentence that named the addressee was cut)"
                if orphan == pick_idx:
                    pl.update(cut=cut, host=f"the pick sentence {cut[orphan]}")
                    return pl
            # (1b) a connective leaning on a cut sentence ("And GLM, ...", "But ...", "So ..."): dropped if the rest stands
            for i in range(n):
                if i in cut or not (i - 1 in cut or (i > 0 and all(k in cut for k in range(i)))):
                    continue
                new = strip_connective(parts[i])
                if new is not None:
                    edited[i] = new
        keep = [i for i in range(n) if i not in cut]
        pl.update(cut=cut, keep=keep, edited=edited)
        if not keep:
            pl["host"] = "nothing of the GM's left (" + "; ".join(sorted(set(cut.values())))[:140] + ")"
        elif cut and keep == [pick_idx] and bare_pick(edited.get(pick_idx, parts[pick_idx]), p.player_name):
            # (2) down to the player's name (or the name and a stat fragment): the host's energetic call says it better
            pl["host"] = "down to the bare pick after cuts: the host calls it (" + \
                "; ".join(f"“{T.strip(parts[i])[:40]}” {why}" for i, why in sorted(cut.items()))[:260] + ")"
            pl["host_kind"] = "bare"
        return pl

    def _air_plan(self) -> None:
        """Calls sentence by sentence, then the SAY lines all or nothing (given the calls that air)."""
        from .judge import FUNNY_MIN, rank_funny, why_not_green
        L, J = self.L, self.J
        all_n = int(self.sc.get("comebacks_all_rounds", 3))
        cap = int(self.sc.get("comebacks_per_round", 3))
        tt_cap = int(self.sc.get("table_talk_per_break", 1))
        by_no = {p.pick_no: p for p in L.picks}
        drops: list[dict] = []
        self.call_plan = {p.pick_no: self._plan_call(p) for p in L.picks}
        for pl in self.call_plan.values():
            if pl["gm"] and pl["host"]:
                drops.append({"what": "call", "pick_no": pl["pick_no"], "seq": pl["seq"], "speaker": pl["team"],
                              "reason": pl["host"]})

        def verdict(say) -> tuple[tuple | None, str | None]:
            if say.team in self.cut:
                return None, "cut model (--cut-models)"
            if say.kind == "comeback":
                cp = self.call_plan.get(say.pick_no)
                if cp and cp["gm"] and cp["host"] and cp.get("host_kind") != "bare":
                    return None, "answers a call that didn't air"
            if say.seq in self.airs_drop:
                return None, f"refers to what the viewer never heard (“{self.airs_drop[say.seq][:70]}”)"
            if say.seq in self.rep_drop:
                return None, self.rep_drop[say.seq]
            it = J.items.get(f"seq{say.seq}") if J is not None else None
            if not it:
                return None, "not judged"
            vs = [J.v.get(f"{say.seq}.{i}") for i in range(len(it.parts))]
            if any(v and v.get("vetoed") for v in vs):
                return None, "owner veto"
            if not vs or any(v is None or not v.get("judged") for v in vs):
                return None, "not judged (judge outage?)"
            if self.all_green:
                vs = [v for v in vs if not v.get("catchphrase")]  # (a catchphrase sentence is trimmed: judge the rest)
                if not vs:
                    return None, "catchphrase filler"
                bad = next((v for v in vs if not (v.get("green") or v.get("forced"))), None)
                if bad is not None:
                    return None, why_not_green(bad) or "not green"
                if self.require_laugh and not any(v.get("forced") for v in vs) and say.seq not in self.restore_says:
                    fs = [float(v.get("funny") or 0.0) for v in vs]
                    if max(fs) < FUNNY_MIN:
                        return None, f"no laugh (best judge funny {max(fs):g} < {FUNNY_MIN:g})"
                    last = max(i for i, f in enumerate(fs) if f >= FUNNY_MIN)
                    flat = [f for f in fs[last + 1:] if 0.75 <= f < FUNNY_MIN]
                    if flat:
                        return None, f"a flat joke after the laugh (judge funny {flat[0]:g})"
            f = [rank_funny(v) for v in vs]
            return (max(f), sum(f) / len(f), -len(say.line), -say.seq), None

        def vetoed(say) -> bool:
            it = J.items.get(f"seq{say.seq}") if J is not None else None
            return bool(it) and any((J.v.get(f"{say.seq}.{i}") or {}).get("vetoed") for i in range(len(it.parts)))

        def choose(pool: list, k: int, what: str, rnd: int) -> set[int]:
            ranked, out = [], set()
            for x in pool:
                sc, why = verdict(x)
                if sc is None:
                    drops.append({"what": what, "seq": x.seq, "round": rnd, "reason": f"complete: {why}"})
                else:
                    ranked.append((sc, x))
            ranked.sort(key=lambda r: r[0], reverse=True)
            for sc, x in ranked[:k]:
                out.add(x.seq)
            for sc, x in ranked[k:]:
                drops.append({"what": what, "seq": x.seq, "round": rnd,
                              "reason": f"complete: not in round {rnd}'s top {k} (judge funny {sc[0]:g})"})
            return out

        self.complete_says = set()
        self.complete_tt = {}
        for rnd in sorted({p.round for p in L.picks}):
            pool = [x for x in L.says if x.kind in ("reaction", "comeback") and x.pick_no in by_no
                    and by_no[x.pick_no].round == rnd and x.team in L.teams]
            for x in pool:
                if not x.airable:
                    drops.append({"what": "comeback", "seq": x.seq, "round": rnd,
                                  "reason": f"complete: fact_check {x.fact_check}"})
            pool = [x for x in pool if x.airable]
            if rnd <= all_n and not self.all_green:
                keep = {x.seq for x in pool if not vetoed(x) and x.team not in self.cut}
                for x in pool:
                    if x.seq not in keep:
                        drops.append({"what": "comeback", "seq": x.seq, "round": rnd,
                                      "reason": "complete: " + ("cut model" if x.team in self.cut else "owner veto")})
            else:
                keep = choose(pool, 999 if rnd <= all_n else cap, "comeback", rnd)
            self.complete_says |= keep
            tt_all = L.table_talk(rnd)
            for x in tt_all:
                if not x.airable:
                    drops.append({"what": "table_talk", "seq": x.seq, "round": rnd,
                                  "reason": f"complete: fact_check {x.fact_check}"})
            tt = [x for x in tt_all if x.airable and x.team in L.teams]
            self.complete_tt[rnd] = sorted(choose(tt, tt_cap, "table_talk", rnd))
        self.plan_drops = drops

    def _gm_timeline(self) -> list[dict]:
        """Every GM line of the draft in ledger order, as the complete show airs it (or doesn't)."""
        L, J = self.L, self.J
        tt_aired = {x for v in self.complete_tt.values() for x in v}
        rows = []
        for p in L.picks:
            pl = self.call_plan.get(p.pick_no)
            if not pl or not pl["gm"]:
                continue
            aired = not pl["host"] and bool(pl["keep"])
            prev = next((q for q in L.picks if q.pick_no == p.pick_no - 1), None)
            rows.append({"seq": p.seq, "kind": "call", "team": p.team, "pick_no": p.pick_no, "pick_idx": pl["pick_idx"],
                         "to": prev.team if prev is not None and prev.team != p.team else None,
                         "aired": aired, "aired_idx": list(pl["keep"]) if aired else [],
                         "aired_parts": [pl.get("edited", {}).get(i, pl["parts"][i]) for i in pl["keep"]] if aired else [],
                         "cut_own": [pl["parts"][i] for i in sorted(pl["cut"])] if aired else [],
                         "full": pl["full"], "why": pl["host"]})
        for x in L.says:
            if x.team not in L.teams:
                continue
            it = J.items.get(f"seq{x.seq}") if J is not None else None
            parts = list(it.parts) if it else [x.line]
            aired = x.seq in self.complete_says or x.seq in tt_aired
            cp = set()
            if aired and it:
                cp = {i for i in range(len(parts)) if (J.v.get(f"{x.seq}.{i}") or {}).get("catchphrase")}
            rows.append({"seq": x.seq, "kind": x.kind, "team": x.team, "pick_no": x.pick_no, "pick_idx": None,
                         "to": x.addressed_to,
                         "aired": aired, "aired_idx": [i for i in range(len(parts)) if i not in cp] if aired else [],
                         "aired_parts": [parts[i] for i in range(len(parts)) if i not in cp] if aired else [],
                         "cut_own": [parts[i] for i in sorted(cp)] if aired else [], "full": T.strip(x.line),
                         "why": None})
        rnd_of = {p.pick_no: p.round for p in L.picks}
        says_by = {x.seq: x for x in L.says}

        def air_key(r: dict) -> tuple:
            if r["kind"] == "call":
                return (rnd_of.get(r["pick_no"], 0), r["pick_no"], 0, r["seq"])
            if r["kind"] == "table_talk":
                rnd = getattr(says_by.get(r["seq"]), "round", None) or 0
                return (rnd, 10 ** 6, 2, r["seq"])
            if r.get("pick_no") in rnd_of:
                return (rnd_of[r["pick_no"]], r["pick_no"], 1, r["seq"])
            return (10 ** 6, 10 ** 6, 3, r["seq"])
        rows.sort(key=air_key)
        for k, r in enumerate(rows):
            r["pos"] = k
        return rows

    def _airs_pass(self, use_llm: bool) -> bool:
        """One round of the aired-context check. -> True when something more was cut."""
        from .judge import _nick, airs_check, board_context
        L = self.L
        rows = self._gm_timeline()
        by_no = {p.pick_no: p for p in L.picks}

        def label(r: dict) -> str:
            k = r["kind"]
            what = (f"pick call at #{r['pick_no']}" if k == "call" else
                    f"{k} after #{r['pick_no']}" if r.get("pick_no") else k.replace("_", " "))
            return f"{_nick(L, r['team'])} ({what})"

        def render(r: dict) -> str:
            if not r["aired"]:
                why = " -- the host announced the pick instead" if r["kind"] == "call" else ""
                return f"[NOT AIRED{why}] {label(r)}: \"{r['full'][:300]}\""
            said = " ".join(T.strip(x) for x in r["aired_parts"])
            if r["cut_own"]:
                cut = " ".join(T.strip(x) for x in r["cut_own"])
                return f"[AIRED IN PART] {label(r)}: aired \"{said[:300]}\" · CUT, never heard: \"{cut[:200]}\""
            return f"[AIRED] {label(r)}: \"{said[:300]}\""

        checks = []
        for k, r in enumerate(rows):
            if not r["aired"]:
                continue
            window = rows[max(0, k - (12 if r["kind"] == "table_talk" else 8)):k]
            if not window:
                continue
            p = by_no.get(r["pick_no"]) if r["kind"] == "call" else None
            board = board_context(L, r["seq"], this_pick=p, speaker=r["team"])[0]
            head = label(r) + (f", aimed at {_nick(L, r['to'])}" if r.get("to") and r["to"] in L.teams else "")
            checks.append({"id": f"air{r['seq']}", "seq": r["seq"], "kind": r["kind"], "head": head,
                           "to": r.get("to"),
                           "context": [board, "EARLIER AT THE TABLE (oldest first):"]
                                      + [f"L{n + 1} {render(w)}" for n, w in enumerate(window)],
                           "tags": {f"L{n + 1}": w["seq"] for n, w in enumerate(window)},
                           "window_seqs": [w["seq"] for w in window],
                           "parts": r["aired_parts"], "idx": r["aired_idx"], "cut_own": r["cut_own"],
                           "unaired": [w["full"] for w in window if not w["aired"]]
                                      + [T.strip(x) for w in window for x in w["cut_own"]] + list(r["cut_own"])})
        if not checks:
            return False
        res = airs_check(checks, self.J.models, log=lambda m: self.notes.append(m), use_llm=use_llm,
                         log_rows=self.airs_log)
        changed = self._shape_repeats(rows)
        changed = self._echo_backstop(rows) or changed
        by_seq = {r["seq"]: r for r in rows}
        for ck in checks:
            r = res.get(ck["id"])
            lex = self._lexical_orphans(ck)  # a distinctive word shared with words the viewer never heard
            if r is None:  # no judge answered: the lexical check alone
                bad = lex
                self.notes.append(f"aired-context check: {ck['id']}: no judge answered; lexical fallback")
            else:  # every judge that answered agrees -- or one does and the words back it up
                bad = {i: refs[0] for i, refs in r["flags"].items()
                       if len(refs) >= max(1, r["answered"]) or i in lex or self._corrects_unheard(ck, refs, by_seq)}
                # setup + payoff (the owner: "air them together"): a laugh that mocks a sentence cut ONLY for having no
                # laugh brings that setup back -- one judge seeing the link is enough (restoring a green setup is cheap)
                for i in sorted(set(r["flags"]) | set(bad)):
                    quotes = r["flags"].get(i) or ([bad[i]] if i in bad else [])
                    if self._funny(ck["seq"], i) >= 2.0 and any(self._pair_setup(ck, q, (ck["seq"], i)) for q in quotes):
                        bad.pop(i, None)
                        changed = True
                for i, quotes in r.get("repeats", {}).items():  # an echo of an aired line: the funnier one stays
                    if i in bad or i in self.rep_cut.get(ck["seq"], {}) or ck["seq"] in self.rep_drop:
                        continue
                    tag = next((m.group(0) for q in quotes for m in [re.search(r"\bL\d+\b", q)] if m), None)
                    changed |= self._resolve_repeat(ck["seq"], i, quotes[0], rows, by_seq, "makes the same point as",
                                                    tag_seq=ck["tags"].get(tag) if tag else None)
            if not bad:
                continue
            if ck["kind"] == "call":
                cur = self.airs_cut.setdefault(ck["seq"], {})
                new = {i: w for i, w in bad.items() if i not in cur}
                if new:
                    cur.update(new)
                    changed = True
            elif ck["seq"] not in self.airs_drop:
                self.airs_drop[ck["seq"]] = next(iter(bad.values()))
                changed = True
        return changed

    NOT_X_THATS_Y = re.compile(
        r"\b(?:that'?s|that is|it'?s|it is|you'?re|you are|he'?s|he is|this is|this'?s|we'?re|they'?re|i'?m)\s+"
        r"(?:not|no)\b[^.!?]{1,70}?[,;:—–-]+\s*(?:that'?s|that is|it'?s|it is|you'?re|you are|he'?s|he is|this is|"
        r"this'?s|we'?re|they'?re|i'?m|just|more like)\b"
        r"|\b(?:isn'?t|aren'?t|wasn'?t|ain'?t)\b[^.!?]{1,60}?[,;:—–-]+\s*(?:it'?s|that'?s|he'?s|they'?re|you'?re)\b"
        r"|\bnot\s+[^.!?,;]{1,40}[,;:—–-]+\s*(?:that'?s|it'?s|you'?re)\b", re.I)

    HYPOCRISY = re.compile(
        r"\b(?:you\s+|to\s+)?(?:chirp|roast|mock|scold|brag|lectur|rib|dunk|grill|trash|clown|call(?:ed)? out)\w*\b"
        r"[^.!?]{0,90}?\b(?:and\s+)?then\b", re.I)
    SHAPES = (("“that's not X, that's Y”", "NOT_X_THATS_Y"), ("“you chirped X for Y, then did Y”", "HYPOCRISY"))

    def _unit_funny(self, row: dict, i: int) -> float:
        """What a repeat costs: a call loses one sentence (its score); a SAY line goes whole (its best score)."""
        if row["kind"] == "call":
            return self._funny(row["seq"], i)
        return max([self._funny(row["seq"], j) for j in row["aired_idx"]] or [0.0])

    LAUGH_ONLY = re.compile(r"^(?:complete: )?(?:no laugh|a flat joke after the laugh)")

    def _pair_setup(self, ck: dict, quote: str, payoff: tuple) -> bool:
        """The payoff mocks a setup the viewer never heard because it was cut ONLY for having no laugh: put the setup
        back on air (the owner: "Fable should be allowed to guess, then the other can mock him"). -> True if restored."""
        from .tighten import content_words
        q = re.sub(r"^\s*L\d+\s*(?:\([^)]*\))?\s*[:\-–—]?\s*", "", quote or "")
        q = re.sub(r"(?i)^[^:\"“]*(?:cut|not aired)[^:\"“]*[:\"“]\s*", "", q).strip(" '\"“”")
        qw = content_words(q)
        drops = {d.get("seq"): d.get("reason", "") for d in self.plan_drops if d.get("seq") is not None}
        tt = {x for v in self.complete_tt.values() for x in v}
        says = {x.seq: x for x in self.L.says}
        best, best_sc = None, 0.0

        def score(text: str) -> float:
            if q and len(q) >= 6 and q.lower()[:40] in text.lower():
                return 1.0
            pw = content_words(text)
            return len(qw & pw) / max(1, len(qw | pw))
        for seq in [ck["seq"]] + list(ck.get("window_seqs") or []):
            pl = next((x for x in self.call_plan.values() if x["seq"] == seq and x["gm"]), None)
            if pl is not None:
                for j, why in pl["cut"].items():
                    sc = score(T.strip(pl["parts"][j]))
                    if sc > best_sc:
                        best, best_sc = ("call", seq, j, why, T.strip(pl["parts"][j])), sc
            elif seq in says and seq not in self.complete_says and seq not in tt:
                sc = score(T.strip(says[seq].line))
                if sc > best_sc:
                    best, best_sc = ("say", seq, None, drops.get(seq, ""), T.strip(says[seq].line)), sc
        if best is None or best_sc < 0.2:
            return False
        kind, seq, j, why, text = best
        if not self.LAUGH_ONLY.search(why or ""):
            return False
        if not self._own_pick_guess(kind, seq, j, text):
            return False  # (the exception is for a guess about the speaker's OWN pick, nothing else)
        if kind == "call":
            cur = self.restore.setdefault(seq, set())
            if j in cur:
                return False
            cur.add(j)
        else:
            if seq in self.restore_says:
                return False
            self.restore_says.add(seq)
        self.restore_meta[(seq, j)] = payoff
        self.notes.append(f"setup + payoff: “{text[:60]}” airs for the payoff at seq {payoff[0]}")
        return True

    GUESS_WORDS = re.compile(
        r"\b(?:math|seconds?|minutes?|fast(?:er|est)?|quick(?:er|est)?|speed|think(?:ing)?|thought|lookups?|"
        r"compute)\b", re.I)

    def _own_pick_guess(self, kind: str, seq: int, j: int | None, text: str) -> bool:
        """A pick call's sentence guessing about the speaker's OWN pick -- the robot would (not) take him, how fast it
        decided -- and naming no other model."""
        if kind != "call" or j is None:
            return False
        pl = next((x for x in self.call_plan.values() if x["seq"] == seq and x["gm"]), None)
        if pl is None or j == pl["pick_idx"]:
            return False
        if self.L.mentioned_team(T.strip(text), {pl["team"]}):
            return False  # it's about another model
        return bool(self.GUESS_WORDS.search(T.strip(text)))

    def _undo_stale_restores(self) -> bool:
        """A restored setup whose payoff no longer airs goes back to being cut (-> True: plan again)."""
        stale = [k for k, pay in self.restore_meta.items() if not self._airs_now(*pay)]
        for seq, j in stale:
            self.restore_meta.pop((seq, j), None)
            if j is None:
                self.restore_says.discard(seq)
            else:
                self.restore.get(seq, set()).discard(j)
        return bool(stale)

    def _airs_now(self, seq: int, i: int | None) -> bool:
        """Does sentence i of line `seq` air in the current plan? (i None: the line)"""
        pl = next((x for x in self.call_plan.values() if x["seq"] == seq), None)
        if pl is not None:
            return not pl["host"] and (i is None or i in pl["keep"])
        tt = {x for v in self.complete_tt.values() for x in v}
        return seq in self.complete_says or seq in tt

    def _undo_stale_repeats(self) -> bool:
        """A repeat cut is stale when the line it was compared with no longer airs: undo it (-> True: plan again)."""
        stale = [k for k, other in self.rep_meta.items() if other is not None and not self._airs_now(*other)]
        for seq, i in stale:
            self.rep_meta.pop((seq, i), None)
            if i is None:
                self.rep_drop.pop(seq, None)
            else:
                self.rep_cut.get(seq, {}).pop(i, None)
        if stale:
            self.notes.append(f"repeated bits: {len(stale)} cut(s) undone (the line they repeated doesn't air)")
        return bool(stale)

    def _funny(self, seq: int, i: int) -> float:
        return float((self.J.v.get(f"{seq}.{i}") or {}).get("funny") or 0.0) if self.J is not None else 0.0

    def _cut_repeat(self, row: dict, i: int, why: str, other: tuple | None = None) -> bool:
        """Cut sentence i of an aired row (a call loses the sentence; a SAY line goes whole). A pick sentence is
        never cut for a repeat (the caller keeps the other one)."""
        if row["kind"] == "call" and i == row.get("pick_idx"):
            return False
        if row["kind"] == "call":
            cur = self.rep_cut.setdefault(row["seq"], {})
            if i in cur:
                return False
            cur[i] = why
            self.rep_meta[(row["seq"], i)] = other
            return True
        if row["seq"] in self.rep_drop:
            return False
        self.rep_drop[row["seq"]] = why
        self.rep_meta[(row["seq"], None)] = other
        return True

    def _resolve_repeat(self, seq: int, i: int, quote: str, rows: list[dict], by_seq: dict, what: str,
                        tag_seq: int | None = None) -> bool:
        """Sentence i of line `seq` repeats an aired sentence above (the judge names the line; its words locate the
        sentence): keep the funnier one. Nothing identifiable to compare with: keep both."""
        from .tighten import content_words
        me = by_seq.get(seq)
        if me is None or i not in me["aired_idx"] or not me.get("to"):
            return False
        mine = T.strip(me["aired_parts"][me["aired_idx"].index(i)])
        q = re.sub(r"^\s*L\d+\s*(?:\([^)]*\))?\s*[:\-–—]?\s*", "", quote or "")
        qw, mw = content_words(q), content_words(mine)
        best, best_sc = None, 0.0
        # the same target (both lines aimed at the same model), aired before it -- the named line first
        pool = [r for r in rows if r["pos"] < me["pos"] and r["aired"]][-8:]
        same = [r for r in pool if r.get("to") == me["to"] and not (
            me["kind"] in ("comeback", "reaction") and r["kind"] == "call" and me.get("pick_no") == r.get("pick_no"))]
        if not same:
            return False
        tagged = by_seq.get(tag_seq) if tag_seq else None
        cands = [tagged] if tagged is not None and tagged in same else same
        for r in cands:
            for part, j in zip(r["aired_parts"], r["aired_idx"]):
                pw = content_words(T.strip(part))
                sc = max(len(qw & pw) / max(1, len(qw | pw)), len(mw & pw) / max(1, len(mw | pw)))
                if q.strip().lower()[:30] and q.strip().lower()[:30] in T.strip(part).lower():
                    sc = 1.0
                if sc > best_sc:
                    best, best_sc = (r, j, part), sc
        if tagged is not None and cands == [tagged] and best is None and tagged["aired_parts"]:
            r = tagged  # the judge named the line: its funniest sentence is the bit
            j = max(r["aired_idx"], key=lambda x: self._funny(r["seq"], x))
            best, best_sc = (r, j, r["aired_parts"][r["aired_idx"].index(j)]), 0.2
        if best is None or best_sc < (0.0 if tagged is not None and cands == [tagged] else 0.15):
            return False
        if best[0]["seq"] == seq:
            return False
        r, j, part = best
        if r["kind"] == "call" and j == r.get("pick_idx"):
            return self._cut_repeat(me, i, f"repeated bit: {what} an aired line (“{T.strip(part)[:60]}”)", (r["seq"], j))
        if me["kind"] == "call" and i == me.get("pick_idx"):
            return self._cut_repeat(r, j, f"repeated bit: a later line {what} it (“{mine[:60]}”)", (seq, i))
        f_me, f_it = self._unit_funny(me, i), self._unit_funny(r, j)
        if f_me > f_it:  # the later one is funnier: the earlier one goes
            return self._cut_repeat(r, j, f"repeated bit: a funnier line later {what} it (“{mine[:60]}”, funny "
                                          f"{f_me:g} vs {f_it:g})", (seq, i))
        return self._cut_repeat(me, i, f"repeated bit: {what} an aired line (“{T.strip(part)[:60]}”, funny "
                                       f"{f_it:g} vs {f_me:g})", (r["seq"], j))

    @staticmethod
    def _shape_x(txt: str) -> set[str]:
        """The X of "that's not X, that's Y" ("scouting"), as distinctive words: the same X twice is the same bit."""
        from .tighten import content_words
        m = re.search(r"\bnot\s+(?:a\s+|an\s+|the\s+)?([^,;:—–.!?]{3,40}?)\s*[,;:—–-]", txt, re.I)
        return {w for w in content_words(m.group(1)) if len(w) >= 5} if m else set()

    def _shape_repeats(self, rows: list[dict]) -> bool:
        """The same joke SHAPE twice within 8 aired lines ("that's not X, that's Y"; "you chirped X for Y, then did
        Y") -- or anywhere in the show when it's the same "not X" ("not scouting, that's shoplifting" / "not scouting,
        that's a mood") or the same model's tic: the funnier one stays (ties: the earlier)."""
        aired = [r for r in rows if r["aired"]]
        changed = False
        for label, attr in self.SHAPES:
            rx = getattr(self, attr)
            seen: list[tuple[int, dict, int, str, set]] = []  # (position among aired lines, row, sentence, text, X)

            def gone(x) -> bool:
                return (x[1]["kind"] == "call" and x[2] in self.rep_cut.get(x[1]["seq"], {})) or \
                    x[1]["seq"] in self.rep_drop
            for pos, r in enumerate(aired):
                for part, i in zip(r["aired_parts"], r["aired_idx"]):
                    txt = T.strip(part)
                    if not rx.search(txt) or (r["kind"] == "call" and i == r.get("pick_idx")):
                        continue
                    if (r["kind"] == "call" and i in self.rep_cut.get(r["seq"], {})) or r["seq"] in self.rep_drop:
                        continue
                    xk = self._shape_x(txt) if attr == "NOT_X_THATS_Y" else set()
                    seen = [x for x in seen if not gone(x)]
                    prev = next((x for x in reversed(seen) if x[1]["seq"] != r["seq"] and pos - x[0] <= 8), None) or \
                        next((x for x in reversed(seen) if x[1]["seq"] != r["seq"] and xk and x[4] & xk), None) or \
                        next((x for x in reversed(seen) if x[1]["seq"] != r["seq"] and x[1]["team"] == r["team"]), None)
                    if prev is None:
                        seen.append((pos, r, i, txt, xk))
                        continue
                    same_x = bool(xk and prev[4] & xk)
                    tag = (f"{label}{', the same “not ' + sorted(xk & prev[4])[0] + '”' if same_x else ''}"
                           + (", the same model again" if prev[1]["team"] == r["team"] and pos - prev[0] > 8 and
                              not same_x else ""))
                    f_me, f_it = self._unit_funny(r, i), self._unit_funny(prev[1], prev[2])
                    if f_me > f_it:
                        changed |= self._cut_repeat(prev[1], prev[2], f"repeated bit ({tag}): a funnier one follows "
                                                                      f"(“{txt[:60]}”, funny {f_me:g} vs {f_it:g})",
                                                    (r["seq"], i))
                        seen = [x for x in seen if x is not prev] + [(pos, r, i, txt, xk)]
                    else:
                        changed |= self._cut_repeat(r, i, f"repeated bit ({tag}) after “{prev[3][:60]}” (funny "
                                                          f"{f_it:g} vs {f_me:g})", (prev[1]["seq"], prev[2]))
        return changed

    NUMBERS = {w: n for n, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen".split())}
    TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
            "ninety": 90}
    FACT_UNITS = {"lookup", "second", "minute", "hour", "millisecond", "goal", "point", "assist", "win", "game",
                  "season", "shot", "save", "compute", "research"}
    ROBOT_REF = re.compile(r"\b(?:robot|autodraft|the code|bot)\b", re.I)
    ROBOT_YES = re.compile(r"\b(?:agree[sd]?|wanted\b[^.!?]{0,40}\btoo|same (?:pick|player|guy)|matched|copying|"
                           r"copied|robot'?s pick|would(?:'ve| have) (?:made|taken)|was standing|through you|"
                           r"checked your work)\b", re.I)
    ROBOT_NO = re.compile(r"\b(?:said no|passed on|passed|skipped|didn'?t (?:even )?want|disagree[sd]?|never got to|"
                          r"wouldn'?t|had (?:somebody|someone) else|wasn'?t the robot'?s)\b", re.I)

    def _key_facts(self, text: str) -> set[tuple]:
        """The key facts a sentence states: (number, unit) pairs ('seventeen lookups' -> (17, 'lookup')) and a robot
        agreed / didn't point."""
        t = T.strip(text).lower().replace("’", "'")
        words = re.findall(r"[a-z0-9']+(?:-[a-z]+)?", t)
        out: set[tuple] = set()
        for k, w in enumerate(words[:-1]):
            n = None
            if w.isdigit():
                n = int(w)
            elif w in self.NUMBERS:
                n = self.NUMBERS[w]
            elif w in self.TENS:
                n = self.TENS[w]
            elif "-" in w and w.split("-")[0] in self.TENS and w.split("-")[1] in self.NUMBERS:
                n = self.TENS[w.split("-")[0]] + self.NUMBERS[w.split("-")[1]]
            if n is None:
                continue
            for u in words[k + 1:k + 3]:  # "seventeen lookups", "twenty-three research lookups"
                u = u.rstrip("s") if u not in ("compute",) else u
                if u in self.FACT_UNITS and u != "research":
                    out.add((n, u))
                    break
        if self.ROBOT_REF.search(t):
            yes, no = bool(self.ROBOT_YES.search(t)), bool(self.ROBOT_NO.search(t))
            if yes != no:
                out.add(("robot", "agreed" if yes else "didn't"))
        return out

    def _echo_backstop(self, rows: list[dict]) -> bool:
        """Two aired sentences aimed at the same model within 3 aired lines, sharing a key fact: the funnier stays
        (ties: the earlier). A pick sentence never goes."""
        aired = [r for r in rows if r["aired"]]
        changed = False
        facts: list[tuple] = []  # (position, row, sentence, text, target, facts)
        for pos, r in enumerate(aired):
            for part, i in zip(r["aired_parts"], r["aired_idx"]):
                if (r["kind"] == "call" and i in self.rep_cut.get(r["seq"], {})) or r["seq"] in self.rep_drop:
                    continue
                txt = T.strip(part)
                kf = self._key_facts(txt)
                if not kf:
                    continue
                target = self.L.mentioned_team(txt, {r["team"]}) or r.get("to")
                if not target:
                    continue
                prev = next((x for x in reversed(facts) if pos - x[0] <= 3 and x[1]["seq"] != r["seq"]
                             and x[4] == target and x[5] & kf and not (
                                 (x[1]["kind"] == "call" and x[2] in self.rep_cut.get(x[1]["seq"], {}))
                                 or x[1]["seq"] in self.rep_drop)), None)
                if prev is None:
                    facts.append((pos, r, i, txt, target, kf))
                    continue
                shared = sorted(prev[5] & kf, key=str)[0]
                what = (f"the same “{shared[0]} {shared[1]}{'s' if shared[0] != 1 else ''}”" if shared[0] != "robot"
                        else f"the same point that the robot {shared[1]}")
                me_pick = r["kind"] == "call" and i == r.get("pick_idx")
                it_pick = prev[1]["kind"] == "call" and prev[2] == prev[1].get("pick_idx")
                f_me, f_it = self._unit_funny(r, i), self._unit_funny(prev[1], prev[2])
                if it_pick and me_pick:
                    facts.append((pos, r, i, txt, target, kf))
                    continue
                if (f_me > f_it and not it_pick) or me_pick:
                    changed |= self._cut_repeat(prev[1], prev[2], f"echo ({what} about the same model): a funnier "
                                                                  f"line follows (“{txt[:60]}”, funny {f_me:g} vs "
                                                                  f"{f_it:g})", (r["seq"], i))
                    facts = [x for x in facts if x is not prev] + [(pos, r, i, txt, target, kf)]
                else:
                    changed |= self._cut_repeat(r, i, f"echo ({what} about the same model) after “{prev[3][:60]}” "
                                                      f"(funny {f_it:g} vs {f_me:g})", (prev[1]["seq"], prev[2]))
        return changed

    def _echo_backed(self, ck: dict, quotes: list[str]) -> bool:
        """One judge's 'repeats' counts when the words back it up (a distinctive word shared with the quoted line)."""
        from .tighten import content_words
        mine = set()
        for part in ck["parts"]:
            mine |= {w for w in content_words(T.strip(part)) if len(w) >= 5}
        for q in quotes:
            shared = mine & {w for w in content_words(q) if len(w) >= 5}
            if len(shared) >= 2 or any(len(w) >= 7 for w in shared):
                return True
        return False

    CORRECTS = re.compile(r"\b(?:never|missed|wrong|actually|nope|newsflash|except|you said|you claimed|so much for|"
                          r"fact[- ]check|not even|didn'?t even|in fact|for the record|correction)\b", re.I)

    def _corrects_unheard(self, ck: dict, quotes: list[str], by_seq: dict) -> bool:
        """The flagged sentence answers words its addressee said that the viewer never heard (a cut sentence or an
        unaired line of the model it's aimed at): the claim it corrects doesn't air, so it drops (one judge is
        enough)."""
        from .tighten import content_words
        to = ck.get("to")
        if not to:
            return False
        idx = [k for k, q in enumerate(ck["parts"])]
        if not any(self.CORRECTS.search(T.strip(ck["parts"][k])) for k in idx):
            return False  # (it must dispute something: "never", "you missed that", "wrong", "so much for" ...)
        texts = []
        for seq in ck.get("window_seqs") or []:
            w = by_seq.get(seq)
            if w is None or w["team"] != to:
                continue
            texts += [w["full"]] if not w["aired"] else [T.strip(x) for x in w["cut_own"]]
        for q in quotes:
            qq = re.sub(r"^\s*L\d+\s*(?:\([^)]*\))?\s*[:\-–—]?\s*", "", q or "").strip(" '\"“”")
            qw = content_words(qq)
            for t in texts:
                tw = content_words(t)
                if (len(qq) >= 8 and qq.lower()[:30] in t.lower()) or len(qw & tw) / max(1, len(qw | tw)) >= 0.25:
                    return True
        return False

    def _lexical_orphans(self, ck: dict) -> dict[int, str]:
        from .judge import NHL
        from .tighten import callback_words, content_words, strong_callback
        L = self.L
        raw = " ".join([t.display + " " + (t.gm_name or "") + " " + (t.franchise_name or "") + " " + t.call_name
                        for t in L.teams.values()] + list(NHL.values()) + [p.player_name for p in L.picks])
        names = content_words(raw) | {w.lower().strip("'\".,!?") for w in raw.split()}
        out = {}
        for part, i in zip(ck["parts"], ck["idx"]):
            mine = {w for w in callback_words(T.strip(part), names) if len(w) >= 5}
            for u in ck["unaired"]:
                shared = mine & {w for w in callback_words(u, names) if len(w) >= 5}
                if len(shared) >= 2 or any(len(w) >= 7 for w in shared):
                    out[i] = u[:120]
                    break
        return out

    def _write_airs_log(self, passes: int) -> None:
        from .judge import review_dir
        d = review_dir(self.L.run)
        rows = [f"# Aired-context check: {self.L.run}", "",
                f"Every aired line re-checked against what AIRS before it ({passes} pass(es) until stable). "
                f"ORPHANED = the sentence refers to a line or sentence the viewer never heard; it is cut when every judge "
                f"that answered agrees, or one does and a distinctive word it shares with the unaired words backs it "
                f"up.",
                "", "| line | judge | sentence | verdict | depends on |", "|---|---|---|---|---|"]
        last = {}
        for r in self.airs_log:  # (a later pass re-checks a line whose context changed: its verdict wins)
            last[(r["line"], r["model"], r["i"])] = r
        for r in sorted(last.values(), key=lambda x: (int(x["line"][3:]), x["i"], x["model"])):
            rows.append(f"| {r['line']} | {r['model'].split('/')[-1]} | {r['sentence'].replace('|', '/')} | "
                        f"{r['verdict']} | {r['refers_to'].replace('|', '/')} |")
        (d / "airs.md").write_text("\n".join(rows) + "\n")

    GM_BEAT_KINDS = ("on_air_call", "pick_statement", "reaction", "comeback", "table_talk", "chirp")  # (grades keep their cutaways)

    def attach_beat_maps(self) -> None:
        """The comic-timing pass for a full-length cut: every GM line gets a beat map (roles by the non-league
        labeller) with EVERY sentence kept -- no words cut -- so the derived audio carries the micro-beats, laugh
        beats, reaction cuts, room reactions and kicker pops."""
        from .beatmap import label_lines
        from .tighten import split_parts
        L = self.L

        def who(tid):
            t = L.teams.get(tid or "")
            return None if not t else ("the robot" if t.is_bot else (t.gm_name or t.display))

        picks = {p.pick_no: p for p in L.picks}
        gm = [ln for ln in self.lines if ln.speaker in L.teams and ln.kind in self.GM_BEAT_KINDS and ln.own_words]
        items = [{"id": ln.id, "text": ln.text, "speaker": who(ln.speaker), "to": who(ln.addressed_to),
                  "player": picks[ln.pick_no].player_name if ln.kind in ("on_air_call", "pick_statement")
                  and ln.pick_no in picks else None, "kind": ln.kind, "beat2": None} for ln in gm]
        bm = label_lines(items, log=lambda m: self.notes.append(m), use_llm=bool(self.sc.get("comic_timing_llm", True)))
        mark_catchphrases(L, bm, gm)
        if self.J is not None:  # a "punch" the panel scores as no joke at all ("Fair shot, Muse.") gets no laugh beat
            demoted = 0
            for ln in gm:
                rec = bm.get(ln.id)
                seq = (ln.refs or [None])[0]
                if not rec or not seq:
                    continue
                rec["labels"] = [dict(x) for x in rec["labels"]]
                for part, lab in zip(rec["parts"], rec["labels"]):
                    if lab.get("role") not in ("PUNCH", "BUTTON"):
                        continue
                    v = self.J.find(seq, part)
                    if v and v.get("judged") and float(v.get("funny") or 0.0) < 1.0:
                        lab.update({"role": "SETUP", "demoted": "judge: not a joke (funny < 1)"})
                        demoted += 1
            if demoted:
                self.notes.append(f"comic timing: {demoted} labelled punch(es) the judge calls no joke get no laugh beat")
        n = 0
        for ln in gm:
            rec = bm.get(ln.id)
            if not rec or len(rec["parts"]) != len(split_parts(ln.text)):
                continue
            ln.beats = {"source": ln.text, "parts": rec["parts"], "labels": rec["labels"],
                        "keep": list(range(len(rec["parts"]))), "label_source": rec.get("source"), "full": True}
            n += 1
        self.notes.append(f"comic timing: beat maps on {n}/{len(gm)} GM lines (every sentence kept)")

    def estimate_total(self) -> float:
        return self._cost(self.lines, len(self.segments)) + 5.0

    def to_dict(self, levels: dict[int, int], target_s: float) -> dict:
        L = self.L
        return {
            "schema": "gmbench.rundown/2",
            "run": L.run,
            "profile": self.cfg.get("profile"),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "ledger_head_seq": L.head_seq,
            "league": L.name,
            "target_duration_s": target_s,
            "estimated_duration_s": round(self.estimate_total(), 1),
            "fit_levels": {str(r): lv for r, lv in sorted(levels.items())},
            "fit_level": max(levels.values()) if levels else None,
            "segments": self.segments,
            "timeline": [asdict(ln) for ln in self.lines],
            "trims": self.trims,
            "dropped": self.dropped,
            "notes": self.notes,
        }


class HighlightBuilder(RundownBuilder):
    """Round highlight cut: hype intro -> curated pick moments (short call, the GM's call, the chirps) -> the best
    table-talk line -> button. GM lines may be trimmed at sentence boundaries (logged); `full` keeps a line whole."""

    def call_high(self, s: str, p: Pick) -> Line:
        t = self.L.teams[p.team]
        spoken = f"[shouting] Pick {number_words(p.pick_no)}... {team_short(t)}... {shout_name(p.player_name)}!"
        display = f"Pick {p.pick_no}... {team_short(t)}... {shout_name(p.player_name)}!"
        return self.host(s, f"p{p.pick_no:03d}-host", spoken, display, kind="host_pick", focus=p.team,
                         pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no}, refs=[p.seq],
                         gap_hint="reveal")

    def chains_for(self, hc: dict) -> list[list[Pick]]:
        """Configured chains (show.yaml script.highlights.chains: [[5, 6, 7, 8], ...]) or the best automatic ones."""
        from .party import best_chains
        by_no = {p.pick_no: p for p in self.L.picks}
        if hc.get("chains"):
            return [[by_no[n] for n in ch if n in by_no] for ch in hc["chains"]]
        rnd = hc.get("round", 1)
        pool = self.L.picks_in_round(int(rnd)) if rnd not in (None, "all") else list(self.L.picks)
        return best_chains(pool, self.L.teams, self.call_words, int(hc.get("n_chains", 2)), int(hc.get("chain_len", 4)))

    def build_chains(self, hc: dict) -> dict:
        """Draft-party highlight cut: the best runs of consecutive picks, each GM playing off the last."""
        rnd = int(hc.get("round", 1) or 1)
        chains = self.chains_for(hc)
        if not chains:
            raise SystemExit("no pick chains with on-air calls in this run")
        s = self.seg("highlights", f"Round {rnd} Highlights", "highlights", f"ROUND {rnd} · HIGHLIGHTS", rnd)
        self.host(s, "hl-intro", f"[shouting] ROUND {number_words(rnd).upper()} HIGHLIGHTS!",
                  f"ROUND {rnd} HIGHLIGHTS!", kind="host_round", round=rnd, gap_hint="beat",
                  card={"type": "highlights", "round": rnd})
        rx = hc.get("reactions", 0)
        for ch in chains:
            for i, p in enumerate(ch):
                lead, lead_d = (f"Pick {number_words(p.pick_no)}! ", f"Pick {p.pick_no}! ") if i == 0 else ("", "")
                self.party_pick(s, p, rx, lead=lead, lead_disp=lead_d)
        if hc.get("button", True):
            allp = self.L.picks_in_round(rnd)
            reveal = [{"pick_no": p.pick_no, "at": round(0.5 * i / max(1, len(allp)), 4)} for i, p in enumerate(allp)]
            self.host(s, "hl-out", f"[shouting] THAT'S ROUND {number_words(rnd).upper()}!", f"THAT'S ROUND {rnd}!",
                      kind="host_close", round=rnd, card={"type": "board", "round": rnd, "reveal": reveal})
        rd = self.to_dict({}, float(self.sc["target_minutes"]) * 60.0)
        rd["chains"] = [[p.pick_no for p in ch] for ch in chains]
        return rd

    def build(self) -> dict:
        hc = self.sc.get("highlights") or {}
        if self.party and not hc.get("moments"):
            return self.build_chains(hc)
        rnd = int(hc.get("round", 1))
        cap = int(hc.get("max_chars", 110))
        picks = {p.pick_no: p for p in self.L.picks_in_round(rnd)}
        best = sorted(picks.values(), key=self.pick_score, reverse=True)[: int(hc.get("n", 5))]
        moments = hc.get("moments") or [{"pick": p.pick_no} for p in sorted(best, key=lambda p: p.pick_no)]
        s = self.seg("highlights", f"Round {rnd} Highlights", "highlights", f"ROUND {rnd} · HIGHLIGHTS", rnd)
        self.host(s, "hl-intro", f"[shouting] ROUND {number_words(rnd).upper()} HIGHLIGHTS!",
                  f"ROUND {rnd} HIGHLIGHTS!", kind="host_round", round=rnd, gap_hint="beat",
                  card={"type": "highlights", "round": rnd})
        for mom in moments:
            p = picks.get(int(mom["pick"]))
            if not p:
                continue
            t = self.L.teams[p.team]
            full = bool(mom.get("full"))
            words0, _ = self.pick_words(p)
            surname = p.player_name.split()[-1].lower() if p.player_name else ""
            names_player = bool(surname) and surname in T.strip(words0).lower()
            # the GM's own call is the reveal when it names the player; the host calls it otherwise
            if mom.get("host_call", not names_player or t.is_bot or bool(p.auto)):
                self.call_high(s, p)
            card = {"type": "pick", "pick_no": p.pick_no}
            if t.is_bot and not mom.get("bot_line"):
                pass
            elif t.is_bot:
                if p.rationale:
                    self.host(s, f"p{p.pick_no:03d}-bot", "[excited] The robot makes its pick!",
                              kind="bot_statement", focus=p.team, pick_no=p.pick_no, round=p.round, card=card, refs=[p.seq])
            elif p.auto:
                self.host(s, f"p{p.pick_no:03d}-auto", f"[surprised] No answer from {t.display}! The league makes "
                          f"that pick!", kind="auto_note", focus=p.team, pick_no=p.pick_no,
                          round=p.round, card=card, refs=[p.seq])
            elif not mom.get("skip_call"):
                words, kind = self.pick_words(p)
                self.gm(s, f"p{p.pick_no:03d}-gm", p.team, words, kind, [p.seq], max_chars=1000 if full else cap,
                        focus=p.team, pick_no=p.pick_no, round=p.round, card={**card, "record": kind == "on_air_call"},
                        gap_hint="punchline")
            wanted = mom.get("reactions", "all")
            for say in self.L.reactions_for(p.pick_no):
                if wanted != "all" and say.seq not in wanted:
                    continue
                self.gm(s, f"say-{say.seq}", say.team, say.line, "reaction", [say.seq],
                        max_chars=1000 if (full or say.seq in (mom.get("full_says") or [])) else cap, focus=say.team,
                        pick_no=p.pick_no, round=p.round, card=card, addressed_to=say.addressed_to or p.team,
                        gap_hint="roast")
        tt = self.L.table_talk(rnd)
        n_tt = int(hc.get("table_talk", 1))
        if tt and n_tt:
            best = sorted(tt, key=lambda x: (T.energy(x.line), -x.seq), reverse=True)[:n_tt]
            q = re.sub(r"^\s*The host asks (?:the panel|you)[:,]\s*", "", tt[0].cue).strip()
            for say in sorted(best, key=lambda x: x.seq):
                self.gm(s, f"say-{say.seq}", say.team, say.line, "table_talk", [say.seq], max_chars=cap, focus=say.team,
                        round=rnd, card={"type": "question", "round": rnd, "question": q}, addressed_to=say.addressed_to,
                        gap_hint="roast")
        if hc.get("button", True):
            allp = self.L.picks_in_round(rnd)
            reveal = [{"pick_no": p.pick_no, "at": round(0.5 * i / max(1, len(allp)), 4)} for i, p in enumerate(allp)]
            self.host(s, "hl-out", f"[shouting] THAT'S ROUND {number_words(rnd).upper()}!", f"THAT'S ROUND {rnd}!",
                      kind="host_close", round=rnd, card={"type": "board", "round": rnd, "reveal": reveal})
        return self.to_dict({}, float(self.sc["target_minutes"]) * 60.0)


class ShortBuilder(HighlightBuilder):
    """Vertical short: cold hook (the best line first) -> context (clock, call, the GM's call, the chirps)."""

    def build_short(self, spec: dict) -> dict:
        if self.party:
            return self.build_tight_short(spec) if spec.get("tight", True) else self.build_chain_short(spec)
        picks = [p for p in self.L.picks if p.pick_no in spec["picks"]]
        picks.sort(key=lambda p: p.pick_no)
        cands = [c for c in self.candidates() if c["pick_no"] in spec["picks"] or
                 (c["kind"] == "reaction" and c["pick_no"] in spec["picks"])]
        hook = None
        if spec.get("hook"):
            want = int(str(spec["hook"]).split("-")[-1])
            hook = next((c for c in cands if c["seq"] == want), None)
        if hook is None and cands:
            hook = self.pick_best(cands, 1)[0]
        s = self.seg("hook", "Hook", "hook", "DRAFT NIGHT")
        if hook:
            text = first_sentences(hook["full"], int(spec.get("hook_chars", 90)))
            self.gm(s, f"hook-{hook['seq']}", hook["team"], text, "hook", [hook["seq"]], max_chars=200,
                    focus=hook["team"], card={"type": "quote", "pick_no": hook["pick_no"]}, addressed_to=hook["to"],
                    gap_hint="beat", pick_no=None)
        s2 = self.seg("moment", "The moment", "moment", f"ROUND {picks[0].round} · PICK {picks[0].pick_no}", picks[0].round)
        for i, p in enumerate(picks):
            if i == 0 and spec.get("clock", True):
                self.clock(s2, p)
            self.call_high(s2, p)  # punchy call: "Pick two... the Foghorns... NATHAN MACKINNON!"
            self.statement(s2, p)
            for say in self.L.reactions_for(p.pick_no):
                self.gm(s2, f"say-{say.seq}", say.team, say.line, "reaction", [say.seq], max_chars=400,
                        focus=say.team, pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no},
                        addressed_to=say.addressed_to or p.team, gap_hint="roast")
        rd = self.to_dict({}, 60.0)
        rd["short"] = {"name": spec.get("name"), "picks": spec["picks"], "hook": hook["seq"] if hook else None}
        return rd


def _chain_short(self: "ShortBuilder", spec: dict) -> dict:
    """Draft-party short: the best chirp as a cold hook, then the chain (cue -> the GM's call, each GM playing off
    the last), then the end card."""
    by_no = {p.pick_no: p for p in self.L.picks}
    if spec.get("picks"):
        chain = [by_no[n] for n in sorted(spec["picks"]) if n in by_no]
    else:
        chain = (self.chains_for({"round": "all", "n_chains": 1, "chain_len": int(spec.get("chain_len", 3))}) or [[]])[0]
    if not chain:
        raise SystemExit("no chain of on-air calls to build a short from")
    nos = {p.pick_no for p in chain}
    cands = [c for c in self.candidates() if c["pick_no"] in nos and c["kind"] in ("on_air_call", "reaction")]
    for c in cands:  # a call's opening line is its reaction beat: aimed at the previous pick's GM
        prev = by_no.get((c["pick_no"] or 0) - 1)
        if c["kind"] == "on_air_call" and prev and c["team"] != prev.team:
            c["to"] = c["to"] or prev.team
    hook = None
    if spec.get("hook"):
        want = int(str(spec["hook"]).split("-")[-1])
        hook = next((c for c in cands if c["seq"] == want), None)
    if hook is None and cands:
        hook = self.pick_best(cands, 1)[0]
    s = self.seg("hook", "Hook", "hook", "DRAFT NIGHT")
    if hook:
        text = first_sentences(hook["full"], int(spec.get("hook_chars", 90)))
        self.gm(s, f"hook-{hook['seq']}", hook["team"], text, "hook", [hook["seq"]], max_chars=200, focus=hook["team"],
                card={"type": "quote", "pick_no": hook["pick_no"]}, addressed_to=hook["to"], gap_hint="beat",
                pick_no=None)
    first = chain[0]
    s2 = self.seg("moment", "The moment", "moment", f"ROUND {first.round} · PICKS {first.pick_no}–{chain[-1].pick_no}",
                  first.round)
    for i, p in enumerate(chain):
        lead, lead_d = (f"Pick {number_words(p.pick_no)}! ", f"Pick {p.pick_no}! ") if i == 0 else ("", "")
        self.party_pick(s2, p, spec.get("reactions", 1), lead=lead, lead_disp=lead_d)
    rd = self.to_dict({}, 60.0)
    rd["short"] = {"name": spec.get("name"), "picks": [p.pick_no for p in chain], "hook": hook["seq"] if hook else None,
                   "kind": "draft"}
    return rd


KEEP_SHORT = {"SETUP", "PUNCH", "PICK", "BUTTON"}
MOTIF_COMMON = {"goalie", "goalies", "minutes", "points", "season", "seasons", "power", "play", "first", "board", "draft",
                "round", "hockey", "player", "players", "defence", "defense", "centre", "center", "winger", "buddy",
                "boys", "right", "night", "unit", "league", "pucks", "puck", "sweet", "beauty", "gimme", "write",
                "april", "since", "still", "grabbed", "taking", "takes", "think", "thing", "never", "every", "years"}


def running_gags(L: League) -> dict[str, set[str]]:
    """{word: {line keys}}: distinctive words shared by lines of at least two GMs (the draft's running gags, e.g.
    'outhouse'). Keys: p<pick_no> for calls, s<seq> for SAYs."""
    from .tighten import content_words
    texts = {f"p{p.pick_no}": (p.team, p.on_air_call) for p in L.picks if p.on_air_call}
    texts.update({f"s{sy.seq}": (sy.team, sy.line) for sy in L.says})
    names = {w.lower() for p in L.picks for w in p.player_name.split()}
    names |= {w.lower().strip("'\"") for t in L.teams.values() for w in (t.display + " " + (t.gm_name or "")).split()}
    by: dict[str, set[tuple[str, str]]] = {}
    for k, (sp, tx) in texts.items():
        for w in content_words(T.strip(tx or "")):
            by.setdefault(w, set()).add((k, sp))
    return {w: {k for k, _ in v} for w, v in by.items() if len(w) >= 5 and w not in MOTIF_COMMON and w not in names
            and len({sp for _, sp in v}) >= 2 and 2 <= len(v) <= 5}


def _tight_short(self: "ShortBuilder", spec: dict) -> dict:
    """The tight draft-party short: no host, whole sentences only (beat map: SETUP / PUNCH / PICK / BUTTON), the
    comebacks right after the call that provoked them, the best punchline as the cold open. Every cut is logged."""
    from .beatmap import label_lines
    from .party import best_chains, split_call
    from .tighten import split_parts
    L = self.L
    by_no = {p.pick_no: p for p in L.picks}
    gags = running_gags(L)

    def bonus(win) -> float:
        nos = {p.pick_no for p in win}
        keys = {f"p{n}" for n in nos} | {f"s{sy.seq}" for n in nos for sy in L.comebacks_for(n)}
        labs = [L.teams[p.team].lab for p in win]
        return (2.0 * sum(1 for n in nos if L.comebacks_for(n))
                + 0.8 * sum(1 for a, b in zip(labs, labs[1:]) if a and b and a != b)
                + 2.5 * sum(1 for ks in gags.values() if len(ks & keys) >= 2))

    if spec.get("picks"):
        chain = [by_no[n] for n in sorted(spec["picks"]) if n in by_no]
    else:
        chain = (best_chains(list(L.picks), L.teams, self.call_words, 1, int(spec.get("chain_len", 3)), bonus=bonus)
                 or [[]])[0]
    if not chain:
        raise SystemExit("no pick chain with on-air calls for a short")
    first = chain[0]
    hook_seg = self.seg("hook", "Hook", "hook", "DRAFT NIGHT")
    s2 = self.seg("moment", "The moment", "moment", f"ROUND {first.round} · PICKS {first.pick_no}–{chain[-1].pick_no}",
                  first.round)
    gm_lines: list[tuple[Line, Pick | None, dict | None]] = []
    only_says = {int(x) for x in spec["says"]} if spec.get("says") is not None else None
    for p in chain:
        t = L.teams[p.team]
        words, kind = self.pick_words(p)
        if t.is_bot or p.auto or not T.strip(words or "") or not getattr(p, "airable", True):
            why = "facts unverified" if not getattr(p, "airable", True) else "bot / auto"
            self.notes.append(f"short: pick {p.pick_no} has no airable GM call ({why}): shown as context only")
            continue
        prev = self.prev_pick(p)
        ln = self.gm(s2, f"p{p.pick_no:03d}-gm", p.team, words, kind, [p.seq], max_chars=1000, focus=p.team,
                     pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no,
                                                              "record": kind == "on_air_call"}, gap_hint="punchline")
        if not ln:
            continue
        info = split_call(ln.display_text, p.player_name, L.teams.get(prev.team) if prev else None)
        ln.card["named"] = info["named"]
        if prev and info["beat2_word"] > 0:
            ln.card.update({"prev_pick": prev.pick_no, "beat2_word": info["beat2_word"], "tone": info["tone"]})
            ln.addressed_to = prev.team
        gm_lines.append((ln, p, info))
        says = list(L.comebacks_for(p.pick_no))
        if only_says is not None:  # a factory candidate: exactly its lines (table talk included)
            says = sorted({sy.seq: sy for sy in says + [sy for sy in L.says if L.pick_for_say(sy) == p.pick_no]
                           if sy.seq in only_says}.values(), key=lambda x: x.seq)
        for sy in says:
            if not getattr(sy, "airable", True):
                self.notes.append(f"short: SAY {sy.seq} facts unverified: never aired")
                continue
            tgt = sy.addressed_to or (L.mentioned_team(T.strip(sy.line), {sy.team}) if sy.kind == "table_talk" else None) \
                or p.team  # table talk: the GM it names by nickname
            cb = self.gm(s2, f"say-{sy.seq}", sy.team, sy.line, "comeback" if sy.kind != "table_talk" else "table_talk",
                         [sy.seq], max_chars=1000, focus=sy.team,
                         pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no},
                         addressed_to=tgt, gap_hint="comeback")
            if cb:
                gm_lines.append((cb, None, None))
    if not gm_lines:
        raise SystemExit("the chain has no GM lines")

    def who(tid: str | None) -> str | None:
        if not tid or tid not in L.teams:
            return None
        t = L.teams[tid]
        return "the robot" if t.is_bot else (t.gm_name or t.display)

    items = []
    for ln, p, info in gm_lines:
        parts = split_parts(ln.text)
        b2 = None
        if p is not None and info and info["beat2_word"] > 0:
            acc = 0
            for k, part in enumerate(parts):
                acc += len(T.strip(part).split())
                if acc > info["beat2_word"]:
                    b2 = k
                    break
        items.append({"id": ln.id, "text": ln.text, "speaker": who(ln.speaker), "to": who(ln.addressed_to),
                      "player": p.player_name if p else None, "kind": ln.kind, "beat2": b2})
    if spec.get("collect") is not None:  # the factory gathers every line first and labels them in one pass
        spec["collect"].extend(items)
        return {"collect": True}
    bm = label_lines(items, log=lambda m: self.notes.append(m), use_llm=spec.get("llm", True))
    mark_catchphrases(L, bm, [ln for ln, _, _ in gm_lines])

    tempo = float(spec.get("tempo") or 1.0)
    cps = 12.6 * tempo
    measured: dict[str, list[float] | None] = {}
    dur_of = spec.get("durations")  # exact sentence lengths from a cached take, when there is one

    def est_line(ln: Line, rec: dict, i: int) -> float:
        if dur_of is not None and ln.id not in measured:
            try:
                measured[ln.id] = dur_of(ln.speaker, rec.get("source_text") or ln.text, rec["parts"])
            except Exception:  # noqa: BLE001
                measured[ln.id] = None
        m = measured.get(ln.id)
        if m and i < len(m) and m[i] > 0:
            return m[i] / tempo + 0.08
        return 0.05 + spoken_len(rec["parts"][i]) / cps

    from .tighten import callback_words, strong_callback
    name_words = {w.lower().strip(".,'\"") for p in L.picks for w in p.player_name.split()}
    name_words |= {w.lower().strip(".,'\"") for t in L.teams.values()
                   for w in (t.display + " " + (t.gm_name or "") + " " + (t.franchise_name or "")).split()}
    name_words |= {n.lower() for t in L.teams.values() for n in [t.p("nickname") or ""] if n}
    try:  # every NHL player's surname is a topic, not a callback ("I passed on Scheifele" / "you dumped Scheifele")
        from .judge import Facts, _norm
        name_words |= {_norm(pp.get("last_name") or "") for pp in Facts(L).players.values()
                       if len(_norm(pp.get("last_name") or "")) >= 4}
    except Exception:  # noqa: BLE001
        pass
    plan: dict[str, list[int]] = {}
    value: dict[tuple[str, int], float] = {}
    J = spec.get("judgement")
    if J is None and spec.get("judge"):
        from .judge import judge_run
        J = judge_run(L, log=lambda m: self.notes.append(m), use_llm=spec.get("llm", True))
    dropped_lines: set[str] = set()
    for ln, p, info in gm_lines:
        rec = bm[ln.id]
        rec["source_text"] = ln.text
        labels = rec["labels"]
        keep = [i for i, lab in enumerate(labels) if lab["role"] in KEEP_SHORT]
        if not keep:
            keep = [max(range(len(labels)), key=lambda i: labels[i]["role"] == "PUNCH")]
        if J is not None:
            keep = judge_gate(self, ln, rec, keep, J)
            jokes = [i for i in keep if labels[i]["role"] in ("PUNCH", "BUTTON")]
            has_pick = any(labels[i]["role"] == "PICK" for i in keep)
            if (p is None and not jokes) or (p is not None and not has_pick
                                            and any(lab["role"] == "PICK" for lab in labels)):
                dropped_lines.add(ln.id)  # nothing left that passes: the whole line is skipped
                self.trims.append({"id": ln.id, "speaker": ln.speaker, "refs": ln.refs, "role": "LINE",
                                   "removed": ln.display_text, "reason": "judge: nothing in this line passes the gate"})
        plan[ln.id] = keep
    if dropped_lines:
        gm_lines = [x for x in gm_lines if x[0].id not in dropped_lines]
        self.lines = [x for x in self.lines if x.id not in dropped_lines]
        if not gm_lines:
            raise SystemExit("short: no line of this chain passes the comedy judge")
    # callbacks: a sentence a LATER kept punchline refers back to stays (as the setup it turned out to be)
    order = [ln for ln, _, _ in gm_lines]
    for a_i, ln in enumerate(order):
        rec = bm[ln.id]
        for i, lab in enumerate(rec["labels"]):
            if lab["role"] in ("PUNCH", "PICK"):
                continue
            jv = lab.get("judge") or {}
            if jv.get("vetoed") or jv.get("factually_ok") == "no":
                continue  # vetoed / factually off: a callback never brings it back
            judge_cut = lab["role"] in ("BUTTON", "SETUP") and bool(jv) and not jv.get("pass", True) \
                and i not in plan[ln.id]
            words = callback_words(T.strip(rec["parts"][i]), name_words)
            for later in order[a_i + 1:]:
                if later.speaker == ln.speaker:
                    continue
                lrec = bm[later.id]
                hits = [j for j in plan[later.id] if lrec["labels"][j]["role"] in ("PUNCH", "BUTTON")
                        and (strong_callback(words, callback_words(T.strip(lrec["parts"][j]), name_words))
                             or run_callback(rec, i, callback_words(T.strip(lrec["parts"][j]), name_words),
                                             name_words))]
                if hits:
                    if lab["role"] in ("FILLER", "STAT") or judge_cut:
                        # the setup of a later GM's (passing) joke: it airs as context, never as a punch
                        rec["labels"][i] = {"i": i, "role": "SETUP", **({"judge": jv} if jv else {})}
                        if judge_cut:
                            stale = T.strip(rec["parts"][i])
                            self.trims = [t_ for t_ in self.trims if not (t_["id"] == ln.id and t_.get("removed") == stale)]
                    labs_ = rec["labels"]
                    j_ = i - 1 - (1 if i > 0 and labs_[i - 1].get("tag") else 0)
                    pick_at = next((q for q, x in enumerate(labs_) if x["role"] == "PICK"), len(labs_))
                    if labs_[i]["role"] == "SETUP" and j_ >= 0 and labs_[j_]["role"] == "PUNCH" and i < pick_at \
                            and len(T.strip(rec["parts"][i]).split()) >= 4 and not judge_cut \
                            and (not jv or jv.get("pass", True)):
                        # it tops the punch before it, and a later GM riffs on it: a laugh line of its own
                        labs_[i] = {"i": i, "role": "PUNCH", "kicker": 3, "spice": 1, "topper": True,
                                    **({"judge": jv} if jv else {})}
                    rec["labels"][i]["callback"] = later.id
                    if i not in plan[ln.id]:
                        plan[ln.id] = sorted(plan[ln.id] + [i])
                    break
    logged = {(t_["id"], t_.get("removed")) for t_ in self.trims}
    for ln in order:
        rec = bm[ln.id]
        for i, lab in enumerate(rec["labels"]):
            if i not in plan[ln.id] and (ln.id, T.strip(rec["parts"][i])) not in logged:
                self.trims.append({"id": ln.id, "speaker": ln.speaker, "refs": ln.refs, "role": lab["role"],
                                   "removed": T.strip(rec["parts"][i]), "reason": f"tight edit: {lab['role']}"})
    # the cold open: the hottest punchline (with its setup when short), cut from the same take
    from .party import name_hit
    best = None
    first_body = gm_lines[0][0].id if gm_lines else None
    want_hook = int(str(spec["hook"]).split("-")[-1]) if spec.get("hook") else None  # the operator's pick
    hook_pool = list(gm_lines)
    if want_hook is not None and not any(want_hook in ln.refs for ln, _, _ in gm_lines):
        xln = outside_hook_line(self, want_hook, J, name_words)  # a line just before the chain opens it
        if xln is not None:
            hook_pool.append((xln, None, None))
            bm[xln.id] = xln._rec
            plan[xln.id] = list(xln._keep)
    for ln, p, info in hook_pool:
        if ln.id == first_body and len(gm_lines) > 1:
            continue  # the cold open never repeats the line that plays right after it
        rec = bm[ln.id]
        for i, lab in enumerate(rec["labels"]):
            if lab["role"] in ("PUNCH", "BUTTON") and not lab.get("tag") and not lab.get("topper") \
                    and i in plan.get(ln.id, [i]) and (lab.get("judge") or {}).get("pass", True) \
                    and (lab["role"] == "PUNCH" or ln.kind in ("comeback", "table_talk")):  # a topper needs its joke
                disp = T.strip(rec["parts"][i])
                n = len(disp.split())
                star = any(name_hit(disp, q.player_name) for q in L.picks)
                sc = (10 * int(lab.get("spice") or 1) + (3 if ln.addressed_to else 0) + (4 if ln.kind != "comeback" else 0)
                      + min(n, 12) / 3 + (2 if star else 0) - (6 if n < 5 else 0) - max(0, n - 12) / 2
                      + (100 if want_hook is not None and want_hook in ln.refs else 0)
                      + 8 * rank_funny(lab.get("judge") or {})
                      - (3 if lab["role"] == "BUTTON" else 0))  # a comeback's closing shot can open the short
                if best is None or sc > best[0]:
                    best = (sc, ln, i)
    lines_all = [ln for ln, _, _ in gm_lines]
    hook = None
    hook_src = None
    if best:
        _, src, pi = best
        rec = bm[src.id]
        keep = [pi]  # the cold open is the punchline alone: its setup plays in the body, where the joke lands in full
        if pi + 1 < len(rec["labels"]) and rec["labels"][pi + 1].get("tag"):
            keep.append(pi + 1)  # ...with its tag ("Bold.")
        hook_src = (src.id, pi)
        if pi > 0 and rec["labels"][pi - 1]["role"] == "SETUP" and src.id in plan and (pi - 1) not in plan[src.id] \
                and not src.id.startswith("src-"):
            plan[src.id] = sorted(plan[src.id] + [pi - 1])
        text = " ".join(rec["parts"][i] for i in keep)
        hook = Line(id=f"hook-{src.refs[0]}", segment=hook_seg, kind="hook", speaker=src.speaker, text=text,
                    display_text=T.strip(text), refs=list(src.refs), focus=src.speaker, pick_no=None,
                    card={"type": "quote", "pick_no": src.pick_no}, addressed_to=src.addressed_to, gap_hint="beat",
                    own_words=True)
        hook.beats = {"source": src.text, "parts": rec["parts"], "labels": rec["labels"], "keep": keep,
                      "label_source": rec.get("source")}
        plan[hook.id] = keep
        self.lines.insert(0, hook)
        lines_all.insert(0, hook)

    def rec_of(ln: Line) -> dict:
        return bm[ln.id] if ln.id in bm else {"parts": ln.beats["parts"], "labels": ln.beats["labels"],
                                               "source_text": ln.beats["source"]}

    def total() -> float:
        t = 0.35 + float(spec.get("end_s", 3.9))
        for ln in lines_all:
            rec = rec_of(ln)
            src = next((x for x in lines_all if x.kind != "hook" and x.refs == ln.refs), ln) if ln.kind == "hook" else ln
            keep = plan[ln.id]
            for k, i in enumerate(keep):
                role = rec["labels"][i]["role"]
                t += est_line(src, rec, i)
                t += 0.45 if role in ("PUNCH", "BUTTON") else 0.0
                t += 0.2 if role == "PUNCH" and k > 0 else 0.0
            t += 0.25
        return t

    target = float(spec.get("target_s", 48.5))
    last_call = next((ln.id for ln in reversed(lines_all) if ln.kind != "comeback"), None)
    prev_name = None
    fb = gm_lines[0][0] if gm_lines else None
    if fb is not None and (fb.card or {}).get("prev_pick") in by_no:
        prev_name = by_no[fb.card["prev_pick"]].player_name.split()[-1].lower()

    def lands_on(labels: list[dict], keep: list[int], k: int) -> str | None:
        """Where a run of setups (and tags) starting at keep[k] lands: the role of the first sentence after it."""
        for kk in range(k + 1, len(keep)):
            lab_ = labels[keep[kk]]
            if lab_["role"] == "SETUP" or lab_.get("tag"):
                continue
            return lab_["role"]
        return None

    def worth(ln: Line, i: int, k: int, keep: list[int]) -> float | None:
        """Value per second of an optional sentence (None = must keep)."""
        rec = bm[ln.id]
        lab = rec["labels"][i]
        role = lab["role"]
        nxt = rec["labels"][keep[k + 1]]["role"] if k + 1 < len(keep) else None
        if role in ("PUNCH", "PICK"):
            return None
        if hook_src and hook_src[0] == ln.id and i == hook_src[1] - 1:
            return None  # the setup of the cold open's punchline: the joke lands in full here
        elif lab.get("callback") and not (ln.id == first_body and prev_name and prev_name in T.strip(rec["parts"][i]).lower()):
            return None  # its punchline is kept: without it the joke is lost (the 'Previously' chip covers pick one)
        elif lab.get("callback"):
            v = 15.0
        elif role == "BUTTON" and len(T.strip(rec["parts"][i]).split()) <= 4:
            v = 65.0
        elif role == "BUTTON":
            v = 60.0 if ln.id == last_call else (55.0 if ln.kind == "comeback" else 45.0)
        elif role == "SETUP" and lands_on(rec["labels"], keep, k) == "PUNCH":
            return None  # the joke's setup: without it the punch is lost
        elif role == "SETUP" and nxt in ("PUNCH", "PICK"):
            v = 40.0
        else:
            v = 15.0
        return v / max(0.3, est_line(ln, rec, i) + (0.45 if role == "BUTTON" else 0.0))

    def candidates() -> list[tuple[float, str, int]]:
        out = []
        for ln in lines_all:
            if ln.kind == "hook":
                continue
            keep = plan[ln.id]
            for k, i in enumerate(keep):
                w = worth(ln, i, k, keep)
                if w is not None and len(keep) > 1:
                    out.append((w, ln.id, i))
        return sorted(out)

    while total() > target:
        cands = candidates()
        if not cands:
            break
        _, lid, i = cands[0]
        plan[lid].remove(i)
        rec = bm[lid]
        ln = next(x for x in lines_all if x.id == lid)
        self.trims.append({"id": lid, "speaker": ln.speaker, "refs": ln.refs, "role": rec["labels"][i]["role"],
                           "removed": T.strip(rec["parts"][i]), "reason": f"budget ({target:.0f} s short)"})
    # still long: drop whole jokes (a punch with its setup run and its tag), the weakest first -- never the cold
    # open's, never a pick, never a joke a later line calls back to, never a call's last joke
    hard_max = float(spec.get("max_s", target + 2.0))
    while total() > hard_max:
        units = []
        for ln in lines_all:
            if ln.kind == "hook":
                continue
            rec = bm[ln.id]
            keep = plan[ln.id]
            labs = rec["labels"]
            jokes = [i for i in keep if labs[i]["role"] == "PUNCH"]
            paid_off = any(bm[x.id]["labels"][q].get("callback") == ln.id for x in lines_all if x is not ln
                           and x.kind != "hook" for q in plan[x.id])
            if paid_off:
                continue  # it pays off an earlier kept setup: cutting it would leave that setup hanging
            for i in jokes:
                if hook_src and hook_src == (ln.id, i):
                    continue
                if ln.kind not in ("comeback", "table_talk") and len(jokes) < 2:
                    continue
                later = [x for x in lines_all[lines_all.index(ln) + 1:] if x.kind != "hook"]
                w = callback_words(T.strip(rec["parts"][i]), name_words)
                if any(strong_callback(w, callback_words(T.strip(bm[x.id]["parts"][j]), name_words))
                       for x in later for j in plan[x.id]):
                    continue
                unit = [i]
                k = i - 1
                while k >= 0 and k in keep and labs[k]["role"] == "SETUP" and not labs[k].get("callback"):
                    unit.append(k)
                    k -= 1
                if i + 1 in keep and labs[i + 1].get("tag"):
                    unit.append(i + 1)
                f = float((labs[i].get("judge") or {}).get("funny") or labs[i].get("spice") or 1)
                units.append((f, -sum(est_line(ln, rec, u) for u in unit), ln.id, sorted(unit)))
        if not units:
            break
        units.sort()
        _, _, lid, unit = units[0]
        rec = bm[lid]
        ln = next(x for x in lines_all if x.id == lid)
        for u in unit:
            plan[lid].remove(u)
            self.trims.append({"id": lid, "speaker": ln.speaker, "refs": ln.refs, "role": rec["labels"][u]["role"],
                               "removed": T.strip(rec["parts"][u]), "reason": f"budget: a whole joke ({hard_max:.0f} s max)"})
        if ln.kind in ("comeback", "table_talk") and not any(rec["labels"][j]["role"] in ("PUNCH", "BUTTON")
                                                           for j in plan[lid]):
            plan[lid] = []
            lines_all = [x for x in lines_all if x.id != lid]  # nothing left of that line: it does not air
            self.lines = [x for x in self.lines if x.id != lid]
    for ln in lines_all:
        if ln.kind == "hook":
            continue
        rec = bm[ln.id]
        keep = plan[ln.id]
        ln.beats = {"source": ln.text, "parts": rec["parts"], "labels": rec["labels"], "keep": keep,
                    "label_source": rec.get("source")}
        if hook and hook_src and hook_src[0] == ln.id:
            hook.beats["labels"] = rec["labels"]
        ln.text = " ".join(rec["parts"][i] for i in keep)
        ln.display_text = T.strip(ln.text)
        ln.est_s = estimate_s(ln.text, self.cps)
    # context for a cold viewer
    prev0 = None
    first_call = next((ln for ln in lines_all if ln.kind != "hook"), None)
    if first_call and (first_call.card or {}).get("prev_pick"):
        pp = by_no.get(first_call.card["prev_pick"])
        if pp and pp.pick_no not in {p.pick_no for p in chain}:
            t = L.teams[pp.team]
            nick = "The robot" if t.is_bot else t.call_name  # model first
            prev0 = {"pick_no": pp.pick_no, "text": f"{nick} just took {pp.player_name}"}
    robot = any(L.teams[x].is_bot for ln in lines_all for x in (ln.speaker, ln.addressed_to) if x in L.teams) or \
        any(" robot" in " " + ln.display_text.lower() for ln in lines_all)
    n_ai = sum(1 for t in L.teams.values() if not t.is_bot)
    rd = self.to_dict({}, 60.0)
    rd["short"] = {"name": spec.get("name"), "picks": [p.pick_no for p in chain], "hook": hook.refs[0] if hook else None,
                   "kind": "draft", "tight": True, "previously": prev0, "robot": robot,
                   "context": (f"{n_ai} AI MODELS VS. 1 ROBOT · THEIR FANTASY HOCKEY DRAFT"
                               if any(t.is_bot for t in L.teams.values()) else
                               f"{n_ai} AI MODELS · 1 FANTASY HOCKEY LEAGUE · THEIR DRAFT PARTY"),
                   "est_s": round(total(), 1), "label_sources": sorted({bm[ln.id].get("source", "?") for ln in lines_all
                                                                        if ln.id in bm}),
                   "judged": J is not None, "judge_panel": J.models if J is not None else None,
                   "lines_used": sorted({r for ln in lines_all for r in ln.refs})}
    return rd


def mark_catchphrases(L, bm: dict, lines: list) -> None:
    """A GM's catchphrase / signature call is filler: never the punch, trimmed from shorts, no laugh beat."""
    from .tighten import catchphrase_parts
    by_no = {p.pick_no: p for p in L.picks}
    for ln in lines:
        rec = bm.get(ln.id)
        if not rec:
            continue
        pk = by_no.get(ln.pick_no) if ln.kind in ("on_air_call", "pick_statement") else None
        for i in catchphrase_parts(L.teams.get(ln.speaker), rec["parts"], pk.player_name if pk else None):
            if rec["labels"][i]["role"] != "PICK":
                rec["labels"][i] = {"i": i, "role": "FILLER", "catchphrase": True}


def outside_hook_line(self, seq: int, J, name_words: set[str]):
    """The line with ledger seq `seq` (a pick call or a SAY) as a cold-open source outside the chain: labelled and
    judged like a body line, never added to the body."""
    from .beatmap import label_lines
    L = self.L
    pk = next((p for p in L.picks if p.seq == seq), None)
    sy = next((x for x in L.says if x.seq == seq), None)
    if pk is not None:
        words, kind = self.pick_words(pk)
        team, pick_no, to = pk.team, pk.pick_no, None
        prev = self.prev_pick(pk)
        if prev is not None and prev.team != pk.team:
            to = prev.team
        player = pk.player_name
    elif sy is not None:
        words, kind, team, pick_no, to, player = sy.line, "comeback", sy.team, sy.pick_no, sy.addressed_to, None
    else:
        return None
    if not T.strip(words or "") or not getattr(pk or sy, "airable", True):
        return None
    text = clean_gm_text(words)
    ln = Line(id=f"src-{seq}", segment="hook", kind=kind, speaker=team, text=text, display_text=T.strip(text),
              refs=[seq], own_words=True, pick_no=pick_no, addressed_to=to)

    def who(tid):
        t = L.teams.get(tid or "")
        return None if not t else ("the robot" if t.is_bot else (t.gm_name or t.display))

    rec = label_lines([{"id": ln.id, "text": ln.text, "speaker": who(team), "to": who(to), "player": player,
                        "kind": kind, "beat2": None}], log=lambda m: self.notes.append(m))[ln.id]
    mark_catchphrases(L, {ln.id: rec}, [ln])
    rec["source_text"] = ln.text
    keep = [i for i, lab in enumerate(rec["labels"]) if lab["role"] in ("PUNCH", "BUTTON")]
    if J is not None:
        keep = judge_gate(self, ln, rec, keep, J)
    ln._rec = rec
    ln._keep = keep
    return ln


def rank_funny(jv: dict) -> float:
    from .judge import rank_funny as _rf
    return _rf(jv) if jv else 0.0


def run_callback(rec: dict, i: int, later_words: set[str], name_words: set[str]) -> bool:
    """A setup told over two short sentences ("Mom's gonna call. It rings till pick twenty.") that a later joke
    picks up ("your mom's letting it ring"): each sentence shares one word, the run shares two."""
    from .tighten import callback_words, strong_callback
    parts = rec["parts"]

    def shared(k: int) -> set[str]:
        return callback_words(T.strip(parts[k]), name_words) & later_words if 0 <= k < len(parts) else set()

    mine = shared(i)
    if not mine:
        return False
    for k in (i - 1, i + 1):
        other = shared(k)
        # only two halves that are weak alone: a neighbour that is a callback on its own props nothing up
        if other and not strong_callback(other, later_words) and strong_callback(mine | other, later_words) \
                and (other - mine):
            return True
    return False


def judge_gate(self, ln, rec: dict, keep: list[int], J) -> list[int]:
    """The comedy judge's gate on one line's kept sentences (shorts): a PUNCH / BUTTON airs only if the panel says it
    makes sense, isn't factually off and scores funny >= 2; nothing vetoed or factually off airs at all. A failed punch
    takes its setup run and its tag with it. Every cut is logged with the judge's reason."""
    labels, parts = rec["labels"], rec["parts"]
    seq = ln.refs[0] if ln.refs else None
    for i, lab in enumerate(labels):
        v = J.find(seq, parts[i]) if seq is not None else None
        if v and v.get("judged"):
            why = "; ".join(f"{m.split('/')[-1]}: {x.get('reason', '')}" for m, x in (v.get("votes") or {}).items())
            lab["judge"] = {"sid": v["sid"], "makes_sense": v["makes_sense"], "funny": v["funny"],
                            "lands_instantly": v.get("lands_instantly", "yes"), "tone": v.get("tone", "friendly"),
                            "consistent": v.get("consistent", "yes"),
                            "factually_ok": v["factually_ok"], "pass": v["pass"], "vetoed": v["vetoed"], "why": why[:300]}
        elif v and v.get("vetoed"):
            lab["judge"] = {"sid": v["sid"], "vetoed": True, "pass": False, "why": "owner veto"}
        elif v:  # in the judge's set but no verdict (an outage): it can't air as a joke unless the owner keeps it
            lab["judge"] = {"sid": v["sid"], "pass": bool(v.get("pass")), "unjudged": True, "funny": 0.0,
                            "makes_sense": "yes", "factually_ok": "unsure", "why": "not judged"}
    drop: dict[int, str] = {}
    for i in keep:
        lab = labels[i]
        jv = lab.get("judge") or {}
        if not jv:
            continue
        if jv.get("vetoed"):
            drop[i] = "owner veto"
        elif jv.get("factually_ok") == "no":
            drop[i] = "judge: facts off"
        elif jv.get("consistent") == "no":
            drop[i] = "judge: gets tonight wrong (board / who said what)"
        elif lab["role"] in ("PUNCH", "BUTTON") and not lab.get("tag") and not jv.get("pass", True):
            later_ok = any(labels[j]["role"] in ("PUNCH", "BUTTON") and (labels[j].get("judge") or {}).get("pass")
                           for j in keep if j > i)
            if jv.get("makes_sense") == "yes" and jv.get("lands_instantly", "yes") == "yes" \
                    and jv.get("consistent", "yes") == "yes" and jv.get("tone") != "mean" and later_ok:
                labels[i] = {"i": i, "role": "SETUP", "demoted": True, "judge": jv}  # context, never a punch beat
                continue
            drop[i] = ("judge: doesn't make sense" if jv.get("makes_sense") == "no"
                       else "judge: gets tonight wrong (board / who said what)" if jv.get("consistent") == "no"
                       else "judge: doesn't land instantly" if jv.get("lands_instantly") == "no"
                       else "judge: mean, not ribbing" if jv.get("tone") == "mean"
                       else f"judge: funny {jv.get('funny')} (< 2)")
    for i in sorted(drop):
        if labels[i]["role"] == "PUNCH":
            k = i - 1
            while k >= 0 and labels[k]["role"] == "SETUP" and k in keep and k not in drop:
                jk = labels[k].get("judge") or {}
                if jk.get("pass"):
                    # the 'setup' is the joke that lands (the labelled punch after it didn't): it is the punch
                    labels[k] = {**labels[k], "role": "PUNCH", "kicker": 3, "spice": 2, "promoted": "judge"}
                    break
                drop[k] = f"setup of a cut punch ({drop[i]})"
                k -= 1
            if i + 1 < len(labels) and labels[i + 1].get("tag") and (i + 1) in keep:
                drop[i + 1] = f"tag of a cut punch ({drop[i]})"
    for i in sorted(drop):
        self.trims.append({"id": ln.id, "speaker": ln.speaker, "refs": ln.refs, "role": labels[i]["role"],
                           "removed": T.strip(parts[i]), "reason": drop[i],
                           "sid": (labels[i].get("judge") or {}).get("sid")})
    return [i for i in keep if i not in drop]


ShortBuilder.build_chain_short = _chain_short
ShortBuilder.build_tight_short = _tight_short


class ReportBuilder(RundownBuilder):
    """The Report Card segment on its own (preview cut of the full-show closer) + a button."""

    def build(self) -> dict:
        if not self.L.report_card():
            raise SystemExit(f"no report card for run {self.L.run}: needs runs/<run>/exports/grades.json (gmbench export) "
                             f"or REPORT_SUBMITTED events in the ledger")
        self.report_card()
        s = self.segments[-1]["id"] if self.segments else self.seg("report_card", "Report Card", "report_card", "REPORT CARD")
        self.host(s, "rc-99", "[shouting] CLASS... DISMISSED!", kind="host_close", card={"type": "report_intro"})
        return self.to_dict({}, float(self.sc["target_minutes"]) * 60.0)


class RoastShortBuilder(RundownBuilder):
    """'The AIs roast each other's drafts': the best roast first (the hook), a one-line host frame, then the
    next best roasts back to back, each followed by a cutaway to the roasted GM."""

    def build_roast(self, spec: dict) -> dict:
        from . import report as R
        rc = self.L.report_card()
        if not rc:
            raise SystemExit(f"no report card for run {self.L.run} (needs exports/grades.json or REPORT_SUBMITTED)")
        voiced = spec.get("voiced")
        J = spec.get("judgement")
        teams = rc["teams"]
        if J is not None:  # the comedy judge's gate: a roast airs whole, so every sentence of it must pass
            teams = [{**t, "grades": [g for g in t["grades"] if J.comment_ok(g["seq"], t["team"])]} for t in teams]
        roasts = R.best_roasts(teams, int(spec.get("n") or 7), voiced)
        if not roasts:
            raise SystemExit("no airable roast lines")
        refs = [r.seq for r in self.L.reports]

        def say(seg: str, g: dict, kind: str) -> None:
            self.gm(seg, f"roast-{g['team']}-{g['from']}", g["from"], g["comment"], kind, [g["seq"]], max_chars=400,
                    focus=g["from"], addressed_to=g["team"], gap_hint="cutaway",
                    card={"type": "roast", "team": g["team"], "grader": g["from"], "grade": g["grade"],
                          "praise": False})

        s = self.seg("hook", "Hook", "hook", "REPORT CARD")
        say(s, roasts[0], "hook")
        s2 = self.seg("roasts", "The roasts", "roasts", "REPORT CARD")
        self.host(s2, "roast-frame", "[shouting] The AI GMs graded each other's drafts... [laughs] and it got UGLY!",
                  kind="host_seg", card={"type": "roast_frame"}, refs=refs)
        for g in roasts[1:]:
            say(s2, g, "grade_comment")
        rd = self.to_dict({}, 60.0)
        rd["short"] = {"name": spec.get("name"), "kind": "roast", "roasts": [[g["from"], g["team"], g["grade"]] for g in roasts]}
        return rd


class DelusionShortBuilder(RundownBuilder):
    """'The Delusion Index' short: where every GM thinks they'll finish vs where the league has them. Hook first (the
    most delusional GM, called out by the Commissioner over the board), the frame, the runner-up, the humble one, and
    the kicker: the harshest grade another GM gave the most delusional team (that GM's own words)."""

    def build_delusion(self, spec: dict) -> dict:
        from . import report as R
        rc = self.L.report_card()
        if not rc:
            raise SystemExit(f"no report card for run {self.L.run} (needs exports/grades.json or REPORT_SUBMITTED)")
        rows = R.delusion_rows(rc["teams"])
        loud = [r for r in rows if r["gap"] >= 2.0]
        if len(rows) < 4 or not loud:
            raise SystemExit("no delusion index (needs self predictions and the league's predictions)")
        refs = [r.seq for r in self.L.reports]
        L = self.L

        def callout(seg: str, r: dict, lid: str, lead: str) -> None:
            t = L.teams[r["team"]]
            who = t.call_name
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(seg, lid, f"{lead}{who} ranked itself... {ordinal_words(r['self']).upper()}! "
                      f"The league average? [laughs] {aw}!",
                      f"{who} ranked itself... {ordinal_words(r['self']).upper()}! The league average? {ad}!",
                      kind="host_delusion", focus=r["team"], card={"type": "delusion", "hl": [r["team"]]}, refs=refs,
                      addressed_to=r["team"], gap_hint="cutaway")  # then a cut to that GM's face

        s = self.seg("hook", "Hook", "hook", "DELUSION INDEX")
        callout(s, loud[0], "dl-hook", "[excited] ")
        s2 = self.seg("delusion", "The Delusion Index", "delusion", "DELUSION INDEX")
        self.host(s2, "dl-frame", "[laughs] The DELUSION INDEX! [excited] Where every GM thinks they'll finish... and "
                  "where the rest of the league has them!", kind="host_delusion", card={"type": "delusion", "hl": []},
                  refs=refs)
        for k, r in enumerate(loud[1:2]):
            callout(s2, r, f"dl-{r['team']}", "[excited] ")
        humble = [r for r in reversed(rows) if r["gap"] <= -2.0][:1]
        for r in humble:
            t = L.teams[r["team"]]
            who = t.call_name
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(s2, f"dl-hum-{r['team']}", f"[surprised] And the humble award... {who} put itself "
                      f"{ordinal_words(r['self'])}! The league says {aw}! [laughs] Somebody give that model a hug!",
                      f"And the humble award... {who} put itself {ordinal_words(r['self'])}! The league says "
                      f"{ad}! Somebody give that model a hug!", kind="host_delusion", focus=r["team"],
                      card={"type": "delusion", "hl": [r["team"]]}, refs=refs)
        top = loud[0]["team"]
        J = spec.get("judgement")
        g = min((g for t in rc["teams"] if t["team"] == top for g in t["grades"] if (g.get("comment") or "").strip()
                 and (J is None or J.comment_ok(g["seq"], top))),
                key=lambda g: R.points(g["grade"]), default=None)
        if g is not None:  # the kicker: what the room actually thinks of the most delusional draft
            self.gm(s2, f"dl-roast-{g['from']}", g["from"], g["comment"], "grade_comment", [g["seq"]], max_chars=300,
                    focus=g["from"], addressed_to=top, gap_hint="cutaway",
                    card={"type": "delusion", "hl": [top], "grader": g["from"], "grade": g["grade"]})
        rd = self.to_dict({}, 60.0)
        rd["short"] = {"name": spec.get("name"), "kind": "delusion",
                       "rows": [[r["team"], r["self"], r["avg"], r["gap"]] for r in rows]}
        return rd


def build_delusion_rundown(league: League, cfg: dict, spec: dict) -> dict:
    return DelusionShortBuilder(league, cfg).build_delusion(spec)


REPORT_CARD_REF = {"min": 5.0, "chars": 4000}  # pd-real's Report Card + Delusion Index (14 teams): for rehearsals


def project_complete(rd: dict, league: League, cfg: dict) -> dict:
    """The complete show's length (the rundown's estimate) and ElevenLabs characters. A rehearsal ledger with fewer
    rounds than the league (party-5: 1 of 14) is extrapolated to the full draft: rounds 1..N at their observed
    density, later rounds with the comebacks capped; a missing Report Card is assumed at pd-real's size."""
    sc = cfg["script"]
    gap = float(sc.get("est_gap_s") or 0.18)
    tl = rd["timeline"]

    def rnd_of(ln: dict) -> int | None:
        m = re.match(r"round_(\d+)", str(ln.get("segment") or ""))
        return int(m.group(1)) if m else None

    def cost(lines: list[dict]) -> tuple[float, int]:
        return (sum(float(x.get("est_s") or 0.0) + float(x.get("pre_hold_s") or 0.0) + gap for x in lines) / 60.0,
                sum(len(x["text"]) for x in lines))

    rounds = sorted({r for r in (rnd_of(x) for x in tl) if r})
    fixed_min, fixed_ch = cost([x for x in tl if rnd_of(x) is None])
    assumed = []
    if not any(x.get("segment") == "report_card" for x in tl):
        fixed_min += REPORT_CARD_REF["min"]
        fixed_ch += REPORT_CARD_REF["chars"]
        assumed.append("Report Card assumed at pd-real's size (no grades yet)")
    per = {r: cost([x for x in tl if rnd_of(x) == r]) for r in rounds}
    n = int(league.rounds or 0)
    out = {"rounds": len(rounds), "of": n, "target_chars": int(sc.get("target_chars") or 0), "assumed": assumed}
    if not rounds or len(rounds) >= n:
        out.update(min=round(fixed_min + sum(v[0] for v in per.values()), 1),
                   chars=int(fixed_ch + sum(v[1] for v in per.values())), projected=bool(assumed))
        return out
    all_n = int(sc.get("comebacks_all_rounds", 3))
    cap = int(sc.get("comebacks_per_round", 3))
    says = [x for x in tl if rnd_of(x) and x["kind"] in ("reaction", "comeback")]
    s_min, s_ch = (cost(says)[0] / len(says), cost(says)[1] / len(says)) if says else (0.0, 0.0)
    full = [per[r] for r in rounds if r <= all_n] or list(per.values())
    f_min, f_ch = sum(x[0] for x in full) / len(full), sum(x[1] for x in full) / len(full)
    n_says = len([x for x in says if (rnd_of(x) or 99) <= all_n]) / max(1, len(full))
    c_min, c_ch = f_min - max(0.0, n_says - cap) * s_min, f_ch - max(0.0, n_says - cap) * s_ch
    tot_min = fixed_min + sum(per[r][0] if r in per else (f_min if r <= all_n else c_min) for r in range(1, n + 1))
    tot_ch = fixed_ch + sum(per[r][1] if r in per else (f_ch if r <= all_n else c_ch) for r in range(1, n + 1))
    out.update(min=round(tot_min, 1), chars=int(tot_ch), projected=True,
               per_round={"full_min": round(f_min, 2), "capped_min": round(c_min, 2), "full_chars": int(f_ch),
                          "capped_chars": int(c_ch), "comebacks_per_round_seen": round(n_says, 1)})
    return out


def build_rundown(league: League, cfg: dict) -> dict:
    mode = cfg["script"].get("mode", "show")
    if mode == "highlights":
        return HighlightBuilder(league, cfg).build()
    if mode == "report":
        return ReportBuilder(league, cfg).build()
    return RundownBuilder(league, cfg).build()


def build_roast_rundown(league: League, cfg: dict, spec: dict) -> dict:
    return RoastShortBuilder(league, cfg).build_roast(spec)


def build_short_rundown(league: League, cfg: dict, spec: dict) -> dict:
    return ShortBuilder(league, cfg).build_short(spec)


def verify_own_words(rundown: dict, league: League) -> list[str]:
    """Guardrail: every own-words line's words must be a verbatim prefix (sentence trim) of a ledger text,
    and its tts text may differ only by allowlisted tags."""
    sources: dict[int, list[str]] = {}

    def add(seq: int | None, text: str) -> None:
        if seq and text:
            sources.setdefault(seq, []).append(T.strip(clean_gm_text(text)))

    for p in league.picks:
        add(p.seq, p.rationale)
        add(p.seq, p.on_air_call)
    for s in league.says:
        add(s.seq, s.line)
    for r in league.reports:  # post-draft report card comments (the grader's own words)
        for g in r.grades:
            add(r.seq, str(g.get("comment") or ""))
    for t in league.teams.values():
        if t.persona_seq:
            parts = [clean_gm_text(t.p("tagline") or ""), clean_gm_text(t.p("catchphrase") or "")]
            add(t.persona_seq, " ".join(x for x in parts if x))
            for x in parts:
                add(t.persona_seq, x)
            add(t.persona_seq, t.p("signature_call") or "")
    problems = []
    for ln in rundown["timeline"]:
        if not ln["own_words"]:
            continue
        cands = [c for r in ln["refs"] for c in sources.get(r, [])]
        ok = any(c.startswith(ln["display_text"]) for c in cands)
        if not ok:  # a sentence-level edit: whole sentences of a source, in order, nothing rewritten
            from .tighten import split_parts
            kept = [T.strip(x) for x in split_parts(ln["text"])]

            def same(k: str, x: str) -> bool:
                if k == x:
                    return True
                y = strip_connective(x)  # (the complete show drops "And"/"But"/"So" after a cut sentence)
                return y is not None and T.strip(y) == k
            for c in cands:
                src = [T.strip(x) for x in split_parts(c)]
                it = iter(src)
                if kept and all(any(same(k, x) for x in it) for k in kept):
                    ok = True
                    break
        if T.strip(ln["text"]) != ln["display_text"] or not ok:
            problems.append(ln["id"])
        if any(t not in T.ALLOWED for t in T.tags_in(ln["text"])):
            problems.append(ln["id"] + ":tags")
    return problems


def save_rundown(rundown: dict, path) -> None:
    path.write_text(json.dumps(rundown, indent=1, ensure_ascii=False))
