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


def team_ref(t: Team, lead: bool = False) -> str:
    """How the host names a franchise: 'the Cold Harbour Auditors' / 'Muse Spark 1.3' / 'Autodraft'."""
    if t.has_persona:
        return ("The " if lead else "the ") + t.franchise_name
    return "Autodraft" if t.is_bot else t.display


def team_short(t: Team, lead: bool = False) -> str:
    if t.has_persona:
        return ("The " if lead else "the ") + t.nickname
    return "Autodraft" if t.is_bot else t.display


def verb(t: Team, plural: str, singular: str) -> str:
    return plural if t.plural_name else singular


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
        bot_bit = f" {number_words(len(bots)).capitalize()} cold-blooded robot!" if bots else ""
        bot_disp = f" {len(bots)} cold-blooded robot!" if bots else ""
        self.host(s, "open-01",
                  f"[shouting] It is DRAFT NIGHT! [excited] {number_words(len(ai)).capitalize()} AI general managers!"
                  f"{bot_bit} {number_words(L.rounds).capitalize()} rounds... {number_words(n_picks)} picks!",
                  f"It is DRAFT NIGHT! {len(ai)} AI general managers!{bot_disp} {L.rounds} rounds... {n_picks} picks!",
                  kind="host_open", card={"type": "title"}, pre_hold_s=float(self.sc.get("title_hold_s", 2.4)),
                  refs=[L.order_seq] if L.order_seq else [])
        if self.sc.get("cold_viewer_intro"):
            labs = []
            for t in ai:
                if t.lab and t.lab not in labs:
                    labs.append(t.lab)
            lab_list = ", ".join(labs[:4]) + (" and more" if len(labs) > 4 else "")
            self.host(s, "open-ctx",
                      f"[excited] Here's the deal: {number_words(len(ai))} AI models... from {lab_list}... each one "
                      f"running a team in a real fantasy hockey league, all season long! Tonight... they draft.",
                      f"Here's the deal: {len(ai)} AI models... from {lab_list}... each one running a team in a real "
                      f"fantasy hockey league, all season long! Tonight... they draft.",
                      kind="host_open", card={"type": "title"})
            if bots:
                self.host(s, "open-robot",
                          "[chuckles] And the robot? That's Autodraft... a dumb control bot. It just takes the best "
                          "player left on the board... and every AI in this room has to beat it!",
                          kind="host_open", focus=bots[0].id, card={"type": "title"})
        self.host(s, "open-02",
                  "[confident] Every word from a GM tonight is the model's own. The voices are synthetic... the trash "
                  "talk is REAL. I'm the Commissioner... [shouting] LET'S DROP THE PUCK!",
                  kind="host_open", card={"type": "title_out"})

    def previously_on(self) -> None:
        mode = self.sc.get("previously_on", "auto")
        if mode is False or mode == "false" or not self.L.history:
            return
        docs = [h for h in (parse_history(n, md) for n, md in self.L.history) if h]
        if not docs:
            return
        s = self.seg("previously", "Previously on GM-Bench", "previously", "PREVIOUSLY")
        self.host(s, "prev-00", "[excited] Previously... on GM-Bench!", kind="host_seg",
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
        s = self.seg("meet_gms", "Meet the GMs", "meet", "MEET THE GMs")
        if self.sc.get("meet_opener", True):
            self.host(s, "meet-00", "[excited] Let's meet the front offices! Here come your general managers!",
                      kind="host_seg")
        order = [t for t in L.order if t in L.teams] + [t for t in L.teams if t not in L.order]
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
                self.host(s, f"meet-{tid}-host", "[chuckles] And the robot. Autodraft. No persona, no speeches... "
                          "it drafts by the house projection.", kind="host_intro", focus=tid, card=card)
                continue
            if not t.has_persona:
                self.host(s, f"meet-{tid}-host", f"[surprised] {t.display}, from {t.lab}... didn't file a Media Day "
                          f"card! The franchise plays under the model's name.", kind="host_intro", focus=tid, card=card)
                continue
            city = t.city or ""
            nick = t.nickname.upper() if t.nickname else t.franchise_name.upper()
            lead = f"Out of {city}... the {nick}!" if city else f"The {t.franchise_name.upper()}!"
            self.host(s, f"meet-{tid}-host", f"[excited] {lead} Behind the bench: {t.gm_name}, running on "
                      f"{t.display} from {t.lab}!", kind="host_intro", focus=tid, card=card, refs=refs)
            words = t.p("signature_call") or ""
            if not words:
                mode = self.sc.get("meet_read", "both")
                parts = []
                if mode in ("tagline", "both") and t.p("tagline"):
                    parts.append(clean_gm_text(t.p("tagline")))
                if mode in ("catchphrase", "both") and t.p("catchphrase"):
                    parts.append(clean_gm_text(t.p("catchphrase")))
                words = " ".join(parts)
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
                self.host(s, f"p{p.pick_no:03d}-bot", "[chuckles] The robot doesn't do speeches. " + p.rationale,
                          kind="bot_statement", focus=p.team, pick_no=p.pick_no, round=p.round, card=card, refs=[p.seq])
            return
        if p.auto:
            reason = str((p.auto or {}).get("reason") or "auto")
            if reason in UNREACHABLE_REASONS:
                spoken = (f"[surprised] No answer from {t.display}! The league makes that pick off the house "
                          f"projection!")
            else:
                spoken = f"[surprised] That pick went in automatically for {team_ref(t)}, off the house projection!"
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
        if self.sc.get("complete"):  # the record of the night: the comebacks fire back too, in ledger order
            says = sorted([x for x in self.L.says if x.pick_no == p.pick_no and x.kind in ("reaction", "comeback")],
                          key=lambda x: x.seq)
        for say in says:
            if limit is not None and n >= limit:
                self.dropped.append({"what": "reaction", "seq": say.seq, "reason": "reactions_per_pick"})
                continue
            if total_cap is not None and sum(1 for x in self.lines if x.kind == "reaction") >= int(total_cap):
                self.dropped.append({"what": "reaction", "seq": say.seq, "reason": "reactions_total_limit"})
                continue
            if say.team not in self.L.teams or not getattr(say, "airable", True):
                continue  # (a SAY with unverified facts never airs)
            ln = self.gm(s, f"say-{say.seq}", say.team, say.line,
                         "comeback" if say.kind == "comeback" else "reaction", [say.seq], focus=say.team,
                         pick_no=p.pick_no, round=p.round, card={"type": "pick", "pick_no": p.pick_no},
                         addressed_to=say.addressed_to or p.team, gap_hint="roast")
            if ln:
                n += 1
                self.aired_says.add(say.seq)
        return n

    def table_talk(self, rnd: int, limit: int | None) -> None:
        says = self.L.table_talk(rnd)
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
            ln = self.gm(s, f"say-{say.seq}", say.team, say.line, "table_talk", [say.seq], focus=say.team, round=rnd,
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

    def cue_who(self, t: Team) -> str:
        if t.is_bot:
            return "Robot"
        if t.has_persona:
            return t.city or t.nickname
        return t.display

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
            return self.host(s, f"p{p.pick_no:03d}-host", f"[chuckles] The robot doesn't do speeches... it takes "
                             f"{name}! [excited] {pos}, {club}.", f"The robot doesn't do speeches... it takes {name}! "
                             f"{pos}, {club}.", kind="host_pick", focus=p.team, pick_no=p.pick_no, round=p.round,
                             card=card, refs=[p.seq], gap_hint="reveal")
        if p.auto:
            self.notes.append(f"pick {p.pick_no}: auto ({(p.auto or {}).get('reason')}); host calls it")
            return self.host(s, f"p{p.pick_no:03d}-host", f"[surprised] No answer from {t.display}! The league takes "
                             f"{name}, off the house projection!", f"No answer from {t.display}! The league takes {name}, "
                             f"off the house projection!", kind="host_pick", focus=p.team, pick_no=p.pick_no,
                             round=p.round, card=card, refs=[p.seq], gap_hint="reveal")
        words, kind = self.pick_words(p)
        if not getattr(p, "airable", True):  # facts unverified: never the GM's words, the Commissioner calls it
            self.notes.append(f"pick {p.pick_no}: fact_check {p.fact_check!r}: the host announces it")
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

    def party_pick(self, s: str, p: Pick, rx_limit: int | None, round_start: bool = False, lead: str = "",
                   lead_disp: str = "", max_chars: int | None = None) -> None:
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
                      f"HERE WE GO!", f"ROUND {rnd}! Crack a cold one, boys... HERE WE GO!", kind="host_round", round=rnd)
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
                  card={"type": "board", "round": rnd, "reveal": []}, gap_hint="beat")
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
        chosen = R.choose_comments([t for t in teams if t["team"] not in blitz_ids], counts)
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
            who = t.short_gm if t.has_persona else t.display
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(s, f"rc-del-{r['team']}", f"[excited] {who} ranked {team_short(t)}... "
                      f"{ordinal_words(r['self']).upper()}! The league average? [laughs] {aw}!",
                      f"{who} ranked {team_short(t)}... {ordinal_words(r['self']).upper()}! The league average? {ad}!",
                      kind="host_delusion", focus=r["team"], card={"type": "delusion", "hl": [r["team"]]}, refs=refs)
        humble = [r for r in reversed(rows) if r["gap"] <= -2.0][: int(rcfg.get("humble") or 0)]
        for r in humble:
            t = L.teams[r["team"]]
            who = t.short_gm if t.has_persona else t.display
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(s, f"rc-hum-{r['team']}", f"[surprised] And the humble award... {who} put {team_short(t)} "
                      f"{ordinal_words(r['self'])}! The league says {aw}! [laughs] Somebody give that GM a hug!",
                      f"And the humble award... {who} put {team_short(t)} {ordinal_words(r['self'])}! The league says "
                      f"{ad}! Somebody give that GM a hug!", kind="host_delusion", focus=r["team"],
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
        return self.to_dict(levels, target_s)

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
                    self.host(s, f"p{p.pick_no:03d}-bot", "[chuckles] The robot doesn't do speeches. " + p.rationale,
                              kind="bot_statement", focus=p.team, pick_no=p.pick_no, round=p.round, card=card, refs=[p.seq])
            elif p.auto:
                self.host(s, f"p{p.pick_no:03d}-auto", f"[surprised] No answer from {t.display}! The league makes "
                          f"that pick off the house projection!", kind="auto_note", focus=p.team, pick_no=p.pick_no,
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
                        and strong_callback(words, callback_words(T.strip(lrec["parts"][j]), name_words))]
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
                        labs_[i] = {"i": i, "role": "PUNCH", "kicker": 3, "spice": 1, "topper": True}
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
    for ln, p, info in gm_lines:
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
                      + 8 * float((lab.get("judge") or {}).get("funny") or 0)
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
        if pi > 0 and rec["labels"][pi - 1]["role"] == "SETUP" and (pi - 1) not in plan[src.id]:
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
            nick = "The robot" if t.is_bot else (t.p("nickname") if hasattr(t, "p") and t.p("nickname") else t.short_gm)
            prev0 = {"pick_no": pp.pick_no, "text": f"{nick} just took {pp.player_name}"}
    robot = any(L.teams[x].is_bot for ln in lines_all for x in (ln.speaker, ln.addressed_to) if x in L.teams) or \
        any(" robot" in " " + ln.display_text.lower() for ln in lines_all)
    n_ai = sum(1 for t in L.teams.values() if not t.is_bot)
    rd = self.to_dict({}, 60.0)
    rd["short"] = {"name": spec.get("name"), "picks": [p.pick_no for p in chain], "hook": hook.refs[0] if hook else None,
                   "kind": "draft", "tight": True, "previously": prev0, "robot": robot,
                   "context": f"{n_ai} AI MODELS · 1 FANTASY HOCKEY LEAGUE · THEIR DRAFT PARTY",
                   "est_s": round(total(), 1), "label_sources": sorted({bm[ln.id].get("source", "?") for ln in lines_all
                                                                        if ln.id in bm}),
                   "judged": J is not None, "judge_panel": J.models if J is not None else None,
                   "lines_used": sorted({r for ln in lines_all for r in ln.refs})}
    return rd


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
        elif lab["role"] in ("PUNCH", "BUTTON") and not lab.get("tag") and not jv.get("pass", True):
            later_ok = any(labels[j]["role"] in ("PUNCH", "BUTTON") and (labels[j].get("judge") or {}).get("pass")
                           for j in keep if j > i)
            if jv.get("makes_sense") == "yes" and later_ok:
                labels[i] = {"i": i, "role": "SETUP", "demoted": True, "judge": jv}  # context, never a punch beat
                continue
            drop[i] = ("judge: doesn't make sense" if jv.get("makes_sense") == "no"
                       else f"judge: funny {jv.get('funny')} (< 2)")
    for i in sorted(drop):
        if labels[i]["role"] == "PUNCH":
            k = i - 1
            while k >= 0 and labels[k]["role"] == "SETUP" and k in keep and k not in drop:
                drop[k] = f"setup of a cut punch ({drop[i]})"
                k -= 1
            if i + 1 < len(labels) and labels[i + 1].get("tag") and (i + 1) in keep:
                drop[i + 1] = f"tag of a cut punch ({drop[i]})"
    for i in sorted(drop):
        self.trims.append({"id": ln.id, "speaker": ln.speaker, "refs": ln.refs, "role": labels[i]["role"],
                           "removed": T.strip(parts[i]), "reason": drop[i]})
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
            who = t.short_gm if t.has_persona else t.display
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(seg, lid, f"{lead}{who} ranked {team_short(t)}... {ordinal_words(r['self']).upper()}! "
                      f"The league average? [laughs] {aw}!",
                      f"{who} ranked {team_short(t)}... {ordinal_words(r['self']).upper()}! The league average? {ad}!",
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
            who = t.short_gm if t.has_persona else t.display
            aw, ad = R.decimal_words(r["avg"], 1), R.decimal_display(r["avg"], 1)
            self.host(s2, f"dl-hum-{r['team']}", f"[surprised] And the humble award... {who} put {team_short(t)} "
                      f"{ordinal_words(r['self'])}! The league says {aw}! [laughs] Somebody give that GM a hug!",
                      f"And the humble award... {who} put {team_short(t)} {ordinal_words(r['self'])}! The league says "
                      f"{ad}! Somebody give that GM a hug!", kind="host_delusion", focus=r["team"],
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
        if not ok and ln.get("beats"):  # the tight edit: whole sentences of a source, in order, nothing rewritten
            from .tighten import split_parts
            kept = [T.strip(x) for x in split_parts(ln["text"])]
            for c in cands:
                src = [T.strip(x) for x in split_parts(c)]
                it = iter(src)
                if kept and all(any(k == x for x in it) for k in kept):
                    ok = True
                    break
        if T.strip(ln["text"]) != ln["display_text"] or not ok:
            problems.append(ln["id"])
        if any(t not in T.ALLOWED for t in T.tags_in(ln["text"])):
            problems.append(ln["id"] + ":tags")
    return problems


def save_rundown(rundown: dict, path) -> None:
    path.write_text(json.dumps(rundown, indent=1, ensure_ascii=False))
