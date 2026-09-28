"""The animation driver: text + timing -> what every character does, frame by frame.

For each on-screen character the plan holds three layers, all on the global show clock:
  mouth    viseme per frame (lipsync.py: from the TTS character alignment, never from loudness)
  eyes     eye/brow state per frame: open | blink | half_lid | happy | angry | surprised | skeptical |
           look_addressee (the renderer turns it into look_left / look_right toward the reaction cam)
  body     gesture clips on the word timeline: point (naming someone), fist_pump (the drafted player's name),
           shrug (a question), laugh (a laugh tag), open_arms ("boys"), talk_hands while talking, idle_breathe
           when silent. Holds 0.6-1.2 s, at most one gesture per ~2 s, clips play their own in/out frames.
Listeners are alive: the GM being talked about reacts on the punchline (surprised, then angry when roasted or happy
when praised); everyone breathes and blinks.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

from . import tags as T
from .captions import word_times
from .lipsync import speaker_tracks, strip_tags

EYES = ("open", "blink", "half_lid", "happy", "angry", "surprised", "skeptical", "look_addressee")
TAG_EYES = {"laughs": "happy", "chuckles": "happy", "sarcastic": "skeptical", "mischievously": "skeptical",
            "angry": "angry", "sighs": "half_lid", "gasps": "surprised", "surprised": "surprised",
            "whispers": "half_lid"}
GESTURE_PRIORITY = {"fist_pump": 5, "point": 4, "laugh": 3, "shrug": 2, "open_arms": 1}
GESTURE_HOLD = {"fist_pump": 1.1, "point": 0.9, "laugh": 1.2, "shrug": 0.8, "open_arms": 1.0}
CROWD_WORDS = {"boys", "fellas", "gentlemen", "folks", "everybody", "everyone", "gents", "lads"}
APEX_LEAD = 0.22  # rig clips reach their key pose after ~2 in-frames at 8-10 fps: start early so the apex is ON the word
MIN_SHOT = 1.2      # the camera CUTS (never moves); no shot is held shorter than this
LISTENER_SHOT = 1.6  # the reaction cut: the surprise, then the verdict


def _rng(key: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def _core(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def tag_times(line: dict) -> list[tuple[str, float, float]]:
    """[(tag, t0, t_scope_end)] in global time: a tag colours the delivery until the end of its sentence."""
    al = line.get("alignment") or {}
    if not al.get("chars"):
        return []
    start, end = float(line["start"]), float(line["end"])
    chars, starts = list(al["chars"]), list(al["starts"])
    s = "".join(chars)
    out = []
    for m in T.TAG_RE.finditer(s):
        tag = m.group(1).strip().lower()
        i = m.start()
        t0 = start + float(starts[i])
        # scope: to the next sentence end after the tag (or the next tag), max 3.5 s
        j = m.end()
        stop = end
        for k in range(j, len(chars)):
            if chars[k] in ".!?" or (chars[k] == "[" and k > j):
                stop = start + float(starts[k])
                break
        out.append((tag, t0, min(stop, t0 + 3.5)))
    return out


class Plan:
    """The whole show's animation plan (global time)."""

    def __init__(self, cues: dict, league, fps: int | None = None, no_close: set[str] | None = None):
        self.cues = cues
        self.no_close = set(no_close or ())  # no production art (a placeholder + model-name card): never a close-up
        self.L = league
        self.fps = int(fps or cues["fps"])
        self.total = int(cues["total_frames"])
        self.lines = cues["lines"]
        self.events = cues.get("events") or []
        self.mouth = speaker_tracks(self.lines, self.fps, self.total)
        self.eyes: dict[str, list[tuple[float, float, str, int]]] = {}  # (t0, t1, state, priority)
        self.body: dict[str, list[tuple[float, float, str]]] = {}
        self.mouth_over: dict[str, list[tuple[float, float, str]]] = {}  # listener reaction mouths (silent frames only)
        self.punches: list[dict] = []
        self.by_id = {ln["id"]: ln for ln in self.lines}
        self.talking: dict[str, list[tuple[float, float]]] = {}
        for ln in self.lines:
            self.talking.setdefault(ln["speaker"], []).append((float(ln["start"]), float(ln["end"])))
        self._pwords: dict[str, list[dict]] = {}
        self._pevent: dict[str, dict] = {}
        self.shot_list: list[dict] = []
        self.gesture_log: list[dict] = []  # every gesture that fired, with the word that triggered it (QA)
        self._speaker_expressions()
        self._listeners()
        self._shots()
        self._gestures()

    # -------------------------------------------------------------- eyes
    def _eye(self, sp: str, t0: float, t1: float, state: str, pri: int) -> None:
        if sp and t1 > t0:
            self.eyes.setdefault(sp, []).append((round(t0, 4), round(t1, 4), state, pri))

    def _speaker_expressions(self) -> None:
        from .party import mention_keys
        reacts = {e["line"]: e for e in self.events if e["type"] == "react"}
        for ln in self.lines:
            sp = ln["speaker"]
            for tag, t0, t1 in tag_times(ln):
                st = TAG_EYES.get(tag)
                if st:
                    self._eye(sp, t0, t1, st, 2)
            r = reacts.get(ln["id"])
            if r:  # the reaction beat: a ribbing smirk or a friendly grin at the buddy on the reaction cam
                self._eye(sp, float(ln["start"]), float(r["until"]), "skeptical" if r.get("tone") != "praise" else "happy", 1)
                tgt = self.L.teams.get(r["team"])
                keys = mention_keys(tgt)
                for w in word_times(ln):
                    if w["start"] >= r["until"]:
                        break
                    if any(k.split()[0] == _core(w["w"]) for k in keys if k):
                        self._eye(sp, w["start"] - 0.05, w["start"] + 0.7, "look_addressee", 3)
                        break

    def punchline(self, e: dict) -> tuple[float, str, list[dict]]:
        """(time, sentence): when the reaction-beat joke lands. The beat's sentences are scored (a simile, the roast /
        praise lexicon, 'you'/'your' aimed at the buddy, ! or ?; a short aside to the room like 'sit down, boys'
        scores down) and the listener reacts just after the best one is SAID (pauses excluded), early enough to be seen
        on the reaction cam before the pick."""
        from .party import PRAISE, ROAST
        ln = self.by_id.get(e.get("line"))
        t0, t1 = float(e["t"]), float(e["until"])
        latest = max(t0 + 0.4, t1 - 0.3)  # the reaction gets its own close-up now: it may land right up to beat 2
        if not ln:
            return max(t0 + 0.4, t1 - 1.6), "", []
        sents, cur = [], []
        for w in word_times(ln):
            if w["start"] >= t1 - 0.05:
                break
            cur.append(w)
            if re.search(r"[.!?]+[\"”')\]]*$", w["w"]):
                sents.append(cur)
                cur = []
        if cur:
            sents.append(cur)
        best, bs = None, -9.0
        for i, s in enumerate(sents):
            ws = [_core(w["w"]) for w in s]
            sc = 2.0 if "like" in ws[1:] else 0.0
            sc += sum(1.0 for x in ws if x in PRAISE or x in ROAST)
            sc += 1.0 if any(x in ("you", "your", "you're", "youre", "yours", "ya") for x in ws) else 0.0
            sc += 0.5 if re.search(r"[!?][\"”')\]]*$", s[-1]["w"]) else 0.0
            if any(x in CROWD_WORDS for x in ws) and len(ws) <= 9:
                sc -= 1.5
            sc += 0.1 * i  # a build-up then the kicker: later wins a tie
            if sc > bs:
                best, bs = s, sc
        if not best:
            return max(t0 + 0.4, t1 - 1.6), "", []
        return min(max(best[-1]["said"] + 0.12, t0 + 0.4), latest), " ".join(w["w"] for w in best), best

    def _mouth(self, sp: str, t0: float, t1: float, vis: str) -> None:
        if t1 > t0:
            self.mouth_over.setdefault(sp, []).append((round(t0, 4), round(t1, 4), vis))

    def _beat_punches(self) -> list[dict]:
        """Beat-mapped lines (the tight edit): every PUNCH / BUTTON mark is a punch. The reaction cut lands 80 ms after
        the kicker's last word; the target is the GM the line is aimed at (a button with no target: the next GM)."""
        from .party import tone as _tone_of
        out = []
        for idx, ln in enumerate(self.lines):
            marks = (ln.get("beats") or {}).get("marks") or []
            if not marks:
                continue
            nxt = self.lines[idx + 1] if idx + 1 < len(self.lines) else None
            words = word_times(ln)
            for mk in marks:
                ke = float(ln["start"]) + float(mk["kicker_end"])
                ps = float(ln["start"]) + float(mk["punch_start"])
                tgt = ln.get("addressed_to")
                nxt_gm = nxt["speaker"] if (nxt and nxt["speaker"] not in (ln["speaker"], "host")
                                            and nxt["speaker"] in self.L.teams) else None
                if mk.get("at_end") and mk.get("role") == "BUTTON" and nxt_gm:
                    tgt = nxt_gm  # a closing tag: the next GM takes it in, then answers
                if not tgt or tgt == ln["speaker"] or tgt not in self.L.teams:
                    tgt = nxt_gm
                sent_words = [w for w in words if ps - 0.05 <= w["start"] <= ke + 0.02]
                sent = " ".join(w["w"] for w in sent_words)
                backhanded = bool(re.search(r"\b(still got|still get|at least|not bad|fair play|give you|gotta give|"
                                            r"credit|props|respect)\b", sent.lower()))
                happy = backhanded or (bool(sent) and _tone_of(sent) == "praise")
                out.append({"line": ln["id"], "listener": tgt, "t": round(ke + 0.08, 4), "kicker_end": round(ke, 4),
                            "punch_start": round(ps, 4), "role": mk.get("role"), "beat": mk.get("beat"),
                            "spice": mk.get("spice"), "sentence": sent, "words": sent_words, "beat_mark": True,
                            "reaction": "laugh" if happy else "mock-outrage", "at_end": bool(mk.get("at_end"))})
        return out

    def _react_listener(self, sp: str, punch: float, until: float, happy: bool) -> None:
        self._eye(sp, punch, punch + 0.45, "surprised", 3)
        self._eye(sp, punch + 0.45, until, "happy" if happy else "angry", 3)
        b = punch + max(1.0, min(2.2, until - 0.7 - punch))
        self.body.setdefault(sp, []).append((round(punch, 4), round(b, 4),
                                             "react_happy|laugh|thumbs_up" if happy else "react_shock|shrug"))
        if happy:  # 'ha-ha-ha' then a grin
            t, k = punch, 0
            while t < min(punch + 1.4, until):
                d = 0.14 if k % 2 == 0 else 0.07
                self._mouth(sp, t, t + d, "laugh" if k % 2 == 0 else "medium_open")
                t, k = t + d, k + 1
            self._mouth(sp, t, until, "smirk")
        else:
            self._mouth(sp, punch, punch + 0.45, "round")
            self._mouth(sp, punch + 0.45, punch + 1.1, "teeth")
            self._mouth(sp, punch + 1.1, until, "smirk")

    def _listeners(self) -> None:
        """The GM on the reaction cam reacts on the punchline: a beat of surprise ('whoa', round mouth), then mock
        outrage (angry brows, gritted teeth, a smirk: it's buddies) when roasted, or a laugh when praised or when the
        jab is a backhanded compliment ('you still got Makar...'); body: react_shock / react_happy."""
        beat = self._beat_punches()
        beat_lines = {bp["line"] for bp in beat}
        for bp in beat:
            self.punches.append({k: v for k, v in bp.items() if k != "words"})
            self._pwords.setdefault(bp["line"], bp["words"])
            if bp["listener"]:
                talk_next = min([a for a, b in self.talking.get(bp["listener"], []) if a > bp["t"] + 0.05] + [bp["t"] + 2.6])
                self._react_listener(bp["listener"], bp["t"], min(bp["t"] + 2.4, max(bp["t"] + 1.2, talk_next - 0.05)),
                                     bp["reaction"] == "laugh")
        evs = [e for e in self.events if e.get("line") not in beat_lines]
        for ln in self.lines:  # the cold open is a roast too: the buddy it names reacts as it lands
            tgt = ln.get("addressed_to")
            if ln["id"] in beat_lines:
                continue
            if ln["kind"] == "hook" and tgt and tgt != ln["speaker"] and tgt in self.L.teams:
                evs.append({"type": "react", "team": tgt, "line": ln["id"], "t": float(ln["start"]),
                            "until": float(ln["end"]) + 1.2, "tone": self._tone(ln), "hook": True})
        for e in evs:
            if e["type"] == "react":
                sp, t0, t1 = e["team"], float(e["t"]), float(e["until"])
                punch, sent, words = self.punchline(e)
                self._pwords[e.get("line")] = words
                self._pevent[e.get("line")] = e
                backhanded = bool(re.search(r"\b(still got|still get|at least|not bad|fair play|give you|gotta give|"
                                            r"credit|props|respect)\b", sent.lower()))
                from .party import tone as _tone_of
                happy = e.get("tone") == "praise" or backhanded or (bool(sent) and _tone_of(sent) == "praise")
                self.punches.append({"line": e.get("line"), "listener": sp, "t": round(punch, 3), "sentence": sent,
                                     "reaction": "laugh" if happy else "mock-outrage"})
                self._eye(sp, punch, punch + 0.45, "surprised", 3)
                self._eye(sp, punch + 0.45, t1 + 1.0, "happy" if happy else "angry", 3)
                b = punch + max(1.0, min(2.2, t1 + 0.3 - punch))
                self.body.setdefault(sp, []).append((round(punch, 4), round(b, 4),
                                                     "react_happy|laugh|thumbs_up" if happy else "react_shock|shrug"))
                if happy:  # 'ha-ha-ha' then a grin
                    t, k = punch, 0
                    while t < min(punch + 1.4, t1 + 1.0):
                        d = 0.14 if k % 2 == 0 else 0.07
                        self._mouth(sp, t, t + d, "laugh" if k % 2 == 0 else "medium_open")
                        t, k = t + d, k + 1
                    self._mouth(sp, t, t1 + 1.0, "smirk")
                else:
                    self._mouth(sp, punch, punch + 0.45, "round")
                    self._mouth(sp, punch + 0.45, punch + 1.1, "teeth")
                    self._mouth(sp, punch + 1.1, t1 + 1.0, "smirk")
            elif e["type"] == "cutaway":  # report-card roasts: the roasted GM's face
                self._eye(e["team"], float(e["t"]), float(e["until"]), "happy" if e.get("praise") else "surprised", 3)

    def mouth_track(self, sp: str) -> list[list]:
        """RLE mouth: lip sync from the text, plus the listener reaction mouths where the character is silent."""
        base = self.mouth.get(sp, [[0, "rest"]])
        over = self.mouth_over.get(sp)
        if not over:
            return base
        arr = ["rest"] * self.total
        for k, (f, v) in enumerate(base):
            f1 = base[k + 1][0] if k + 1 < len(base) else self.total
            arr[int(f):int(f1)] = [v] * max(0, min(self.total, int(f1)) - int(f))
        for t0, t1, v in over:
            for f in range(max(0, int(round(t0 * self.fps))), min(self.total, int(round(t1 * self.fps)))):
                if arr[f] == "rest":
                    arr[f] = v
        rle, prev = [], None
        for f, v in enumerate(arr):
            if v != prev:
                rle.append([f, v])
                prev = v
        return rle

    def eye_track(self, sp: str) -> list[list]:
        """RLE [[frame, state], ...]: priority-merged states + seeded natural blinks (4 frames, every 2.6-6.2 s)."""
        arr = ["open"] * self.total
        pri = [0] * self.total
        for t0, t1, st, p in sorted(self.eyes.get(sp, []), key=lambda x: x[3]):
            for f in range(max(0, int(t0 * self.fps)), min(self.total, int(t1 * self.fps))):
                if p >= pri[f]:
                    arr[f], pri[f] = st, p
        rng = _rng(f"blink:{sp}")
        # never blink on a gesture's apex (the point on 'Millet' with the eyes shut) or as a close-up cuts in
        quiet = [(a + APEX_LEAD - 0.25, a + APEX_LEAD + 0.35) for a, b, g in self.body.get(sp, [])
                 if g.split("|")[0] in ("point", "fist_pump", "open_arms", "shrug", "thumbs_up")]
        quiet += [(x["t0"] - 0.1, x["t0"] + 0.4) for x in self.shot_list if x["sp"] == sp]
        t = float(rng.uniform(0.6, 3.0))
        while t * self.fps < self.total - 4:
            if any(a <= t <= b for a, b in quiet):
                t += 0.3
                continue
            f = int(t * self.fps)
            if arr[f] not in ("happy", "surprised") and pri[f] < 3:
                for k, st in zip(range(f, min(self.total, f + 4)), ("half_lid", "blink", "blink", "half_lid")):
                    arr[k] = st
            t += float(rng.uniform(2.6, 6.2))
        rle, prev = [], None
        for f, v in enumerate(arr):
            if v != prev:
                rle.append([f, v])
                prev = v
        return rle

    def _tone(self, ln: dict) -> str:
        from .party import tone
        try:
            return tone(ln.get("tts_text") or ln.get("display_text") or "")
        except Exception:  # noqa: BLE001
            return "roast"

    # -------------------------------------------------------------- camera: cuts, never moves
    def _name_end(self, ln: dict, rv: dict) -> float:
        """When the voice finishes the drafted player's name (the reveal event starts at its first name)."""
        R = float(rv["t"])
        pk = next((p for p in self.L.picks if p.pick_no == rv.get("pick_no")), None)
        parts = [_core(x) for x in (pk.player_name.split() if pk else [])]
        end = R + 0.6
        for w in word_times(ln):
            if w["start"] >= R - 0.05 and _core(w["w"]) in parts:
                end = max(end, w["said"])
            elif w["start"] > R + 1.6:
                break
        return end

    def _shots(self) -> None:
        """The big frame's camera plan. The medium (waist-up) shot is the default and carries the gestures; CUT to
        a close-up (the head at twice the medium zoom) for the punchline's kicker and for the name call, and to the
        listener's close-up for the reaction. Shots never go under MIN_SHOT: short gaps are closed by cutting
        straight from one close-up to the next. Result: self.shot_list = [{t0, t1, sp, kind: cu|listener, line}]."""
        reveals = {e["line"]: e for e in self.events if e["type"] == "reveal"}
        punch = {p["line"]: p for p in self.punches if not p.get("beat_mark")}
        beat_by: dict[str, list[dict]] = {}
        for p in self.punches:
            if p.get("beat_mark"):
                beat_by.setdefault(p["line"], []).append(p)
        def switch(i: int) -> float:  # when the layout cuts to line i (the show model's rule, incl. overlapping lines)
            ln_ = self.lines[i]
            if ln_.get("sw") is not None:
                return float(ln_["sw"])
            return 0.0 if i == 0 else max(float(self.lines[i - 1]["end"]) + 0.02, float(ln_["start"]) - 0.25)
        # the last line's shot runs on under the end card's fade (no medium flash before it is opaque)
        sw = [switch(i + 1) if i + 1 < len(self.lines) else float(self.lines[-1]["end"]) + 1.5
              for i in range(len(self.lines))]
        segs: list[list] = []
        for i, ln in enumerate(self.lines):
            sp, ls, le = ln["speaker"], float(ln["start"]), float(ln["end"])
            nxt_sw = sw[i]
            nxt_host = i + 1 < len(self.lines) and self.lines[i + 1]["speaker"] == "host"
            line_segs: list[list] = []
            p = punch.get(ln["id"])
            if sp in self.no_close:
                p = None
            if p is not None:
                P = float(p["t"])
                words = self._pwords.get(ln["id"]) or []
                s0 = words[0]["start"] if words else P - 2.0
                if P - s0 > 3.2:  # a long sentence: only its kicker (from a simile's 'like' when there is one)
                    like = [w for w in words if _core(w["w"]) == "like" and P - 3.4 <= w["start"] <= P - MIN_SHOT]
                    later = [w for w in words if w["start"] >= P - 2.4]
                    s0 = like[0]["start"] if like else (later[0]["start"] if later else P - 2.4)
                if ln["kind"] == "hook":
                    s0 = ls - 0.1  # the cold open: in their face from frame one
                line_segs.append([min(s0 - 0.08, P - MIN_SHOT), P, sp, "cu"])
                ev = self._pevent.get(ln["id"]) or {}
                l1 = P + max(MIN_SHOT, min(LISTENER_SHOT, float(ev.get("until", P)) + 0.3 - P))
                cap = nxt_sw + (MIN_SHOT + 0.2 if nxt_host else 0.0)  # may J-cut into a host cue, never into a GM
                if cap - P >= MIN_SHOT and p["listener"] not in self.no_close:
                    line_segs.append([P, min(l1, cap), p["listener"], "listener"])
            for bp in (beat_by.get(ln["id"]) or []):  # (a speaker without close-up art still gets the reaction cut)
                P = float(bp["t"])
                if bp["role"] == "PUNCH" and sp not in self.no_close:  # in their face from the punch's first word
                    s0 = min(float(bp["punch_start"]) - 0.05, P - MIN_SHOT)
                    if ln["kind"] == "hook":
                        s0 = min(s0, ls - 0.1)
                    line_segs.append([s0, P, sp, "cu"])
                lis = bp.get("listener")
                if lis and lis not in self.no_close:
                    nxt_ln = self.lines[i + 1] if i + 1 < len(self.lines) else None
                    comeback = bool(nxt_ln and nxt_ln["speaker"] == lis and nxt_ln["kind"] == "comeback"
                                    and bp.get("at_end"))
                    l1 = P + max(MIN_SHOT, min(LISTENER_SHOT, float(bp.get("beat") or 0.4) + 1.0))
                    nxt_is_lis = bool(nxt_ln and nxt_ln["speaker"] == lis)
                    # the reaction may run on under the next GM's first words (a J-cut), briefly
                    cap = nxt_sw + (MIN_SHOT + 0.2 if (nxt_host or comeback or nxt_is_lis) else 1.3)
                    if comeback:  # the buddy fires back from the reaction shot: it runs on into the comeback
                        l1 = max(l1, float(nxt_ln["start"]) + 0.6)
                    if nxt_ln is None and bp.get("at_end"):  # the final beat: the face holds under the end card
                        l1 = cap = P + LISTENER_SHOT + 0.45
                    if nxt_is_lis and not comeback and bp.get("at_end"):  # takes it in, then talks: own close-up
                        line_segs.append([P, max(P + MIN_SHOT, l1), lis, "cu"])
                    elif cap - P >= MIN_SHOT or comeback:
                        line_segs.append([P, min(l1, cap) if not comeback else l1, lis, "listener"])
            rv = reveals.get(ln["id"])
            if rv is not None and sp not in self.no_close and (sp == "host" or ln["kind"] in ("on_air_call", "pick_statement")):
                R = float(rv["t"])
                a = R - 0.25
                line_segs.append([a, max(a + MIN_SHOT, self._name_end(ln, rv) + 0.35), sp, "cu"])
            line_segs.sort(key=lambda x: x[0])
            # overlaps: the later shot starts when the earlier one ends (a listener cut is never shortened below MIN,
            # and never leaves mid-word: the reaction may take the start of the name call, the cut back waits for
            # the word to finish)
            ln_words = word_times(ln)
            for k in range(1, len(line_segs)):
                prev, cur = line_segs[k - 1], line_segs[k]
                if cur[0] < prev[1]:
                    if prev[3] == "listener":
                        cur_end = min(cur[1], nxt_sw) if cur[2] == sp else cur[1]
                        e = max(prev[0] + MIN_SHOT, cur[0])
                        w_in = next((w for w in ln_words if w["start"] < e < w["said"]), None)
                        e2 = (w_in["said"] + 0.04) if w_in is not None else e
                        if cur_end - e < MIN_SHOT:  # no room for both: the reaction holds, the name is heard over it
                            prev[1] = max(prev[1], cur_end)
                            cur[0] = cur[1] = prev[1]
                            continue
                        prev[1] = e2 if cur_end - e2 >= MIN_SHOT else e
                    else:
                        prev[1] = max(prev[0] + 0.01, cur[0])
                    cur[0] = max(cur[0], prev[1])
                    cur[1] = max(cur[1], cur[0] + MIN_SHOT)
            line_segs = [x for x in line_segs if x[1] - x[0] > 1e-3]
            # medium gaps shorter than MIN_SHOT: cut straight from shot to shot (same subject: one shot)
            k = 1
            while k < len(line_segs):
                prev, cur = line_segs[k - 1], line_segs[k]
                if cur[0] - prev[1] < MIN_SHOT:
                    if prev[2] == cur[2] and prev[3] == cur[3]:
                        prev[1] = max(prev[1], cur[1])
                        line_segs.pop(k)
                        continue
                    prev[1] = cur[0]
                k += 1
            # line edges: no sliver of medium before the first close-up or after the last one
            start_sw = switch(i)
            if line_segs and line_segs[0][2] == sp and line_segs[0][0] - start_sw < MIN_SHOT:
                line_segs[0][0] = start_sw
            if line_segs and line_segs[-1][3] == "cu" and line_segs[-1][2] == sp:
                last = line_segs[-1]
                last[1] = min(last[1], nxt_sw)
                if nxt_sw - last[1] < MIN_SHOT or i == len(self.lines) - 1:  # (the last runs on under the end card)
                    last[1] = nxt_sw
            for x in line_segs:
                if x[1] - x[0] >= 0.5:
                    segs.append(x + [ln["id"]])
        segs.sort(key=lambda x: x[0])
        # across lines: a gap under MIN_SHOT between two shots would flash a medium sliver -- the shot of whoever holds
        # the frame in that gap covers it (the next speaker's close-up starts early, or the last one runs on)
        def holder(t: float) -> str:
            j = max((k for k in range(len(self.lines)) if switch(k) <= t + 1e-6), default=0)
            return self.lines[j]["speaker"]
        for a_, b_ in zip(segs, segs[1:]):
            gap = b_[0] - a_[1]
            if 1e-3 < gap < MIN_SHOT:
                f = holder(a_[1] + gap / 2)
                if f == b_[2]:
                    b_[0] = a_[1]
                elif f == a_[2]:
                    a_[1] = b_[0]
        # a reaction shot that runs on under the next speaker's close-up leaves that close-up at least MIN_SHOT on
        # screen: the close-up runs on (or the reaction gives way), never a flash of it
        for k in range(len(segs) - 1):
            a_, b_ = segs[k], segs[k + 1]
            if a_[3] == "listener" and b_[2] != a_[2] and b_[0] < a_[1] < b_[1] and b_[1] - a_[1] < MIN_SHOT:
                nxt0 = segs[k + 2][0] if k + 2 < len(segs) else 1e9
                b_[1] = max(b_[1], min(a_[1] + MIN_SHOT, nxt0))
                if b_[1] - a_[1] < MIN_SHOT and (b_[1] - MIN_SHOT) - a_[0] >= MIN_SHOT:
                    a_[1] = b_[1] - MIN_SHOT
        # a speaker's close-up squeezed under MIN_SHOT (a fast line: the name call right before the next line) is
        # dropped: the shot before it runs on over it (a reaction holds through the name, into the comeback)
        k = 0
        while k < len(segs):
            x = segs[k]
            if x[3] == "cu" and x[1] - x[0] < MIN_SHOT - 1e-3:
                prv = segs[k - 1] if k > 0 else None
                nxt = segs[k + 1] if k + 1 < len(segs) else None
                if prv is not None and abs(prv[1] - x[0]) < 0.06:
                    prv[1] = max(prv[1], x[1])
                    segs.pop(k)
                    continue
                if nxt is not None and abs(nxt[0] - x[1]) < 0.06 and nxt[2] == x[2]:
                    nxt[0] = x[0]
                    segs.pop(k)
                    continue
                segs.pop(k)  # nothing to lean on: the line plays in the medium shot, no flash of a close-up
                continue
            k += 1
        self.shot_list = [{"t0": round(a, 3), "t1": round(b, 3), "sp": who, "kind": k, "line": lid}
                          for a, b, who, k, lid in segs]

    def cu_windows(self, sp: str) -> list[tuple[float, float]]:
        """The close-up instance's on-air windows for `sp`: the UNION of their shots (a reaction shot running into
        their own line's close-up is one window; overlapping windows toggled the instance off mid-shot)."""
        out: list[list[float]] = []
        for a, b in sorted((s["t0"], s["t1"]) for s in self.shot_list if s["sp"] == sp):
            if out and a <= out[-1][1] + 1e-3:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return [(a, b) for a, b in out]

    # -------------------------------------------------------------- body
    def _gestures(self) -> None:
        from .party import mention_keys
        picks = {p.pick_no: p for p in self.L.picks}
        reacts = {e["line"]: e for e in self.events if e["type"] == "react"}
        reveals = {e["line"]: e for e in self.events if e["type"] == "reveal"}
        all_keys = {tid: mention_keys(t) for tid, t in self.L.teams.items()}
        for ln in self.lines:
            sp = ln["speaker"]
            ws = word_times(ln)
            cands: list[tuple[float, str, str, float]] = []  # (start, gesture, trigger word, trigger time)
            r = reacts.get(ln["id"])
            addr = r["team"] if r else ln.get("addressed_to")
            if not addr and ln.get("kind") == "host_clock":  # "Millet, let's hear it!": the host points at the GM up
                pk = picks.get((ln.get("card") or {}).get("pick_no"))
                addr = getattr(pk, "team", None) if pk else None
            ls = float(ln["start"]) - 0.15  # a gesture may start just before the voice does
            for w in ws:
                c = _core(w["w"])
                if not c:
                    continue
                if addr and any(k.split()[0] == c for k in all_keys.get(addr, []) if k):
                    cands.append((max(ls, w["start"] - APEX_LEAD), "point", w["w"], w["start"]))
                if c in CROWD_WORDS:
                    cands.append((max(ls, w["start"] - APEX_LEAD), "open_arms", w["w"], w["start"]))
                if w["w"].rstrip("\"”')").endswith("?"):
                    cands.append((max(ls, w["start"] - APEX_LEAD), "shrug", w["w"], w["start"]))
            rv = reveals.get(ln["id"])
            if rv and (sp == "host" or ln["kind"] in ("on_air_call", "pick_statement")):
                pk_ = picks.get(rv.get("pick_no"))
                cands.append((max(ls, float(rv["t"]) - APEX_LEAD), "fist_pump",
                              pk_.player_name if pk_ else "(the pick)", float(rv["t"])))
            for tag, t0, _ in tag_times(ln):
                if tag in ("laughs", "chuckles"):
                    cands.append((t0, "laugh", f"[{tag}]", t0))
            # one gesture per ~2 s; higher priority wins a clash
            cands.sort(key=lambda x: (x[0], -GESTURE_PRIORITY[x[1]]))
            kept: list[tuple[float, str, str, float]] = []
            for c_ in cands:
                t, g = c_[0], c_[1]
                if kept and t - kept[-1][0] < 2.0:
                    if GESTURE_PRIORITY[g] > GESTURE_PRIORITY[kept[-1][1]] and t - kept[-1][0] < 0.8:
                        kept[-1] = c_
                    continue
                kept.append(c_)
            cu = [(x["t0"], x["t1"]) for x in self.shot_list if x["sp"] == sp and x["line"] == ln["id"]]
            moved: list[tuple[float, str]] = []
            for t, g, word, wt in kept:
                inside = next(((a, b) for a, b in cu if a - 0.05 <= t + APEX_LEAD < b), None)
                if inside is None:
                    moved.append((t, g))
                    self.gesture_log.append({"sp": sp, "line": ln["id"], "g": g, "t": round(t, 4), "word": word,
                                             "word_t": round(wt, 4), "after_cu": None})
                elif g == "fist_pump" and inside[1] < float(ln["end"]) + 0.2:
                    moved.append((inside[1] - 0.02, g))  # the cut back to medium lands on the celebration
                    self.gesture_log.append({"sp": sp, "line": ln["id"], "g": g, "t": round(inside[1] - 0.02, 4),
                                             "word": word, "word_t": round(wt, 4), "after_cu": round(inside[1], 4)})
            kept = moved
            for t, g in kept:
                t1 = min(float(ln["end"]) + 0.4, t + GESTURE_HOLD[g])
                self.body.setdefault(sp, []).append((round(t, 4), round(max(t1, t + 0.6), 4), g))

    def _sips(self, sp: str) -> list[tuple[float, float, str]]:
        """Beer sips in long silent stretches (>= 8 s): every 12-20 s, 2.2 s each (seeded, never while talking)."""
        rng = _rng(f"sip:{sp}")
        talk = sorted(self.talking.get(sp, []))
        busy = sorted([(a - 1.0, b + 1.0) for a, b in talk] + [(a - 0.5, b + 0.5) for a, b, _ in self.body.get(sp, [])])
        out = []
        t = float(rng.uniform(4.0, 12.0))
        T_end = self.total / self.fps - 3.0
        while t < T_end:
            if not any(a < t + 2.2 and b > t for a, b in busy):
                out.append((round(t, 4), round(t + 2.2, 4), "drink_sip"))
                t += float(rng.uniform(12.0, 20.0))
            else:
                t += 1.5
        return out

    def body_track(self, sp: str) -> list[tuple[float, float, str]]:
        """Gesture segments with talk_hands filling the talking time and idle_breathe everywhere else."""
        gest = sorted(self.body.get(sp, []) + self._sips(sp))
        talk = sorted(self.talking.get(sp, []))
        out: list[tuple[float, float, str]] = []
        T_end = self.total / self.fps
        cursor = 0.0
        events = sorted([(a, b, g) for a, b, g in gest])
        k = 0
        while cursor < T_end:
            nxt = events[k] if k < len(events) else None
            if nxt and nxt[0] <= cursor + 1e-6:
                a, b, g = nxt
                out.append((max(cursor, a), b, g))
                cursor = max(cursor, b)
                k += 1
                continue
            stop = nxt[0] if nxt else T_end
            # between gestures: talk_hands inside talking spans, idle_breathe elsewhere
            t = cursor
            for a, b in talk:
                if b <= t or a >= stop:
                    continue
                if a > t:
                    out.append((t, a, "idle_breathe"))
                    t = a
                seg_end = min(b, stop)
                if seg_end - t > 0.5:
                    out.append((t, seg_end, "talk_hands"))
                else:
                    out.append((t, seg_end, "idle_breathe"))
                t = seg_end
            if t < stop:
                out.append((t, stop, "idle_breathe"))
            cursor = stop
        merged: list[list] = []
        for a, b, g in out:
            if b - a <= 1e-4:
                continue
            if merged and merged[-1][2] == g and abs(merged[-1][1] - a) < 1e-3:
                merged[-1][1] = b
            else:
                merged.append([round(a, 4), round(b, 4), g])
        return [tuple(x) for x in merged]

    def speakers(self) -> list[str]:
        return sorted(set(self.mouth) | set(self.eyes) | set(self.body) | set(self.talking))

    def export(self) -> dict:
        sps = sorted(set(self.speakers()) | set(self.L.teams) | {"host"})
        return {"schema": "gmbench.anim/1", "fps": self.fps, "total_frames": self.total,
                "punchlines": self.punches, "shots": self.shot_list,
                "speakers": {sp: {"mouth": self.mouth_track(sp), "eyes": self.eye_track(sp),
                                  "body": [list(x) for x in self.body_track(sp)]} for sp in sps}}
