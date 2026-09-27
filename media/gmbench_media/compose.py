"""Timeline -> HyperFrames HTML.

The whole show is modelled as global state timelines (stage focus, stage card, captions, speaking
highlight, on-the-clock tag, ticker, mouth states). The show is cut into render chunks (segment-
aligned, <= render.chunk_max_s); each chunk is one composition whose initial DOM state equals the
global state at its start time and whose paused GSAP timeline replays every change inside its
window. Everything is data-driven `tl.set` / `fromTo` calls: deterministic and seekable, no timers.

Written per build:
  show/render/<cid>.html        standalone compositions (rendered one by one, then concatenated)
  show/compositions/<cid>.html  the same chunks as <template> sub-compositions
  show/index.html               Studio preview of the whole show (chunks as sub-compositions + audio)
  show/assets/gen/...           avatar frames resized for the stage/tiles, preview audio
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import studio_css
from .rigview import RIG_CSS, RIG_JS
from .avatars import POSES, STATES, FrameSet
from .captions import fit_size, meme_pages, text_width
from .motion import MotionPlan
from .ledger import League
from .nhl import club_spoken, ordinal_suffix, pos_display
from .paths import SHOW, SHOW_GEN, rel
from .script import parse_history

RED, OFF = "#E32402", "#EFF0F5"
PANEL = "#132258"  # tile/panel colour as composited over the studio background
# board beats: the full grid comes back (draft order, round boards, history, the table-talk question, report-card
# boards). Everything else is a speaker beat: the current speaker BIG, the grid collapsed to a slim bench strip.
BOARD_CARDS = {"order", "board", "history", "question_full", "report_intro", "report_board", "delusion"}
FULL_STAGE = BOARD_CARDS
CONFETTI = ["#E32402", "#EFF0F5", "#4770DB", "#FFC83D"]
TILE_W, TILE_H, TILE_GAP_X, TILE_GAP_Y = 370, 86, 12, 8
TICK_W, TICK_VIEW = 384, 1684
esc = html.escape


def slug(key: tuple) -> str:
    return "cd-" + "-".join(str(k) for k in key if k is not None).replace(".", "_").replace(" ", "_")


@dataclass
class Chunk:
    cid: str
    t0: float  # exact frame multiples (f / fps): chunks concatenate frame-exactly
    t1: float
    segments: list[str] = field(default_factory=list)
    fps: int = 30

    @property
    def frames(self) -> int:
        return int(round(self.t1 * self.fps)) - int(round(self.t0 * self.fps))

    @property
    def dur(self) -> float:
        return round(self.frames / self.fps, 6)

    @property
    def dur_attr(self) -> str:
        """data-duration for HyperFrames, which renders ceil(duration x fps) frames: a hair under the exact count, so
        a chunk never gains a frame (one extra frame shifted every later chunk 33 ms behind the audio)."""
        return f"{(self.frames - 0.01) / self.fps:.6f}"


class ShowModel:
    def __init__(self, league: League, rundown: dict, cues: dict, tracks: dict, framesets: dict[str, FrameSet],
                 cfg: dict, motion: MotionPlan | None = None, env: dict | None = None,
                 layout: str = "studio"):
        self.L, self.rundown, self.cues, self.tracks, self.fs, self.cfg = league, rundown, cues, tracks, framesets, cfg
        self.layout = layout
        self.fps = int(cues["fps"])
        self.T = float(cues["duration_s"])
        self.lines = cues["lines"]
        self.segments = cues["segments"]
        self.events = cues.get("events") or []
        self.motion = motion or MotionPlan(cues, env or {"speakers": {}}, league, cfg)
        self.picks = {p.pick_no: p for p in league.picks}
        self.n_teams = len(league.teams)
        self.history = {h["doc"]: h for h in (parse_history(n, md) for n, md in league.history) if h}
        self.report = league.report_card() or {}
        for ln in self.lines:  # the vertical short is always a speaker layout
            ln["_mode"] = "board" if (layout == "studio" and self.card_key(ln)[0] in BOARD_CARDS) else "speaker"
        if layout == "studio":
            c = cfg["captions"]
            wide = (float(c["max_width_px"]), int(c["font_px"]))
            side = (float(c.get("side_width_px", 860)), int(c.get("side_font_px", 48)))
            self.pages = meme_pages(self.lines, cfg, layout_for=lambda ln: side if ln.get("_mode") == "speaker" else wide)
        else:
            self.pages = meme_pages(self.lines, cfg)
        self._compute()

    # -- derived timelines
    def card_key(self, ln: dict) -> tuple:
        c = ln.get("card") or {}
        t = c.get("type")
        if t == "title":
            return ("live",)
        if t == "title_out":
            return ("disclosure",)
        if t == "quote":
            return ("quote", ln["id"])
        if t == "gm":
            return ("gm", c["team"])
        if t == "order":
            return ("order",)
        if t == "clock":
            return ("clock", c["pick_no"])
        if t == "pick":
            return ("pick", c["pick_no"])
        if t == "react":
            return ("react", c["pick_no"])
        if t == "board":
            return ("board", c["round"])
        if t == "question":
            return ("question", c["round"]) if ln["speaker"] != "host" else ("question_full", c["round"])
        if t == "history":
            return ("history", c.get("doc") or next(iter(self.history), None))
        if t == "close":
            return ("close",)
        if t == "tape":
            return ("tape",)
        if t == "highlights":
            return ("highlights", c.get("round", 1))
        if t in ("report", "roast"):
            return ("report", c["team"])
        if t in ("report_intro", "roast_frame"):
            return ("report_intro",)
        if t == "report_board":
            return ("report_board",)
        if t == "delusion":
            return ("delusion",)
        return ("live",)

    def _compute(self) -> None:
        prev_end = 0.0
        prev_ln = None
        for i, ln in enumerate(self.lines):  # the preroll already shows the first line's layout
            ln["sw"] = 0.0 if i == 0 else round(max(prev_end + 0.02, ln["start"] - 0.25), 4)
            if prev_ln is not None:  # a line that ENDS on a punch: the cut waits for the kicker (+80 ms)
                ends = [float(prev_ln["start"]) + float(mk["kicker_end"]) + 0.08
                        for mk in (prev_ln.get("beats") or {}).get("marks") or [] if mk.get("at_end")]
                if ends:
                    ln["sw"] = round(min(float(ln["start"]), max(ln["sw"], max(ends))), 4)
            prev_end = ln["end"]
            prev_ln = ln
        self.focus_ev: list[tuple[float, str | None]] = [(0.0, None)]
        self.card_ev: list[tuple[float, tuple]] = [(0.0, ("live",))]
        self.clock_ev: list[tuple[float, str | None]] = [(0.0, None)]
        self.mode_ev: list[tuple[float, str]] = [(0.0, self.lines[0]["_mode"] if self.lines else "speaker")]
        self.cut_ev: list[tuple[float, bool]] = [(0.0, False)]
        # stage focus: the current speaker, big (the host included); cutaways hold on the roasted GM
        foc: list[tuple[float, str | None, bool]] = []
        for ln in self.lines:
            foc.append((ln["sw"], ln["speaker"] if ln["_mode"] == "speaker" else None, False))
        self.cutaways = [e for e in self.events if e["type"] == "cutaway"]
        for e in self.cutaways:
            foc.append((round(e["t"], 4), e["team"], True))
        foc.sort(key=lambda x: (x[0], x[2]))
        for t, f, cut in foc:
            _push(self.focus_ev, t, f)
            _push(self.cut_ev, t, cut)
        # the camera plan (rigs): close-ups on punchlines / name calls and a CUT to the listener's close-up for the
        # reaction (animate.Plan._shots). The listener cut takes the big frame: a hard cut, REACTION CAM label on.
        self.plan = None
        self.shots: list[dict] = []
        self.hard_cuts: set[float] = set()
        if (self.cfg.get("animation") or {}).get("rigs", True):
            from .animate import Plan
            placeholder = {sp for sp, f in (self.fs or {}).items() if getattr(f, "style", "") == "placeholder"}
            self.plan = Plan(self.cues, self.L, no_close=placeholder)
            self.shots = self.plan.shot_list
        for x in self.shots:  # a reaction shot and the cutaway to the same face a beat earlier are one cut
            if x["kind"] != "listener":
                continue
            cw = [float(e["t"]) for e in self.cutaways if e["team"] == x["sp"] and x["t0"] - 0.35 <= float(e["t"]) < x["t0"]]
            if cw:
                x["t0"] = round(min(cw), 4)
        lis = [x for x in self.shots if x["kind"] == "listener"]
        early = []  # the next speaker's close-up, cut to on the kicker: the frame is theirs from its first frame
        for x in self.shots:
            if x["kind"] != "cu":
                continue
            f0 = self.value_at(self.focus_ev, x["t0"] + 1e-6)
            nxt_t = min([t for t, f in self.focus_ev if t > x["t0"] + 1e-6 and f == x["sp"]] + [1e9])
            if f0 is not None and f0 != x["sp"] and nxt_t - x["t0"] <= 1.5:
                early.append({**x, "t1": nxt_t})
        if lis or early:
            base_f, base_c = list(self.focus_ev), list(self.cut_ev)
            pts = sorted({t for t, _ in base_f} | {t for t, _ in base_c} | {x["t0"] for x in lis + early}
                         | {x["t1"] for x in lis + early})
            self.focus_ev, self.cut_ev = [(0.0, base_f[0][1])], [(0.0, base_c[0][1])]
            for t in pts:
                s_ = next((x for x in lis if x["t0"] - 1e-6 <= t < x["t1"] - 1e-6), None)
                e_ = next((x for x in early if x["t0"] - 1e-6 <= t < x["t1"] - 1e-6), None) if not s_ else None
                f_ = s_["sp"] if s_ else (e_["sp"] if e_ else self.value_at(base_f, t))
                c_ = True if s_ else self.value_at(base_c, t)
                _push(self.focus_ev, t, f_)
                _push(self.cut_ev, t, c_)
            self.hard_cuts = {round(x["t0"], 4) for x in lis} | {round(x["t1"], 4) for x in lis} | \
                {round(x["t0"], 4) for x in early}
        self._min_shots()
        self._snap_close_ups()
        from .sfx import beat2_time
        self.beat2: dict[str, float] = {}
        card_pts: list[tuple[float, int, tuple]] = []
        for i, ln in enumerate(self.lines):
            key = self.card_key(ln)
            c = ln.get("card") or {}
            b2 = beat2_time(ln) if c.get("prev_pick") and c.get("beat2_word") else None
            prev_pk = self.picks.get(c.get("prev_pick"))
            if prev_pk is not None and prev_pk.team == ln["speaker"]:
                b2 = None  # snake turn: the GM reacting to their own last pick needs no reaction cam
            if b2 is not None and b2 > ln["start"] + 0.3:
                self.beat2[ln["id"]] = b2
                card_pts.append((ln["sw"], i * 2, ("react", c["prev_pick"])))
                card_pts.append((round(max(ln["sw"] + 0.2, b2 - 0.12), 4), i * 2 + 1, key))
            else:
                card_pts.append((ln["sw"], i * 2, key))
        for t, _, key in sorted(card_pts, key=lambda x: (x[0], x[1])):
            _push(self.card_ev, t, key)
        for ln in self.lines:
            _push(self.mode_ev, ln["sw"], ln["_mode"])
            team = (self.picks[ln["pick_no"]].team
                    if ln.get("pick_no") in self.picks and ln["kind"] not in ("teaser", "hook") else None)
            _push(self.clock_ev, ln["sw"], team)
        # reveal times for picks (ticker + round boards), order rows and report-card rows
        self.pick_reveal: dict[int, float] = {}
        self.order_reveal: dict[str, float] = {}
        self.grade_reveal: dict[str, float] = {}
        self.stamp_t: dict[str, float] = {}
        for e in self.events:
            if e["type"] == "reveal":
                self.pick_reveal.setdefault(int(e["pick_no"]), float(e["t"]))
            elif e["type"] == "tick" and e.get("pick_no"):
                self.pick_reveal.setdefault(int(e["pick_no"]), float(e["t"]))
            elif e["type"] == "stamp" and e.get("team"):
                self.stamp_t.setdefault(e["team"], float(e["t"]))
                self.grade_reveal.setdefault(e["team"], float(e["t"]))
        for ln in self.lines:
            c = ln.get("card") or {}
            dur = ln["end"] - ln["start"]
            if ln["kind"] == "host_pick" and ln.get("pick_no"):
                pk = self.picks.get(ln["pick_no"])
                txt = ln.get("display_text") or ""
                pos = txt.find(pk.player_name) if pk and pk.player_name else -1
                frac = pos / max(1, len(txt)) if pos >= 0 else 0.55
                self.pick_reveal.setdefault(ln["pick_no"], round(max(ln["start"], ln["start"] + frac * dur - 0.1), 4))
            for r in c.get("reveal") or []:
                t = round(max(ln["start"], ln["start"] + float(r["at"]) * dur - 0.2), 4)
                if "pick_no" in r:
                    self.pick_reveal.setdefault(int(r["pick_no"]), t)
                elif "team" in r and c.get("type") == "report_board":
                    self.grade_reveal.setdefault(r["team"], t)
                elif "team" in r:
                    self.order_reveal.setdefault(r["team"], t)
        # (no monotonic clamp: highlight cuts reveal picks out of order; the ticker orders by reveal time)
        # delusion-index row highlights
        self.delusion_hl: list[tuple[float, str | None]] = [(0.0, None)]
        for ln in self.lines:
            c = ln.get("card") or {}
            if c.get("type") == "delusion":
                _push(self.delusion_hl, ln["sw"], (c.get("hl") or [None])[0])
            else:
                _push(self.delusion_hl, ln["sw"], None)
        # report-card grader chip highlight (the comment on air)
        self.grader_on: list[tuple[float, str | None]] = [(0.0, None)]
        for ln in self.lines:
            c = ln.get("card") or {}
            _push(self.grader_on, ln["sw"], f"{c['team']}:{c['grader']}" if c.get("type") in ("report", "roast")
                  and c.get("grader") else None)
        title = next((ln for ln in self.lines if (ln.get("card") or {}).get("type") == "title"), None)
        self.title_show = round(title["sw"], 4) if title else 0.0
        self.title_hide = round(title["end"] + 0.45, 4) if title else 0.0
        nxt_sw = {}
        for i, ln in enumerate(self.lines):
            j = i + 1
            if j < len(self.lines) and self.lines[j]["speaker"] != ln["speaker"]:
                nxt_sw[ln["id"]] = self.lines[j]["sw"]
        for pg in self.pages:
            cut = nxt_sw.get(pg["line"])
            if cut is not None and pg["hide"] > cut:
                pg["hide"] = round(max(pg["show"] + 0.2, cut), 3)
        close2 = next((ln for ln in self.lines if ln["id"] == "close-02"), None)
        self.close_show = round(close2["end"] + 0.25, 4) if close2 else None
        self.mouth: dict[str, list[tuple[float, str]]] = {}
        for sp, track in self.tracks["speakers"].items():
            self.mouth[sp] = [(round(f / self.fps, 4), st) for f, st in track]

    # -- queries
    def _min_shots(self) -> None:
        """No speaker shot under MIN_SHOT (a flash of the host on a 0.7 s cue is choppy). TV grammar: a short HOST cue
        becomes a voice-over on the GM it calls (\"Stettler, you're up!\" over Stettler); a short GM line (a
        catchphrase) is cut to earlier, over the tail of the host naming them. Reaction cuts / cutaways never move."""
        from .animate import MIN_SHOT
        ev, cut = list(self.focus_ev), self.cut_ev
        for _ in range(3):
            segs = [[t, ev[i + 1][0] if i + 1 < len(ev) else self.T, f] for i, (t, f) in enumerate(ev)]
            changed = False
            for i, sg in enumerate(segs):
                a, b, f = sg
                if f is None or b - a >= MIN_SHOT - 1e-6 or i + 1 >= len(segs) or self.value_at(cut, a + 1e-4):
                    continue
                prev = segs[i - 1] if i > 0 else None
                nxt = segs[i + 1]
                nxt_ok = nxt[2] is not None and not self.value_at(cut, nxt[0] + 1e-4)
                need = MIN_SHOT - (b - a)
                if f == "host" and nxt_ok:  # voice-over: the called GM is already on screen
                    nxt[0] = a
                    sg[1] = a
                elif prev and prev[2] is not None and not self.value_at(cut, prev[0] + 1e-4) \
                        and (a - prev[0]) - need >= MIN_SHOT:
                    sg[0] = prev[1] = round(a - need, 4)
                elif nxt_ok and (nxt[1] - nxt[0]) - need >= MIN_SHOT:
                    sg[1] = nxt[0] = round(b + need, 4)
                else:
                    continue
                changed = True
            segs = [x for x in segs if x[1] - x[0] > 1e-6]
            new_ev: list[tuple[float, str | None]] = [(0.0, segs[0][2])] if segs else [(0.0, None)]
            for a, _, f in segs[1:]:
                _push(new_ev, round(a, 4), f)
            ev = new_ev
            if not changed:
                break
        self.focus_ev = ev

    def _snap_close_ups(self) -> None:
        """After the speaker timeline settles (voice-overs, early cuts): a close-up swallows any medium sliver shorter
        than MIN_SHOT between the cut to that speaker and the close-up, or between the close-up and the cut away.
        The plan's shot dicts are edited in place, so the rig view (close-up instances) follows."""
        from .animate import MIN_SHOT
        ev = self.focus_ev
        for x in self.shots:
            if x["kind"] != "cu":
                continue
            sp = x["sp"]
            before = [t for t, f in ev if t <= x["t0"] + 1e-6]
            if not before or self.value_at(ev, x["t0"] + 1e-6) != sp:
                continue
            seg_start = max(before)
            seg_end = min([t for t, f in ev if t > x["t0"] + 1e-6] + [self.T])
            others = [y for y in self.shots if y is not x and y["t1"] > seg_start and y["t0"] < seg_end]
            if 0 < x["t0"] - seg_start < MIN_SHOT and not any(y["t1"] <= x["t0"] + 1e-6 for y in others):
                x["t0"] = round(seg_start, 4)
            if 0 < seg_end - x["t1"] < MIN_SHOT and not any(y["t0"] >= x["t1"] - 1e-6 for y in others):
                x["t1"] = round(seg_end, 4)

    def suit_window(self, sp: str, dur: float = 4.2) -> tuple[float, float] | None:
        """When the suit tag shows: the GM's first stretch (>= 2 s) on screen in the medium shot -- never over a
        close-up (it would sit on the face) or while the camera is on someone else's reaction."""
        busy = [(x["t0"], x["t1"]) for x in getattr(self, "shots", [])]
        for ln in self.lines:
            if ln["speaker"] != sp or ln.get("_mode") != "speaker":
                continue
            a0 = float(ln["start"]) + 0.6
            free = [(a0, float(ln["end"]))]
            for x0, x1 in busy:
                nxt = []
                for u, v in free:
                    if x1 <= u or x0 >= v:
                        nxt.append((u, v))
                        continue
                    if x0 > u:
                        nxt.append((u, x0))
                    if x1 < v:
                        nxt.append((x1, v))
                free = nxt
            for u, v in free:
                if v - u >= 2.0:
                    u = u + (0.2 if u > a0 else 0.0)
                    return round(u, 4), round(min(v, u + dur), 4)
        return None

    def value_at(self, ev: list[tuple[float, object]], t: float):
        v = ev[0][1]
        for tt, vv in ev:
            if tt <= t:
                v = vv
            else:
                break
        return v

    def speaking_at(self, t: float) -> str | None:
        for ln in self.lines:
            if ln["start"] <= t < ln["end"]:
                return ln["speaker"]
        return None

    def picks_revealed_before(self, t: float) -> int:
        return sum(1 for v in self.pick_reveal.values() if v < t)

    # -- chunking
    def chunks(self, limit_s: float | None = None) -> list[Chunk]:
        maxd = float(self.cfg["render"]["chunk_max_s"])
        mind = min(45.0, maxd / 2)
        fq = lambda t: round(t * self.fps) / self.fps  # noqa: E731  exact frame multiples (never rounded decimals)
        end = min(self.T, limit_s) if limit_s else self.T
        cuts = [0.0]
        cur_start = 0.0
        for s in self.segments[1:]:
            b = fq(s["start"])
            if b >= end:
                break
            if b - cur_start >= mind:
                cuts.append(b)
                cur_start = b
        if len(cuts) > 1 and fq(end) - cuts[-1] < mind / 2 and fq(end) - cuts[-2] <= maxd * 1.25:
            cuts.pop()  # fold a short tail into the previous chunk
        cuts.append(fq(end))
        final = [cuts[0]]
        busy = [e["t"] for e in self.events if e["type"] in ("reveal", "shout", "title", "stamp", "cutaway")]
        for a, b in zip(cuts, cuts[1:]):
            n = int(math.ceil((b - a) / maxd))
            if n > 1:
                for k in range(1, n):
                    target = a + (b - a) * k / n
                    cand = [ln for ln in self.lines if a + 20 < ln["sw"] < b - 20
                            and not any(0 <= ln["sw"] - bt < 3.0 for bt in busy)]
                    if cand:
                        ln = min(cand, key=lambda x: abs(x["sw"] - target))
                        prev = max((x["end"] for x in self.lines if x["end"] <= ln["start"]), default=ln["sw"])
                        cut = fq(max(prev + 0.05, ln["sw"] - 0.04))
                        if final[-1] + 5 < cut < b - 5:
                            final.append(cut)
            final.append(b)
        final = sorted(set(final))
        out = []
        for i, (a, b) in enumerate(zip(final, final[1:])):
            segs = [s["id"] for s in self.segments if s["start"] < b and s["end"] > a]
            out.append(Chunk(f"c{i + 1:02d}", a, b, segs, self.fps))
        return out


def _push(ev: list, t: float, v) -> None:
    if ev[-1][1] == v:
        return
    if ev[-1][0] >= t:
        ev[-1] = (ev[-1][0], v)
        if len(ev) > 1 and ev[-2][1] == v:
            ev.pop()
    else:
        ev.append((t, v))


# --------------------------------------------------------------------------- assets

class Assets:
    """Copies/resizes avatar frames into show/assets/gen (content-addressed).
    X = full frame for the big speaker stage, M = full frame for the host box, S = face crop for tiles / the bench."""

    SIZES = {"X": 768, "M": 256, "S": 192, "T": 128}
    CROPPED = {"S", "T"}

    def __init__(self, framesets: dict[str, FrameSet], sizes: dict[str, int] | None = None):
        self.fs = framesets
        self.sizes = sizes or self.SIZES
        self.urls: dict[tuple[str, str, str], str] = {}
        self.pose_names: dict[str, list[str]] = {}
        self.same: dict[tuple[str, str], bool] = {}
        base = SHOW_GEN / "av"
        base.mkdir(parents=True, exist_ok=True)
        for sp, fset in framesets.items():
            srcs = {st: fset.path(st) for st in STATES}
            for pose, d_ in (fset.poses or {}).items():
                for tag in ("open", "closed"):
                    if d_.get(tag):
                        srcs[f"p-{pose}-{tag}"] = Path(d_[tag])
            self.pose_names[sp] = [pz for pz in POSES if f"p-{pz}-open" in srcs and f"p-{pz}-closed" in srcs]
            for pz in self.pose_names[sp]:
                self.same[(sp, pz)] = Path(srcs[f"p-{pz}-open"]).resolve() == Path(srcs[f"p-{pz}-closed"]).resolve()
            h = hashlib.sha1("".join(f"{k}:{v}:{v.stat().st_mtime_ns}" for k, v in sorted(srcs.items()))
                             .encode()).hexdigest()[:10]
            d = base / f"{sp}-{h}"
            d.mkdir(exist_ok=True)
            crop_s = face_crop(fset) if fset.style != "placeholder" else None
            for key, src in srcs.items():
                for tag, px in self.sizes.items():
                    out = d / f"{key}-{tag}.png"
                    if not out.exists():
                        im = Image.open(src).convert("RGBA")
                        side = min(im.size)
                        box = ((im.width - side) // 2, (im.height - side) // 2,
                               (im.width + side) // 2, (im.height + side) // 2)
                        if tag in self.CROPPED and crop_s:
                            box = crop_s  # small tiles show the face, not the whole portrait
                        im = im.crop(box)
                        if im.size != (px, px):
                            im = im.resize((px, px), Image.LANCZOS)
                        im.save(out, optimize=True)
                    self.urls[(sp, key, tag)] = f"gen/av/{d.name}/{key}-{tag}.png"

    def av(self, sp: str, size: str, a: str, extra_cls: str = "", initial: str = "closed", pose: str = "none",
           poses: set[str] | None = None) -> str:
        imgs = "".join(f'<img class="f f-{st}" src="{a}{self.urls[(sp, st, size)]}" alt="">' for st in STATES)
        names = self.pose_names.get(sp, [])
        if poses is not None:  # only the gestures this chunk actually uses (lighter DOM, faster frames)
            names = [pz for pz in names if pz in poses]
        for pz in names:
            uo, uc = self.urls[(sp, f"p-{pz}-open", size)], self.urls[(sp, f"p-{pz}-closed", size)]
            if self.same.get((sp, pz)):  # one sprite for both mouth states: a single node
                imgs += f'<img class="p p-{pz}-open p-{pz}-closed" src="{a}{uo}" alt="">'
                continue
            imgs += f'<img class="p p-{pz}-open" src="{a}{uo}" alt=""><img class="p p-{pz}-closed" src="{a}{uc}" alt="">'
        has = " ".join(f"has-{pz}" for pz in names)
        pose = pose if pose in names else "none"
        return f'<div class="av av-{sp} {has} {extra_cls}" data-m="{initial}" data-p="{pose}">{imgs}</div>'


def face_crop(fset: FrameSet) -> tuple[int, int, int, int] | None:
    """Square crop around the face for small tiles: locate the mouth as the region that differs between the
    closed and open frames, then frame the head above it."""
    import numpy as np

    a = np.asarray(Image.open(fset.path("closed")).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(fset.path("open")).convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:
        return None
    diff = np.abs(a - b).sum(axis=2) > 60
    ys, xs = np.nonzero(diff)
    if len(xs) < 20:
        return None
    h, w = diff.shape
    cx, cy = float(np.median(xs)), float(np.median(ys))
    side = int(0.46 * min(w, h))
    x0 = int(min(max(0, cx - side / 2), w - side))
    y0 = int(min(max(0, cy - 0.62 * side), h - side))
    return (x0, y0, x0 + side, y0 + side)


# --------------------------------------------------------------------------- HTML builders

def contrast(a: str, b: str) -> float:
    from .avatars import hex_rgb, luminance
    la, lb = luminance(hex_rgb(a)), luminance(hex_rgb(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def team_vars(t) -> str:
    """--c1 primary, --c2 secondary, --ct readable text on --c1 (every --ct use is text on a --c1 chip): the
    secondary colour when it reads (AA 4.5:1), else the best of off-white / white / navy / black on --c1."""
    c1, c2 = t.colors
    if contrast(c1, c2) >= 4.5:
        ct = c2
    else:
        ct = max((OFF, "#FFFFFF", "#0E1B4D", "#000000"), key=lambda c: contrast(c1, c))
    return f"--c1:{c1};--c2:{c2};--ct:{ct}"


HOST_VARS = "--c1:#0E1B4D;--c2:#4770DB;--ct:#EFF0F5"


def fit(text: str, px: float, fam: str, mx: int, mn: int, lines: int = 1, weight: int = 800, width: int = 108) -> int:
    return fit_size(text, px, fam, mx, mn, lines, weight, width)[0]


def grade_cls(grade: str) -> str:
    g = (grade or "?").strip().upper()[:1]
    return {"A": "ga", "B": "gb"}.get(g, "gc")


SW, FW = 852, 972  # inner widths: side card (speaker beats), full card (board beats)
BIG_X, BIG_Y, BIG = 92, 108, 768  # the current speaker: 768 = 256x3 = 192x4 (whole-number pixel art scale)
COL_X = 952  # right column (bench strip, strap, side card, captions)


class Builder:
    def __init__(self, model: ShowModel, assets: Assets, cfg: dict, rigview=None):
        self.m, self.A, self.cfg = model, assets, cfg
        self.RV = rigview
        self.L = model.L
        self.t0 = 0.0
        self.pose0: dict[str, str] = {}
        self.used: dict[str, set[str]] = {}
        self.a = "../assets/"
        self.rc = {t["team"]: t for t in (model.report or {}).get("teams", [])}

    # ---- labels
    def who(self, sp: str) -> tuple[str, str, str]:
        """(name, franchise line, model line) for plates."""
        if sp == "host":
            return "The Commissioner", "League office · reads the ledger", "HOST"
        t = self.L.teams[sp]
        if t.has_persona:
            return t.gm_name, t.franchise_name, f"{t.display} · {t.lab}"
        if t.is_bot:
            return "Autodraft", "Control bot · no persona", "House projection"
        return t.display, "No Media Day card on file", f"{t.display} · {t.lab}"

    def speaker_vars(self, sp: str) -> str:
        return HOST_VARS if sp == "host" else team_vars(self.L.teams[sp])

    def tname(self, tid: str) -> str:
        t = self.L.teams[tid]
        return t.franchise_name if t.has_persona else ("Autodraft" if t.is_bot else t.display)

    # ---- persistent furniture
    def header(self, a: str) -> str:
        return (
            f'<div class="bug aibug" id="{{cid}}-aibug"><div class="dot"></div><div class="t1">AI-GENERATED</div>'
            f'<div class="t2">voices &amp; avatars</div></div>'
            f'<div class="bug sponsor" id="{{cid}}-sponsor"><div class="t1">PRESENTED BY</div>'
            f'<div class="t2">GAME DAY <b>SUITS</b></div></div>'
        )

    def show_title(self) -> str:
        c = self.cfg
        return (f'<div class="showtitle"><div class="t1">{esc(c["show_title"].upper())}</div>'
                f'<div class="t2">{esc(c["show_kicker"])}</div></div>')

    def order_ids(self) -> list[str]:
        return [t for t in self.L.order if t in self.L.teams] + [t for t in self.L.teams if t not in self.L.order]

    def tiles(self, a: str, clock0: str | None, on0: str | None, mouth0: dict) -> str:  # noqa: C901
        order = self.order_ids()
        rows = int(math.ceil(len(order) / 2))
        out = []
        for i, tid in enumerate(order):
            t = self.L.teams[tid]
            col, row = divmod(i, rows)
            x, y = col * (TILE_W + TILE_GAP_X), row * (TILE_H + TILE_GAP_Y)
            name = t.short_gm if t.has_persona else ("Autodraft" if t.is_bot else t.display)
            model = "Control bot" if t.is_bot else t.display
            ns = fit(name, 262, "archivo", 21, 13, 1, 700, 100)
            ms = fit(model, 200 - text_width(t.abbrev, "archivo", 14, 800, 110), "questrial", 15, 11, 1, 400, 100)
            out.append(
                f'<div class="tile" id="{{cid}}-tile-{tid}" style="left:{x}px;top:{y}px;{team_vars(t)}" '
                f'data-on="{1 if on0 == tid else 0}" data-clock="{1 if clock0 == tid else 0}">'
                f'<div class="tav" data-layout-allow-overflow><div class="tbob" id="{{cid}}-tb-{tid}">'
                f'{self.RV.html(tid, a, 70, self.t0, crop="face") if self.RV else self.A.av(tid, "S", a, initial=mouth0.get(tid, "closed"), pose=self.pose0.get(tid, "none"), poses=self.used.get(tid, set()))}'
                f'</div></div>'
                f'<div class="ab">{esc(t.abbrev)}</div><div class="md" style="font-size:{ms}px">{esc(model)}</div>'
                f'<div class="nm" style="font-size:{ns}px">{esc(name)}</div><div class="clk">ON THE CLOCK</div></div>')
        return '<div class="grid">' + "".join(out) + "</div>"

    def bench(self, a: str, clock0: str | None, on0: str | None, mouth0: dict) -> str:
        """The collapsed grid: every GM's face in one slim strip over the right column."""
        order = self.order_ids()
        n = len(order)
        chip, gap = 58, (920 - 58 * n) / max(1, n - 1) if n > 1 else 0
        out = []
        for i, tid in enumerate(order):
            t = self.L.teams[tid]
            x = round(i * (chip + gap), 1)
            out.append(f'<div class="bn" id="{{cid}}-bn-{tid}" style="left:{x}px;{team_vars(t)}" '
                       f'data-on="{1 if on0 == tid else 0}" data-clock="{1 if clock0 == tid else 0}">'
                       f'<div class="ph">{self.RV.html(tid, a, 58, self.t0, crop="face") if self.RV else self.A.av(tid, "T", a, initial=mouth0.get(tid, "closed"), pose=self.pose0.get(tid, "none"), poses=self.used.get(tid, set()))}'
                       f'</div><div class="ab">{esc(t.abbrev)}</div></div>')
        return '<div class="bench">' + "".join(out) + "</div>"

    def host_box(self, a: str, on0: bool, mouth0: dict) -> str:
        return (f'<div class="host panel" id="{{cid}}-host" data-on="{1 if on0 else 0}">'
                f'<div class="hav" data-layout-allow-overflow><div class="hbob" id="{{cid}}-hb">'
                f'{self.RV.html("host", a, 186, self.t0, crop="face") if self.RV else self.A.av("host", "M", a, initial=mouth0.get("host", "closed"), pose=self.pose0.get("host", "none"), poses=self.used.get("host", set()))}'
                f'</div></div>'
                f'<div class="who"><div class="k">HOST</div><div class="n">The Commissioner</div>'
                f'<div class="n2">Reads facts from the league ledger</div></div></div>')

    def model_card(self, sp: str, width: int) -> str:
        """No production art yet (e.g. a GM that never filed a persona): the model's name rides over the neutral
        placeholder, so a reveal still reads as a reveal."""
        fset = self.m.fs.get(sp)
        if sp == "host" or not fset or fset.style != "placeholder":
            return ""
        t = self.L.teams[sp]
        main = (t.gm_name if t.has_persona else t.display).upper()
        sub = (t.franchise_name if t.has_persona else (t.lab or "")).upper()
        ms = fit(main, width - 70, "archivo", 96, 36, 1, 900, 112)
        return (f'<div class="mcard"><div class="m1" style="font-size:{ms}px">{esc(main)}</div>'
                f'<div class="m2">{esc(sub)}</div></div>')

    def big(self, sp: str, a: str, vis: bool, mouth0: dict, cut0: bool) -> str:
        name, fr, mdl = self.who(sp)
        gs = fit(name, BIG - 12, "archivo", 50, 26, 1, 800, 108)
        fs_ = fit(fr, BIG - 12 - 20 - text_width(mdl, "questrial", 18) - 40, "questrial", 26, 16, 1, 400, 100)
        cu = ""
        if self.RV is not None and self.RV.cu.get(sp):
            on0 = any(a_ - 1e-6 <= self.t0 < b_ for a_, b_ in self.RV.cu[sp])
            cu = (f'<div class="cu" id="{{cid}}-cu-{sp}" data-layout-allow-overflow style="{_vis(on0)}">'
                  f'{self.RV.html(sp, a, BIG, self.t0, crop="close", key=f"{sp}-cu")}</div>')
        return (f'<div class="bigpt" id="{{cid}}-bp-{sp}" style="{self.speaker_vars(sp)};{_vis(vis)}">'
                f'<div class="frame" data-layout-allow-overflow><div class="bob" id="{{cid}}-bob-{sp}">'
                f'{self.RV.html(sp, a, BIG, self.t0) if self.RV else self.A.av(sp, "X", a, initial=mouth0.get(sp, "closed"), pose=self.pose0.get(sp, "none"), poses=self.used.get(sp, set()))}'
                f'</div>{cu}{self.model_card(sp, BIG)}</div>'
                f'<div class="cutlab" id="{{cid}}-cut-{sp}" style="{_vis(cut0)}">REACTION CAM</div>'
                f'<div class="plate"><div class="gm" style="font-size:{gs}px">{esc(name)}</div>'
                f'<div class="row2"><div class="fr" style="font-size:{fs_}px">{esc(fr)}</div>'
                f'<div class="mdl">{esc(mdl)}</div></div></div></div>')

    # ---- cards
    def card(self, key: tuple, a: str, vis: bool) -> str:
        kind = key[0]
        ident = slug(key)
        full = kind in BOARD_CARDS
        body = getattr(self, f"card_{kind}")(key)
        style = _vis(vis) + (";" + body[1] if isinstance(body, tuple) else "")
        inner = body[0] if isinstance(body, tuple) else body
        return (f'<div class="card panel {"full" if full else "side"} k-{kind}" id="{{cid}}-{ident}" '
                f'style="{style}">{inner}</div>')

    def card_live(self, key):
        n_ai = sum(1 for t in self.L.teams.values() if not t.is_bot)
        return (f'<div class="lab">Live from the league ledger</div>'
                f'<div class="big" style="font-size:118px;margin-top:22px">DRAFT</div>'
                f'<div class="big" style="font-size:118px;color:#E32402">NIGHT</div><div class="rule"></div>'
                f'<div class="row"><span class="pill red">{n_ai} AI GMs</span><span class="pill royal">1 CONTROL BOT</span>'
                f'<span class="pill ghost">{self.L.rounds} ROUNDS</span></div>')

    def card_disclosure(self, key):
        n_ai = sum(1 for t in self.L.teams.values() if not t.is_bot)
        return (f'<div class="lab">How tonight works</div>'
                f'<div class="big" style="font-size:64px;margin-top:16px">Their words.</div>'
                f'<div class="big" style="font-size:64px">Synthetic voices.</div>'
                f'<div class="rule"></div>'
                f'<div class="txt" style="font-size:24px">Every general manager line is the model\'s own output, read from '
                f'the league ledger. Voices and avatars are AI-generated. The Commissioner reads facts from the ledger.</div>'
                f'<div class="row" style="margin-top:20px"><span class="pill red">{n_ai} AI GMs</span>'
                f'<span class="pill royal">1 CONTROL BOT</span><span class="pill ghost">{self.L.rounds} ROUNDS</span></div>')

    def card_quote(self, key):
        ln = next(x for x in self.m.lines if x["id"] == key[1])
        p = self.m.picks.get(ln.get("pick_no") or (ln.get("card") or {}).get("pick_no"))
        if not p:
            return self.card_live(key)
        pt = self.L.teams[p.team]
        rt = self.L.teams.get(ln["speaker"])
        who = (rt.gm_name if rt and rt.has_persona else (rt.display if rt else ""))
        picker = pt.franchise_name if pt.has_persona else pt.display
        head = f"Round {p.round} · Pick {p.pick_no}"
        what = (f"{esc(who)} on taking <b>{esc(p.player_name)}</b>" if rt and rt.id == p.team else
                f"{esc(who)} on the {esc(picker)} taking <b>{esc(p.player_name)}</b>")
        return (f'<div class="lab">Coming up tonight</div><div class="big" style="font-size:'
                f'{fit(head, SW, "archivo", 84, 40, 1)}px;margin-top:18px">{esc(head)}</div><div class="rule"></div>'
                f'<div class="txt" style="font-size:30px">{what} ({esc(pos_display(p.position))} · {esc(p.nhl_team)}).</div>',
                team_vars(pt))

    def card_gm(self, key):
        t = self.L.teams[key[1]]
        slot = (self.L.order.index(t.id) + 1) if t.id in self.L.order else None
        lab = f"Meet the GM{f' · draft slot {slot}' if slot else ''}"
        if not t.has_persona:
            title = "Autodraft" if t.is_bot else t.display
            sub = "Control bot · house projection" if t.is_bot else f"{t.lab} · no Media Day card on file"
            size = fit(title, SW, "archivo", 64, 30, 2)
            return (f'<div class="lab">{esc(lab)}</div><div class="big" style="font-size:{size}px;margin-top:16px">'
                    f'{esc(title)}</div><div class="rule"></div><div class="txt">{esc(sub)}</div>', team_vars(t))
        size = fit(t.franchise_name, SW, "archivo", 56, 28, 1)
        tag = t.p("tagline") or ""
        tsz, _ = fit_size(f"“{tag}”", SW, "questrial", 26, 17, 3)
        chips = "".join(f"<span>{esc(x)}</span>" for x in (t.p("personality") or [])[:4])
        suit = t.p("suit") or {}
        fab = self.L.fabrics.get(suit.get("fabric_id") or "", {})
        jk = suit.get("jacket") or {}
        bits = [b for b in [fab.get("summary"), f"{jk.get('lapel_style')} lapels" if jk.get("lapel_style") else None,
                            jk.get("button_layout"), suit.get("tie")] if b]
        suit_txt = "; ".join(bits)
        if len(suit_txt) > 110:
            suit_txt = suit_txt[:107].rsplit(" ", 1)[0] + "…"
        city = f'<span class="stat">{esc(t.city)}</span>' if t.city else ""
        return (f'<div class="lab">{esc(lab)}</div>'
                f'<div class="big" style="font-size:{size}px;margin-top:12px">{esc(t.franchise_name)}</div>'
                f'<div class="row"><span class="pill" style="background:var(--c1);color:var(--ct);'
                f'box-shadow:inset 0 0 0 2px var(--c2)">{esc(t.abbrev)}</span>{city}</div><div class="rule"></div>'
                f'<div class="txt" style="font-size:{tsz}px">“{esc(tag)}”</div><div class="chips">{chips}</div>'
                + (f'<div class="suit"><b>Suit</b> · {esc(suit_txt)}</div>' if suit_txt else ""), team_vars(t))

    def card_clock(self, key):
        p = self.m.picks[key[1]]
        t = self.L.teams[p.team]
        name = self.tname(p.team)
        return (f'<div class="lab">On the clock</div><div class="big" style="font-size:110px;margin-top:10px">'
                f'Pick {p.pick_no}</div><div class="rule"></div>'
                f'<div class="big" style="font-size:{fit(name, SW, "archivo", 48, 24, 2)}px">{esc(name)}</div>',
                team_vars(t))

    def card_pick(self, key):
        p = self.m.picks[key[1]]
        t = self.L.teams[p.team]
        in_round = (p.pick_no - 1) % self.m.n_teams + 1
        size = fit(p.player_name, SW, "archivo", 84, 40, 1)
        proj = ""
        if p.projected_points is not None:
            rng = ""
            if p.range_80 and len(p.range_80) == 2:
                rng = f' · 80% <b>{int(p.range_80[0])}–{int(p.range_80[1])}</b>'
            proj = f'<span class="stat">Projection <b>{int(round(p.projected_points))}</b> pts{rng}</span>'
        name = self.tname(p.team)
        auto = ""
        if p.auto:
            auto = ('<span class="pill ghost">CONTROL BOT</span>' if t.is_bot else '<span class="pill ghost">AUTO PICK</span>')
        flag = self.L.value_flags().get(p.pick_no)
        flag_pill = {"reach": '<span class="pill gold">REACH?!</span>', "steal": '<span class="pill royal">STEAL!</span>'}.get(flag or "", "")
        shown = self.m.pick_reveal.get(p.pick_no, 1e9) <= self.t0 + 1e-6
        pre_txt = "On the clock." if self.cfg["script"].get("format", "party") == "party" else "The pick is in."
        pre = (f'<div id="{{cid}}-pk{p.pick_no}-pre" style="{_vis(not shown)}"><div class="big" style="font-size:84px">'
               f'{pre_txt}</div><div class="stat" style="margin-top:10px">{esc(self.tname(p.team))}</div></div>')
        post = (f'<div id="{{cid}}-pk{p.pick_no}-post" style="{_vis(shown)}">'
                f'<div class="big" style="font-size:{size}px">{esc(p.player_name)}</div>'
                f'<div class="row"><span class="pill red">{esc(pos_display(p.position))}</span>'
                f'<span class="pill ghost">{esc(p.nhl_team)}</span>{auto}{flag_pill}{proj}</div></div>')
        by = (f'<div class="by"><span class="k">Selected by</span> <b style="font-size:'
              f'{fit(name, SW - 150, "archivo", 30, 18, 1)}px">{esc(name)}</b></div>')
        gm = f'<div class="stat" style="margin-top:6px">GM {esc(t.gm_name)} · {esc(t.display)}</div>' if t.has_persona else ""
        rec = ""
        if p.on_air_call and p.rationale and not t.is_bot:  # the rationale names the player: after the reveal only
            rsz, _ = fit_size(p.rationale, SW, "questrial", 18, 12, 3, 400, 100)
            rec = (f'<div class="rec" id="{{cid}}-pk{p.pick_no}-rec" style="font-size:{rsz}px;{_vis(shown)}">'
                   f'<b>FOR THE RECORD</b> {esc(p.rationale)}</div>')
        return (f'<div class="lab">Round {p.round} · pick {in_round} · overall {p.pick_no}</div>'
                f'<div class="pk">{pre}{post}</div><div class="rule"></div>{by}{gm}{rec}', team_vars(t))

    def card_react(self, key):
        """Beat 1 of a call: the previous pick, and the buddy it was made by (the reaction cam)."""
        p = self.m.picks[key[1]]
        t = self.L.teams[p.team]
        who = t.short_gm if t.has_persona else ("Autodraft" if t.is_bot else t.display)
        nsz = fit(p.player_name, SW - 330, "archivo", 54, 26, 2)
        wsz = fit(who, SW - 330, "archivo", 30, 18, 1)
        return (f'<div class="lab">On the last pick · #{p.pick_no}</div>'
                f'<div class="rcam"><div class="ph">{self.RV.html(p.team, self.a, 290, self.t0, crop="bust") if self.RV else self.A.av(p.team, "M", self.a, initial="closed", pose=self.pose0.get(p.team, "none"), poses=self.used.get(p.team, set()))}</div>'
                f'<div class="tg">REACTION CAM</div></div>'
                f'<div class="rtx"><div class="big" style="font-size:{wsz}px">{esc(who)}</div>'
                f'<div class="stat" style="margin-top:6px">{esc(self.tname(p.team))}</div><div class="rule"></div>'
                f'<div class="stat">took</div><div class="big" style="font-size:{nsz}px;margin-top:4px">{esc(p.player_name)}</div>'
                f'<div class="row"><span class="pill red">{esc(pos_display(p.position))}</span>'
                f'<span class="pill ghost">{esc(p.nhl_team)}</span></div></div>', team_vars(t))

    def team_row(self, tid: str, n: str, main: str, sub: str, rid: str, vis: bool, right: str = "") -> str:
        t = self.L.teams[tid]
        ms = fit(main, 300, "archivo", 21, 14, 1, 700, 100)
        return (f'<div class="tr" id="{{cid}}-{rid}" style="{team_vars(t)};{_vis(vis)}"><div class="n">{esc(n)}</div>'
                f'<div class="ab">{esc(t.abbrev)}</div><div><div class="nm" style="font-size:{ms}px">{esc(main)}</div>'
                f'<div class="sm">{esc(sub)}</div></div>{right}</div>')

    def card_order(self, key, t0: float = 0.0):
        ev = self.L.order_event or {}
        rows = []
        for i, tid in enumerate(self.L.order):
            if tid not in self.L.teams:
                continue
            t = self.L.teams[tid]
            main = t.franchise_name if t.has_persona else ("Autodraft" if t.is_bot else t.display)
            sub = "Control bot" if t.is_bot else f"{t.display} · {t.lab}"
            rt = self.m.order_reveal.get(tid, 0.0)
            rows.append(self.team_row(tid, str(i + 1), main, sub, f"ord-{tid}", rt <= self.t0 + 1e-6))
        half = int(math.ceil(len(rows) / 2))
        rnd = str(ev.get("randomness") or "")
        foot = f"Method: {esc(str(ev.get('method') or 'n/a'))}"
        if rnd:
            foot += f" · randomness {esc(rnd[:24])}…"
        if self.L.is_snake():
            foot += " · snake draft: the order reverses every round"
        return (f'<div class="lab">The draft order · round 1</div><div class="cols tbl" style="margin-top:14px">'
                f'<div>{"".join(rows[:half])}</div><div>{"".join(rows[half:])}</div></div><div class="foot">{foot}</div>')

    def card_board(self, key):
        rnd = key[1]
        picks = self.L.picks_in_round(rnd)
        rows = []
        for p in picks:
            rt = self.m.pick_reveal.get(p.pick_no, 1e9)
            rows.append(self.team_row(p.team, str(p.pick_no), p.player_name,
                                      f"{pos_display(p.position)} · {p.nhl_team}", f"brd-{p.pick_no}",
                                      rt <= self.t0 + 1e-6))
        half = int(math.ceil(len(rows) / 2))
        return (f'<div class="lab">Round {rnd} · the board</div><div class="cols tbl" style="margin-top:14px">'
                f'<div>{"".join(rows[:half])}</div><div>{"".join(rows[half:])}</div></div>')

    def card_history(self, key):
        h = self.m.history.get(key[1]) if key[1] else None
        if not h:
            return '<div class="lab">Previously on GM-Bench</div>'
        hdr = {k.lower(): k for k in h["headers"]}
        pts = next((hdr[k] for k in hdr if k in ("pts", "points")), None) or next((hdr[k] for k in hdr if "unspent" in k), None)
        gm = next((hdr[k] for k in hdr if k.startswith("gm")), None)
        model = hdr.get("model")
        rows = []
        for i, r in enumerate(h["rows"]):
            who = r.get(gm, "") if gm else ""
            sub = r.get(model, "") if model else ""
            rows.append(f'<div class="tr"><div class="n">{i + 1}</div><div><div class="nm">{esc(who)}</div>'
                        f'<div class="sm">{esc(sub)}</div></div><div class="nm" style="margin-left:auto">'
                        f'{esc(r.get(pts, "")) if pts else ""}</div></div>')
        size = fit(h["title"], FW, "archivo", 52, 28, 2)
        return (f'<div class="lab">Previously on GM-Bench</div><div class="big" style="font-size:{size}px;margin-top:14px">'
                f'{esc(h["title"])}</div><div class="rule"></div><div class="tbl" style="width:900px">{"".join(rows)}</div>')

    def _question(self, key, full: bool):
        says = self.L.table_talk(key[1])
        ln = next((x for x in self.m.lines if (x.get("card") or {}).get("type") == "question"
                   and (x.get("card") or {}).get("round") == key[1]), None)
        q = (ln or {}).get("card", {}).get("question") or (says[0].cue if says else "")
        w = FW if full else SW
        size = fit(q, w, "archivo", 58 if full else 44, 24, 4 if full else 5, 700, 100)
        return (f'<div class="lab">Table talk · round {key[1]}</div><div class="big" style="font-size:{size}px;'
                f'margin-top:18px;font-weight:700;font-stretch:100%">{esc(q)}</div>')

    def card_question_full(self, key):
        return self._question(key, True)

    def card_question(self, key):
        return self._question(key, False)

    def card_highlights(self, key):
        n_ai = sum(1 for t in self.L.teams.values() if not t.is_bot)
        return (f'<div class="lab">Draft Night · the best of round {key[1]}</div>'
                f'<div class="big" style="font-size:132px;margin-top:8px">ROUND {key[1]}</div>'
                f'<div class="big" style="font-size:{fit("HIGHLIGHTS", SW, "archivo", 124, 60, 1)}px;color:#E32402">'
                f'HIGHLIGHTS</div><div class="rule"></div>'
                f'<div class="row"><span class="pill red">{n_ai} AI GMs</span><span class="pill royal">1 CONTROL BOT</span>'
                f'<span class="pill ghost">EVERY WORD THEIR OWN</span></div>')

    def card_tape(self, key):
        return ('<div class="lab">▶ Play · the trash-talk tape</div><div class="big" style="font-size:96px;margin-top:18px">'
                'THE TRASH-TALK</div><div class="big" style="font-size:96px;color:#E32402">TAPE.</div><div class="rule"></div>'
                '<div class="row"><span class="pill red">● REC</span><span class="pill ghost">GM WORDS · UNEDITED</span>'
                '<span class="pill ghost">NO REFEREES</span></div>')

    def card_close(self, key):
        n_lines = sum(1 for x in self.m.lines if x.get("own_words"))
        return (f'<div class="lab">Draft complete</div><div class="big" style="font-size:84px;margin-top:18px">'
                f'{len(self.L.picks)} picks.</div><div class="big" style="font-size:84px">{self.L.rounds} rounds.</div>'
                f'<div class="rule"></div><div class="txt" style="font-size:26px">{len(self.L.teams)} franchises · '
                f'{n_lines} GM lines on air tonight, every one in the GM\'s own words.</div>')

    # ---- report card
    def card_report_intro(self, key):
        teams = list(self.rc.values())
        n_graders = len({g["from"] for t in teams for g in t["grades"]})
        n_grades = sum(len(t["grades"]) for t in teams)
        return (f'<div class="lab">Post-draft · the GMs grade each other</div>'
                f'<div class="big" style="font-size:150px;margin-top:18px;line-height:1.02">REPORT</div>'
                f'<div class="big" style="font-size:150px;line-height:1.02;color:#E32402">CARD</div><div class="rule"></div>'
                f'<div class="txt" style="font-size:26px;width:900px">{n_graders} GMs graded every other draft: {n_grades} '
                f'letter grades, each with a line of trash talk in the grader\'s own words. Bottom of the class first.</div>'
                f'<div class="row" style="margin-top:18px"><span class="pill red">A+ TO F</span>'
                f'<span class="pill royal">GPA = AVERAGE GRADE</span><span class="pill ghost">NO MERCY</span></div>')

    def card_report(self, key):
        tid = key[1]
        t = self.L.teams[tid]
        r = self.rc.get(tid) or {"grades": [], "gpa": None, "letter": "?"}
        from .report import ranked
        teams = ranked(list(self.rc.values()))
        rank = next((i + 1 for i, x in enumerate(teams) if x["team"] == tid), None)
        name = self.tname(tid)
        nsz = fit(name, 560, "archivo", 50, 26, 2)
        _, _, mdl = self.who(tid)
        gm = f"GM {t.gm_name} · {t.display}" if t.has_persona else mdl
        st = self.m.stamp_t.get(tid)
        shown = st is None or st <= self.t0 + 1e-6
        stamp = (f'<div class="stamp {grade_cls(r["letter"])}" id="{{cid}}-rs-{tid}" data-layout-allow-overlap '
                 f'style="{_vis(shown)}"><div class="l" data-layout-allow-overlap>{esc(r["letter"])}</div>'
                 f'<div class="g" data-layout-allow-overlap>GPA {float(r["gpa"]):.2f}</div></div>'
                 if r.get("gpa") is not None else "")
        chips = []
        for g in sorted(r["grades"], key=lambda g: self.order_ids().index(g["from"]) if g["from"] in self.L.teams else 99):
            gt = self.L.teams.get(g["from"])
            if not gt:
                continue
            chips.append(f'<div class="gch" id="{{cid}}-gc-{tid}-{g["from"]}" data-on="0" style="{team_vars(gt)}">'
                         f'<span class="ab">{esc(gt.abbrev)}</span><span class="gl {grade_cls(g["grade"])}">'
                         f'{esc(g["grade"])}</span></div>')
        lab = f"Report card · {ordinal_suffix(rank)} of {len(teams)}" if rank else "Report card"
        return (f'<div class="lab">{esc(lab)}</div><div class="big" style="font-size:{nsz}px;margin-top:12px;width:560px">'
                f'{esc(name)}</div><div class="stat" style="margin-top:8px">{esc(gm)}</div>{stamp}<div class="rule"></div>'
                f'<div class="lab" style="font-size:15px">Every grade · who gave what</div>'
                f'<div class="gchips">{"".join(chips)}</div>', team_vars(t))

    def card_report_board(self, key):
        from .report import ranked
        teams = ranked(list(self.rc.values()))
        rows = []
        for i, r in enumerate(teams):
            tid = r["team"]
            t = self.L.teams[tid]
            rt = self.m.grade_reveal.get(tid, 1e9)
            shown = rt <= self.t0 + 1e-6
            sub = f"GPA {float(r['gpa']):.2f} · {t.display}"
            right = (f'<div class="lt q" id="{{cid}}-rcq-{tid}" style="{_vis(not shown)}">?</div>'
                     f'<div class="lt {grade_cls(r["letter"])}" id="{{cid}}-rcl-{tid}" style="{_vis(shown)}">'
                     f'{esc(r["letter"])}</div>')
            rows.append(self.team_row(tid, str(i + 1), self.tname(tid), sub, f"rcb-{tid}", True, right))
        half = int(math.ceil(len(rows) / 2))
        return (f'<div class="lab">Report card · the class of Draft Night</div><div class="cols tbl" style="margin-top:14px">'
                f'<div>{"".join(rows[:half])}</div><div>{"".join(rows[half:])}</div></div>')

    def card_delusion(self, key):
        from .report import delusion_rows
        rows = delusion_rows(list(self.rc.values()))
        n = max(2, len(self.L.teams))
        x0, w = 372, 470
        out = []
        for r in rows:
            t = self.L.teams[r["team"]]
            who = t.short_gm if t.has_persona else t.display
            xs = x0 + (r["self"] - 1) / (n - 1) * w
            xa = x0 + (r["avg"] - 1) / (n - 1) * w
            lo, hi = min(xs, xa), max(xs, xa)
            gap = f'+{r["gap"]:.1f}' if r["gap"] > 0 else f'{r["gap"]:.1f}'
            out.append(f'<div class="dr" id="{{cid}}-dl-{r["team"]}" data-on="0" style="{team_vars(t)}">'
                       f'<div class="ab">{esc(t.abbrev)}</div><div class="nm">{esc(who)}</div>'
                       f'<div class="bar" style="left:{lo:.0f}px;width:{max(4, hi - lo):.0f}px"></div>'
                       f'<div class="ds" style="left:{xs - 9:.0f}px"></div><div class="da" style="left:{xa - 9:.0f}px"></div>'
                       f'<div class="gp">{gap}</div></div>')
        ticks = "".join(f'<div class="tk" style="left:{x0 + (k - 1) / (n - 1) * w - 20:.0f}px">{k}</div>'
                        for k in (1, 4, 7, 10, 14) if k <= n)
        return (f'<div class="lab">Post-draft · self-prediction vs the league</div>'
                f'<div class="big" style="font-size:54px;margin-top:6px">THE DELUSION INDEX</div>'
                f'<div class="legend"><span class="ks"></span>Where the GM put their own team'
                f'<span class="ka"></span>Where the rest of the league put them (average)</div>'
                f'<div class="dchart"><div class="ticks">{ticks}</div>{"".join(out)}</div>')

    # ---- captions, ticker, straps, overlays
    def short_label(self, sp: str, addressed: str | None) -> str:
        if sp == "host":
            s = "THE COMMISSIONER"
        else:
            t = self.L.teams[sp]
            s = f"{t.short_gm} · {t.nickname}" if t.has_persona else t.display
        if addressed and addressed in self.L.teams and addressed != sp:
            tt = self.L.teams[addressed]
            s += "  →  " + (tt.nickname if tt.has_persona else tt.display)
        return s.upper()

    def caption_page(self, i: int, pg: dict, vis: bool, t: float) -> str:
        ln = next(x for x in self.m.lines if x["id"] == pg["line"])
        b2 = self.m.beat2.get(ln["id"])
        to = ln.get("addressed_to") if (b2 is None or pg["start"] < b2 - 0.05) else None
        label = self.short_label(pg["speaker"], to)
        side = pg.get("mode") == "speaker"
        ls = fit(label, 860 if side else 1340, "archivo", 19, 12, 1, 800, 105)
        size = int(pg.get("size") or 44)
        rows = []
        k = 0
        for row in pg["rows"]:
            spans = []
            for w in row:
                active = w["start"] <= t < w["end"]
                spans.append(f'<span class="w" id="{{cid}}-w{i}_{k}" style="color:{RED if active else OFF}">{esc(w["w"])}</span>')
                k += 1
            rows.append(f'<div class="r" style="font-size:{size}px;line-height:{round(size * 1.36)}px;'
                        f'height:{round(size * 1.36)}px">' + " ".join(spans) + "</div>")
        return (f'<div class="mc {"s" if side else "b"}" id="{{cid}}-cp{i}" style="{self.speaker_vars(pg["speaker"])};{_vis(vis)}">'
                f'<div class="who"><div class="sw"></div><div class="t" style="font-size:{ls}px">{esc(label)}</div></div>'
                f'<div class="rows">{"".join(rows)}</div></div>')

    def ticker(self, upto: list[int], hint: bool) -> str:
        ents = []
        for idx, pn in enumerate(upto):
            p = self.m.picks[pn]
            t = self.L.teams[p.team]
            room = (TICK_W - 28 - 56 - 30 - (text_width(t.abbrev, "archivo", 14, 800, 110) + 16)
                    - text_width(f"{pos_display(p.position)} · {p.nhl_team}", "questrial", 15) - 4)
            ps = fit(p.player_name, room, "archivo", 17, 11, 1, 700, 100)
            ents.append(f'<div class="te" style="left:{idx * TICK_W}px;{team_vars(t)}"><div class="no">R{p.round}·{p.pick_no}</div>'
                        f'<div class="ab">{esc(t.abbrev)}</div><div class="pl" style="font-size:{ps}px">{esc(p.player_name)}</div>'
                        f'<div class="ps">{esc(pos_display(p.position))} · {esc(p.nhl_team)}</div></div>')
        return (f'<div class="ticker"><div class="lab" data-layout-allow-overlap>DRAFT BOARD</div><div class="view">'
                + (f'<div class="hint" id="{{cid}}-hint">Picks land here as they are announced</div>' if hint else "")
                + f'<div class="strip" id="{{cid}}-strip" data-layout-allow-overflow style="width:{max(1, len(upto)) * TICK_W}px">{"".join(ents)}</div>'
                f'</div></div>')

    def strap(self, seg: dict, vis: bool, where: str) -> str:
        sub = self.strap_sub(seg)
        return (f'<div class="strap {where}" id="{{cid}}-st{where}-{seg["id"]}" style="{_vis(vis)}"><div class="chip">'
                f'{esc(seg["strap"])}</div><div class="sub">{esc(sub)}</div></div>')

    def strap_sub(self, seg: dict) -> str:
        k = seg["kind"]
        if seg.get("round"):
            picks = self.L.picks_in_round(seg["round"])
            if picks and k != "table_talk":
                return f"Picks {picks[0].pick_no}–{picks[-1].pick_no} of {self.m.n_teams * self.L.rounds}"
            return "The panel weighs in"
        return {"cold_open": "Cold open", "previously": "Past AI drafts",
                "meet": f"{sum(1 for t in self.L.teams.values() if not t.is_bot)} AI GMs + 1 control bot",
                "order": "Set by published randomness", "close": "Presented by Game Day Suits",
                "report_card": "The GMs grade each other", "trash_tape": "GM words · unedited"}.get(k, "")

    def overlay_title(self, vis: bool) -> str:
        L = self.L
        n_ai = sum(1 for t in L.teams.values() if not t.is_bot)
        date = self.draft_date() if L.picks else ""
        return (f'<div class="ovl" id="{{cid}}-ovl-title" style="{_vis(vis)}"><div class="bg2"></div><div class="blk">'
                f'<div class="k1" id="{{cid}}-ot1">GM-Bench presents</div>'
                f'<div class="ttl" id="{{cid}}-ot2">{esc(self.cfg["show_title"].upper())}</div>'
                f'<div class="bar" id="{{cid}}-ot3"></div>'
                f'<div class="l2" id="{{cid}}-ot4">{n_ai} AI GMs · 1 CONTROL BOT · {L.rounds} ROUNDS · '
                f'{self.m.n_teams * L.rounds} PICKS</div>'
                f'<div class="l3" id="{{cid}}-ot5">{esc(date)}{" · " if date else ""}The Suits 2026–27</div></div>'
                f'<div class="l4" id="{{cid}}-ot6">Presented by Game Day Suits · AI-generated voices and avatars · '
                f'GM words are the models\' own</div></div>')

    def overlay_close(self, vis: bool) -> str:
        opener = ""
        if self.L.first_puck_drop_utc:
            import datetime as dt
            from zoneinfo import ZoneInfo
            d = dt.datetime.fromisoformat(self.L.first_puck_drop_utc.replace("Z", "+00:00"))
            d = d.astimezone(ZoneInfo(self.L.timezone or "America/Edmonton"))
            opener = f"Season opens {d.strftime('%a %b')} {d.day}"
        return (f'<div class="ovl" id="{{cid}}-ovl-close" style="{_vis(vis)}"><div class="bg2"></div><div class="blk">'
                f'<div class="k1">That\'s the draft</div><div class="ttl" style="font-size:150px">{esc(self.cfg["show_title"].upper())}</div>'
                f'<div class="bar"></div><div class="l2">{esc(opener.upper())}</div>'
                f'<div class="l3">Presented by Game Day Suits · gamedaysuits.ca</div></div>'
                f'<div class="l4">GM lines: the models\' own words from the league ledger · voices and avatars are AI-generated</div></div>')

    def draft_date(self) -> str:
        import datetime as dt
        from zoneinfo import ZoneInfo
        ts = None
        try:
            for line in (self.L_ledger_path()).open():
                e = json.loads(line)
                if e.get("type") == "DRAFT_PICK":
                    ts = e["ts"]
                    break
        except OSError:
            return ""
        if not ts:
            return ""
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(self.L.timezone or "America/Edmonton"))
        return f"{d.strftime('%A, %B')} {d.day}, {d.year}"

    def L_ledger_path(self) -> Path:
        from .paths import run_dir
        return run_dir(self.L.run) / "ledger" / "league.jsonl"

    # ---- chunk assembly
    def chunk_html(self, ch: Chunk, standalone: bool) -> tuple[str, dict]:  # noqa: C901
        m = self.m
        a = "../assets/" if standalone else "assets/"
        t0, t1 = ch.t0, ch.t1
        self.t0 = t0
        self.a = a
        cid = ch.cid
        EPS = 1e-6
        focus0 = m.value_at(m.focus_ev, t0)
        cut0 = m.value_at(m.cut_ev, t0)
        card0 = m.value_at(m.card_ev, t0)
        clock0 = m.value_at(m.clock_ev, t0)
        mode0 = m.value_at(m.mode_ev, t0)
        speak0 = m.speaking_at(t0)
        mouth0 = {sp: _state_at(ev, t0) for sp, ev in m.mouth.items()}
        self.pose0 = {sp: m.motion.pose_at(sp, t0) for sp in m.mouth}

        def window(ev):
            return [(t, v) for t, v in ev if t0 - EPS < t < t1 and t > 0.0]

        foc_w = window(m.focus_ev)
        cut_w = window(m.cut_ev)
        card_w = window(m.card_ev)
        clock_w = window(m.clock_ev)
        mode_w = window(m.mode_ev)
        focus_set = {v for _, v in foc_w if v} | ({focus0} if focus0 else set())
        cards_set = [card0] + [v for _, v in card_w if v not in (card0,)]
        cards_set = list(dict.fromkeys(cards_set))

        data = {"m": [], "a": [], "v": [], "c": [], "x": [], "e": [], "p": [], "ps": []}
        vis_events: dict[str, list[tuple[float, int, float, float]]] = {}

        def vis(el: str, t: float, on: int, d: float, dy: float = 0.0) -> None:
            vis_events.setdefault(el, []).append((round(t - t0, 4), on, d, dy))

        def attr(t: float, el: str, name: str, val: str) -> None:
            data["a"].append([round(t - t0, 4), el, name, val])

        # layout mode: speaker beats (big speaker + bench strip) <-> board beats (full grid + host box)
        prev = mode0
        for t, v in mode_w:
            if v != prev:
                vis("brd", t, 1 if v == "board" else 0, 0.0)
                vis("spk", t, 1 if v == "speaker" else 0, 0.0)
            prev = v
        # the big speaker (hard cuts, TV style) + the reaction-cam label on cutaways
        prev = focus0
        for t, v in foc_w:
            if prev:
                vis(f"bp-{prev}", t, 0, 0.0)
            if v:
                vis(f"bp-{v}", t, 1, 0.0 if round(t, 4) in m.hard_cuts else 0.12)
            prev = v
        if self.RV is not None:  # close-ups: cuts inside the big frame (the medium instance keeps playing under it)
            for sp in focus_set:
                for a_, b_ in self.RV.cu.get(sp, []):
                    if t0 + EPS < a_ < t1:
                        vis(f"cu-{sp}", a_, 1, 0.0)
                    if t0 + EPS < b_ < t1:
                        vis(f"cu-{sp}", b_, 0, 0.0)
        prev_cut, prev_f = cut0, focus0
        cut_on_el = f"cut-{focus0}" if (cut0 and focus0) else None
        for t, v in sorted(set(foc_w) | {(tt, None) for tt, _ in cut_w}, key=lambda x: x[0]):
            f = m.value_at(m.focus_ev, t + 1e-4)
            c = m.value_at(m.cut_ev, t + 1e-4)
            want = f"cut-{f}" if (c and f) else None
            if want != cut_on_el:
                if cut_on_el:
                    vis(cut_on_el, t, 0, 0.0)
                if want:
                    vis(want, t, 1, 0.12, -8)
                cut_on_el = want
        # cards
        prev = card0
        for t, v in card_w:
            if prev:
                vis(slug(prev), t, 0, 0.0)
            vis(slug(v), t, 1, 0.3, 18)
            prev = v
        kinds_in = {k[0] for k in cards_set}
        # order rows / board rows / report-board rows
        for tid, rt in m.order_reveal.items():
            if t0 + EPS < rt < t1 and "order" in kinds_in:
                vis(f"ord-{tid}", rt, 1, 0.3, 10)
        for pn, rt in m.pick_reveal.items():
            p = m.picks.get(pn)
            if p and t0 + EPS < rt < t1 and ("board", p.round) in cards_set:
                vis(f"brd-{pn}", rt, 1, 0.3, 10)
        if "report_board" in kinds_in:
            for tid, rt in m.grade_reveal.items():
                if t0 + EPS < rt < t1:
                    vis(f"rcq-{tid}", rt, 0, 0.0)
                    data["p"].append([round(rt - t0, 4), f"rcl-{tid}", 0])
        # pick cards: "the pick is in" -> player reveal beat
        for k in cards_set:
            if k[0] == "pick":
                rt = m.pick_reveal.get(k[1])
                if rt is not None and t0 + EPS < rt < t1:
                    vis(f"pk{k[1]}-pre", rt, 0, 0.0)  # hard swap: the name slam covers the cut
                    vis(f"pk{k[1]}-post", rt, 1, 0.35, 14)
                    p_ = m.picks.get(k[1])
                    if p_ is not None and p_.on_air_call and p_.rationale:
                        vis(f"pk{k[1]}-rec", round(rt + 0.6, 4), 1, 0.4, 0)
            if k[0] == "report":
                st = m.stamp_t.get(k[1])
                if st is not None and t0 + EPS < st < t1:
                    data["p"].append([round(st - t0, 4), f"rs-{k[1]}"])
                elif st is None or st <= t0 + EPS:
                    data["ps"].append(f"rs-{k[1]}")
        # report-card grader chips + delusion rows (highlight the one on air)
        prev = m.value_at(m.grader_on, t0)
        if prev and "report" in kinds_in:
            pass  # initial state set in the DOM below
        for t, v in window(m.grader_on):
            if prev:
                attr(t, "gc-" + prev.replace(":", "-"), "data-on", "0")
            if v:
                attr(t, "gc-" + v.replace(":", "-"), "data-on", "1")
            prev = v
        grader0 = m.value_at(m.grader_on, t0)
        prev = m.value_at(m.delusion_hl, t0)
        dl0 = prev
        for t, v in window(m.delusion_hl):
            if "delusion" not in kinds_in:
                break
            if prev:
                attr(t, f"dl-{prev}", "data-on", "0")
            if v:
                attr(t, f"dl-{v}", "data-on", "1")
            prev = v
        # clock tag (tiles + bench)
        prev = clock0
        for t, v in clock_w:
            for pre_ in ("tile", "bn"):
                if prev:
                    attr(t, f"{pre_}-{prev}", "data-clock", "0")
                if v:
                    attr(t, f"{pre_}-{v}", "data-clock", "1")
            prev = v
        # speaking highlight
        for ln in m.lines:
            for t, on in ((ln["start"], "1"), (ln["end"], "0")):
                if t0 + EPS < t < t1:
                    if ln["speaker"] == "host":
                        attr(t, "host", "data-on", on)
                    else:
                        attr(t, f"tile-{ln['speaker']}", "data-on", on)
                        attr(t, f"bn-{ln['speaker']}", "data-on", on)
        # mouths: text-driven rigs (animate.py) when available, else the old amplitude tracks
        speakers = [sp for sp in m.mouth]
        if self.RV is not None:
            data.update(self.RV.events(set(self.RV.state), t0, t1))
            data["rs"] = sorted(self.RV.state)
            data["rigmode"] = True
        else:
            for sp in speakers:
                for t, st in m.mouth[sp]:
                    if t0 + EPS < t < t1:
                        data["m"].append([round(t - t0, 4), sp, st])
            data["m"].sort(key=lambda r: r[0])
        # captions
        pages = [(i, pg) for i, pg in enumerate(m.pages) if pg["show"] < t1 and pg["hide"] > t0]
        for i, pg in pages:
            if t0 + EPS < pg["show"] < t1:
                vis(f"cp{i}", pg["show"], 1, 0.0)
            if t0 + EPS < pg["hide"] < t1:
                vis(f"cp{i}", pg["hide"], 0, 0.0)
        data["w"] = word_events(pages, t0, t1)
        # ticker
        revealed = sorted(m.pick_reveal.items(), key=lambda kv: (kv[1], kv[0]))  # slide one slot per reveal
        upto = [pn for pn, rt in revealed if rt < t1]
        k0 = sum(1 for _, rt in revealed if rt <= t0)
        x0 = TICK_VIEW - k0 * TICK_W
        ticks = [(rt, idx + 1) for idx, (pn, rt) in enumerate(revealed) if t0 < rt < t1]
        if k0 == 0 and ticks:
            vis("hint", ticks[0][0], 0, 0.25)
        for n, (rt, k) in enumerate(ticks):
            nxt = ticks[n + 1][0] if n + 1 < len(ticks) else rt + 10
            d = min(0.6, nxt - rt - 0.02)
            data["x"].append([round(rt - t0, 4), TICK_VIEW - (k - 1) * TICK_W, TICK_VIEW - k * TICK_W, round(max(0.0, d), 3)])
        # straps (one per layout)
        segs = [s for s in m.segments if s["start"] < t1 and s["end"] > t0]
        for s in segs:
            for where in ("b", "s"):
                if t0 + EPS < s["start"] < t1:
                    vis(f"st{where}-{s['id']}", s["start"], 1, 0.3, -10)
                if t0 + EPS < s["end"] < t1:
                    vis(f"st{where}-{s['id']}", s["end"], 0, 0.2)
        # overlays
        title_in = m.title_hide > 0 and t0 < m.title_hide and m.title_show < t1
        title_entrance = title_in and t0 <= m.title_show
        if title_in:
            if title_entrance:
                ts = m.title_show - t0
                if m.title_show > 0.05:
                    vis("ovl-title", m.title_show, 1, 0.0)
                for k, (el, dt_, dy) in enumerate([("ot1", 0.25, 20), ("ot2", 0.45, 36), ("ot3", 0.75, 0),
                                                    ("ot4", 0.95, 18), ("ot5", 1.15, 18), ("ot6", 1.35, 0)]):
                    data["e"].append([round(ts + dt_, 4), el, 0.7, dy])
            if t0 < m.title_hide < t1:
                vis("ovl-title", m.title_hide, 0, 0.6)
        close_in = m.close_show is not None and m.close_show < t1
        if close_in and t0 < m.close_show:
            vis("ovl-close", m.close_show, 1, 0.8, 0)
        set_off = []
        if m.title_hide > 0:
            set_off.append((m.title_show + (0.3 if m.title_show > 0.05 else 0.0), m.title_hide))
        if m.close_show is not None:
            set_off.append((m.close_show + 0.85, 1e9))
        set_vis0 = not any(lo <= t0 < hi for lo, hi in set_off)
        for lo, hi in set_off:
            if t0 + EPS < lo < t1 and lo > 0:
                vis("set", lo, 0, 0.0)
            if t0 + EPS < hi < t1:
                vis("set", hi, 1, 0.0)
        for el, evs in vis_events.items():
            evs.sort(key=lambda e: e[0])
            for n, (t, on, d, dy) in enumerate(evs):
                nxt = evs[n + 1][0] if n + 1 < len(evs) else t + 99
                d2 = min(d, max(0.0, nxt - t - 0.02))
                data["v"].append([t, el, on, round(d2 if d2 >= 0.08 else 0.0, 3), dy])
        data["v"].sort(key=lambda r: r[0])
        data["a"].sort(key=lambda r: r[0])
        data["c"].sort(key=lambda r: r[0])
        data["p"].sort(key=lambda r: r[0])
        present = {"portraits": focus_set, "tiles": set(self.L.teams), "host": True}
        data.update(motion_data(m, t0, t1, present, self.cfg, rig=self.RV is not None))
        data["origin"] = f"{BIG_X + BIG // 2}px {BIG_Y + int(BIG * 0.45)}px"
        self.used = {}
        for sp, pz in self.pose0.items():
            if pz != "none":
                self.used.setdefault(sp, set()).add(pz)
        for _, sp, pz in data["o"]:
            if pz != "none":
                self.used.setdefault(sp, set()).add(pz)
        slams = [x for x in m.motion.slams if t0 <= x["t"] < t1]
        bursts = [x for x in m.motion.bursts if t0 <= x["t"] < t1]
        # the suit card: the first time each GM talks on the show, what they are wearing (a real Game Day Suit)
        from .suits import suit_info, swatch_url
        suit_html = []
        firsts: dict[str, dict] = {}
        for ln in m.lines:
            if ln["speaker"] != "host" and ln["speaker"] not in firsts and ln.get("_mode") == "speaker":
                firsts[ln["speaker"]] = ln
        for sp, ln in firsts.items():
            win = m.suit_window(sp, 4.2)
            if not win:
                continue
            on, off = win
            if not (on < t1 and off > t0):
                continue
            si = suit_info(self.L, sp)
            if not si:
                continue
            sw = swatch_url(si)
            key = f"sc-{sp}"
            shown = on <= t0 < off
            if t0 + EPS < on < t1:
                data["v"].append([round(on - t0, 4), key, 1, 0.3, 12])
            if t0 + EPS < off < t1:
                data["v"].append([round(off - t0, 4), key, 0, 0.25, 0])
            swd = (f'<div class="sw" style="background-image:url({a}{sw})"></div>' if sw else
                   f'<div class="sw" style="background:{si["color"]}"></div>')
            l2 = " · ".join(x for x in (si["summary"], si["cut"]) if x)
            suit_html.append(f'<div class="suitc" id="{{cid}}-{key}" style="{_vis(shown)}">{swd}'
                             f'<div class="k" style="font-size:{fit(si["possessive"].upper() + " SUIT", 164, "archivo", 16, 10, 1, 900, 100)}px">'
                             f'{esc(si["possessive"].upper())} SUIT</div>'
                             f'<div class="f" style="font-size:{fit(si["fabric"], 164, "archivo", 15, 10, 2, 800, 100)}px">'
                             f'{esc(si["fabric"])}</div><div class="d" style="font-size:{fit(l2, 164, "questrial", 13, 9, 3, 400, 100)}px">'
                             f'{esc(l2)}</div></div>')
        data["v"].sort(key=lambda r: r[0])

        # ---- DOM
        bigs = "".join(self.big(sp, a, sp == focus0, mouth0, bool(cut0) and sp == focus0)
                       for sp in sorted(focus_set, key=lambda x: (x != "host", x)))
        cards = "".join(self.card(k, a, k == card0) for k in cards_set)
        if grader0:
            cards = cards.replace(f'id="{{cid}}-gc-{grader0.replace(":", "-")}" data-on="0"',
                                  f'id="{{cid}}-gc-{grader0.replace(":", "-")}" data-on="1"')
        if dl0:
            cards = cards.replace(f'id="{{cid}}-dl-{dl0}" data-on="0"', f'id="{{cid}}-dl-{dl0}" data-on="1"')
        caps = "".join(self.caption_page(i, pg, pg["show"] <= t0 < pg["hide"], t0) for i, pg in pages)
        straps_b = "".join(self.strap(s, s["start"] <= t0 < s["end"], "b") for s in segs)
        straps_s = "".join(self.strap(s, s["start"] <= t0 < s["end"], "s") for s in segs)
        title_html = self.overlay_title(m.title_show <= t0 < m.title_hide) if title_in else ""
        if title_html and title_entrance:
            for el in ("ot1", "ot2", "ot3", "ot4", "ot5", "ot6"):
                title_html = title_html.replace(f'id="{{cid}}-{el}"', f'id="{{cid}}-{el}" style="opacity:0;visibility:hidden"')
        close_html = self.overlay_close(m.close_show <= t0) if close_in else ""
        studio = (
            f'<div class="studio" id="{cid}-s"><div class="bg"></div>{self.header(a)}'
            f'<div class="set" id="{cid}-set" style="{_vis(set_vis0)}"><div class="cam" id="{cid}-cam">'
            f'{self.show_title()}'
            f'<div class="brd" id="{cid}-brd" style="{_vis(mode0 == "board")}">{straps_b}'
            f'{self.tiles(a, clock0, speak0, mouth0)}{self.host_box(a, speak0 == "host", mouth0)}'
            f'<div class="capbox b panel"></div></div>'
            f'<div class="spk" id="{cid}-spk" style="{_vis(mode0 == "speaker")}">{bigs}{"".join(suit_html)}'
            f'{self.bench(a, clock0, speak0, mouth0)}{straps_s}<div class="capbox s panel"></div></div>'
            f'{cards}{self.ticker(upto, k0 == 0)}</div></div>'
            f'{"".join(burst_html(b, self.L, BIG_X + BIG // 2, BIG_Y + 360) for b in bursts)}'
            f'{"".join(slam_html(x, self.L, 1920, 300) for x in slams)}<div class="flash" id="{{cid}}-flash"></div>'
            f'{title_html}{close_html}<div class="caps">{caps}</div></div>'
        ).replace("{cid}", cid)
        root = (f'<div id="root" data-composition-id="{cid}" data-start="0" data-width="1920" data-height="1080" '
                f'data-duration="{ch.dur_attr}" data-fps="{m.fps}">{studio}</div>')
        js = RUNTIME_JS.replace("__CID__", cid).replace("__RIG_JS__", RIG_JS if self.RV else "").replace("__DATA__", json.dumps(
            {**data, "speakers": speakers, "x0": x0}, separators=(",", ":")))
        css = studio_css.css(a) + (RIG_CSS if self.RV else "")
        if standalone:
            doc = (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n'
                   f'<meta name="viewport" content="width=1920, height=1080" />\n'
                   f'<title>GM-Bench {esc(self.cfg["show_title"])} {cid}</title>\n'
                   f'<script src="{a}vendor/gsap-3.14.2.min.js"></script>\n'
                   f'<style>\nhtml, body {{ margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: #0E1B4D; }}\n'
                   f'{css}</style>\n</head>\n<body>\n{root}\n<script>\n{js}\n</script>\n</body>\n</html>\n')
        else:
            doc = (f'<!doctype html>\n<html lang="en">\n<head><meta charset="UTF-8" /></head>\n<body>\n'
                   f'<template id="{cid}-template">\n<style>\n{css}</style>\n{root}\n<script>\n{js}\n</script>\n'
                   f'</template>\n</body>\n</html>\n')
        stats = {"cid": cid, "t0": t0, "t1": t1, "dur": ch.dur, "segments": ch.segments, "mouth_sets": len(data["m"]),
                 "vis_events": len(data["v"]), "attr_sets": len(data["a"]), "caption_pages": len(pages),
                 "bob_tweens": len(data["k"]), "pose_sets": len(data["o"]), "word_pops": len(data["w"]),
                 "slams": len(slams), "bursts": len(bursts), "html_kb": round(len(doc) / 1024, 1)}
        return doc, stats


# --------------------------------------------------------------------------- shared motion helpers

def _px(vals: list[float], k: float) -> list[float]:
    return [round(v * k, 2) for v in vals]


def _sc(vals: list[float], base: float) -> list[float]:
    return [round(v * base, 4) for v in vals]


def motion_data(m: "ShowModel", t0: float, t1: float, present: dict, cfg: dict, rig: bool = False) -> dict:
    """Window [t0, t1) of the global motion plan as runtime arrays (times relative to t0). rig=True: the characters
    animate themselves -- no loudness bobs, no pose swaps, no camera shake or push; one punch-in on the name slam."""
    mp = m.motion
    mo = cfg.get("motion", {})
    if rig:
        D = {"k": [], "s": [], "z": [], "zp": [], "l": [], "f": [], "o": []}
        for z in ([] if getattr(m, "shots", None) else mp.zooms):  # with a shot plan the name call is a CUT, no zoom
            if z.get("kind") == "reveal" and t0 <= z["t"] < t1:
                D["z"].append([round(z["t"] - t0, 4), 1.0, round(1 + (z["peak"] - 1) * 0.7, 4), 0.6])
        for x in mp.slams:
            if t0 <= x["t"] < t1:
                D["l"].append([round(x["t"] - t0, 4), x["key"]])
        for x in mp.bursts:
            if t0 <= x["t"] < t1:
                D["f"].append([round(x["t"] - t0, 4), x["key"]])
        return D
    bob, tbob = float(mo.get("bob_px", 16)), float(mo.get("tile_bob_px", 5))
    big = float(mo.get("big_bob_px", bob * 1.35))
    D: dict = {"k": [], "s": [], "z": [], "zp": [], "l": [], "f": [], "o": []}
    for sp, items in mp.bobs.items():
        for b in items:
            if not (t0 <= b["t"] < t1):
                continue
            rt = round(b["t"] - t0, 4)
            if sp == "host" and present.get("host"):
                D["k"].append([rt, "hb", b["dur"], _px(b["y"], 5), _px(b["r"], 1.6), _sc(b["sx"], 1.06),
                               _sc(b["sy"], 1.06)])
            if sp in present.get("tiles", ()):
                D["k"].append([rt, f"tb-{sp}", b["dur"], _px(b["y"], tbob), _px(b["r"], 3.0), _sc(b["sx"], 1.08),
                               _sc(b["sy"], 1.08)])
            if sp in present.get("portraits", ()):
                D["k"].append([rt, f"bob-{sp}", b["dur"], _px(b["y"], big), _px(b["r"], 1.6), _sc(b["sx"], 1.06),
                               _sc(b["sy"], 1.06)])
    for x in mp.shakes:
        if t0 <= x["t"] < t1:
            D["s"].append([round(x["t"] - t0, 4), x["dur"], x["x"], x["y"]])
    for z in mp.zooms:
        if t0 <= z["t"] < t1:
            D["z"].append([round(z["t"] - t0, 4), z["from"], round(z["peak"], 4), z["hold"]])
        pz = z.get("push")
        if pz and t0 <= pz["t"] and pz["t"] + pz["dur"] < t1:
            D["zp"].append([round(pz["t"] - t0, 4), pz["dur"], pz["to"]])
    for x in mp.slams:
        if t0 <= x["t"] < t1:
            D["l"].append([round(x["t"] - t0, 4), x["key"]])
    for x in mp.bursts:
        if t0 <= x["t"] < t1:
            D["f"].append([round(x["t"] - t0, 4), x["key"]])
    speakers = set(present.get("tiles", ())) | set(present.get("portraits", ())) | ({"host"} if present.get("host") else set())
    for sp, seq in mp.poses.items():
        if sp not in speakers:
            continue
        for t, pose in seq:
            if t0 < t < t1:
                D["o"].append([round(t - t0, 4), sp, pose])
    for k in D:
        D[k].sort(key=lambda r: r[0])
    return D


def word_events(pages: list[tuple[int, dict]], t0: float, t1: float) -> list:
    out = []
    for i, pg in pages:
        k = 0
        for row in pg["rows"]:
            for w in row:
                if t0 < w["start"] < t1 and w["start"] < pg["hide"]:
                    out.append([round(w["start"] - t0, 4), f"w{i}_{k}", RED, 1])
                if t0 < w["end"] < t1 and w["end"] < pg["hide"]:
                    out.append([round(w["end"] - t0, 4), f"w{i}_{k}", OFF, 0])
                k += 1
    out.sort(key=lambda r: r[0])
    return out


def slam_html(x: dict, L, width: int, top: int) -> str:
    t = L.teams.get(x["team"])
    size = fit(x["text"], width - 160, "archivo", 250, 96, 1, 900, 125)
    color = "#FFC83D" if x.get("reach") else OFF
    return (f'<div class="slam" id="{{cid}}-{x["key"]}" data-layout-allow-overlap style="{team_vars(t) if t else ""};'
            f'top:{top}px;width:{width}px;opacity:0;visibility:hidden"><div class="st" data-layout-allow-overlap '
            f'style="font-size:{size}px;color:{color}">{esc(x["text"])}</div>'
            f'<div class="ss" data-layout-allow-overlap>{esc(x["sub"])}</div></div>')


def burst_html(b: dict, L, x0: int, y0: int) -> str:
    t = L.teams.get(b.get("team") or "")
    cols = [t.colors[0], t.colors[1]] if t else []
    cols = (cols + CONFETTI)[:4] if cols else CONFETTI
    parts = []
    for p in b["parts"]:
        c = cols[p["c"] % len(cols)]
        w = 10 + 6 * (p["c"] % 2)
        parts.append(f'<i style="background:{c};width:{w}px;height:{w}px;opacity:0;visibility:hidden" '
                     f'data-dx="{p["dx"]}" data-dy="{p["dy"]}" data-f="{p["fall"]}" data-r="{p["r"]}" '
                     f'data-s="{p["s"]}" data-d="{p["d"]}"></i>')
    return f'<div class="burst" id="{{cid}}-{b["key"]}" style="left:{x0}px;top:{y0}px">{"".join(parts)}</div>'


def _vis(on: bool) -> str:
    return "opacity:1;visibility:inherit" if on else "opacity:0;visibility:hidden"


def _state_at(ev: list[tuple[float, str]], t: float) -> str:
    s = "closed"
    for tt, st in ev:
        if tt <= t:
            s = st
        else:
            break
    return s


RUNTIME_JS = """(function () {
  var CID = "__CID__";
  var D = __DATA__;
  var S = document.getElementById(CID + "-s");
  var el = function (k) { return document.getElementById(CID + "-" + k); };
  var tl = gsap.timeline({ paused: true });
  var AV = {};
  D.speakers.forEach(function (sp) { AV[sp] = S.querySelectorAll(".av-" + sp); });
  gsap.set(el("strip"), { x: D.x0 });
  D.m.forEach(function (r) { if (AV[r[1]].length) tl.set(AV[r[1]], { attr: { "data-m": r[2] } }, r[0]); });
  D.a.forEach(function (r) { var o = {}; o[r[2]] = r[3]; tl.set(el(r[1]), { attr: o }, r[0]); });
  D.v.forEach(function (r) {
    var e = el(r[1]);
    if (r[3] <= 0) { tl.set(e, { autoAlpha: r[2] }, r[0]); return; }
    if (r[2]) tl.fromTo(e, { autoAlpha: 0, y: r[4] }, { autoAlpha: 1, y: 0, duration: r[3], ease: "power2.out", immediateRender: false }, r[0]);
    else tl.fromTo(e, { autoAlpha: 1 }, { autoAlpha: 0, duration: r[3], ease: "power1.in", immediateRender: false }, r[0]);
  });
  D.c.forEach(function (r) { tl.set(el(r[1]), { color: r[2] }, r[0]); });
  D.x.forEach(function (r) {
    if (r[3] > 0) tl.fromTo(el("strip"), { x: r[1] }, { x: r[2], duration: r[3], ease: "power2.inOut", immediateRender: false }, r[0]);
    else tl.set(el("strip"), { x: r[2] }, r[0]);
  });
  D.e.forEach(function (r) {
    tl.fromTo(el(r[1]), { autoAlpha: 0, y: r[3] }, { autoAlpha: 1, y: 0, duration: r[2], ease: "power3.out", immediateRender: false }, r[0]);
  });
  // report-card stamps: already down at chunk start -> just tilted; landing inside the chunk -> punched in
  (D.ps || []).forEach(function (k) { var e = el(k); if (e) gsap.set(e, { rotation: -8 }); });
  (D.p || []).forEach(function (r) {
    var e = el(r[1]); if (!e) return;
    var rot = r.length > 2 ? r[2] : -8;
    tl.fromTo(e, { autoAlpha: 0, scale: 2.6, rotation: rot - 16 }, { autoAlpha: 1, scale: 1, rotation: rot, duration: 0.2, ease: "power4.in", immediateRender: false }, r[0]);
  });
  // --- motion: poses, word pops, head-bob / squash, camera shake + zoom punches, name slams, confetti
  (D.o || []).forEach(function (r) { if (AV[r[1]] && AV[r[1]].length) tl.set(AV[r[1]], { attr: { "data-p": r[2] } }, r[0]); });
  (D.w || []).forEach(function (r) {
    var w = el(r[1]); if (!w) return;
    tl.set(w, { color: r[2] }, r[0]);
    if (r[3]) tl.fromTo(w, { y: -9, scale: 1.05 }, { y: 0, scale: 1, duration: 0.18, ease: "back.out(3)", immediateRender: false }, r[0]);
  });
  var bobs = S.querySelectorAll(".bob,.tbob,.hbob");
  if (!D.rigmode) bobs.forEach(function (b) { gsap.set(b, { scaleX: b.classList.contains("tbob") ? 1.08 : 1.06, scaleY: b.classList.contains("tbob") ? 1.08 : 1.06 }); });
  (D.k || []).forEach(function (r) {
    var b = el(r[1]); if (!b) return;
    tl.to(b, { keyframes: { y: r[3], rotation: r[4], scaleX: r[5], scaleY: r[6], easeEach: "sine.inOut" }, duration: r[2], ease: "none" }, r[0]);
  });
  var cam = el("cam");
  if (cam) {
    gsap.set(cam, { transformOrigin: (D.origin || "484px 496px") });
    (D.s || []).forEach(function (r) { tl.to(cam, { keyframes: { x: r[2], y: r[3] }, duration: r[1], ease: "none" }, r[0]); });
    (D.zp || []).forEach(function (r) { tl.fromTo(cam, { scale: 1 }, { scale: r[2], duration: r[1], ease: "sine.inOut", immediateRender: false }, r[0]); });
    (D.z || []).forEach(function (r) {
      tl.fromTo(cam, { scale: r[1] }, { scale: r[2], duration: 0.12, ease: "power4.out", immediateRender: false }, r[0]);
      tl.fromTo(cam, { scale: r[2] }, { scale: 1, duration: 0.65, ease: "power2.inOut", immediateRender: false }, r[0] + 0.12 + r[3]);
    });
  }
  (D.kp || []).forEach(function (r) {  // kicker pops: the punch words, big, on the punch
    var e = el(r[1]); if (!e) return;
    tl.fromTo(e, { autoAlpha: 0, scale: 1.55, rotation: -9 }, { autoAlpha: 1, scale: 1, rotation: -4, duration: 0.14, ease: "back.out(2.2)", immediateRender: false }, r[0]);
    tl.fromTo(e, { autoAlpha: 1 }, { autoAlpha: 0, duration: 0.18, ease: "power1.in", immediateRender: false }, r[2]);
  });
  var flash = el("flash");
  (D.l || []).forEach(function (r) {
    var e = el(r[1]); if (!e) return;
    tl.fromTo(e, { autoAlpha: 0, scale: 2.6, rotation: -9 }, { autoAlpha: 1, scale: 1, rotation: -3, duration: 0.16, ease: "power4.in", immediateRender: false }, r[0]);
    tl.fromTo(e, { scale: 1 }, { scale: 1.07, duration: 1.05, ease: "none", immediateRender: false }, r[0] + 0.16);
    tl.fromTo(e, { autoAlpha: 1 }, { autoAlpha: 0, duration: 0.25, ease: "power2.in", immediateRender: false }, r[0] + 1.21);
    if (flash) tl.fromTo(flash, { autoAlpha: D.rigmode ? 0.4 : 0.85 }, { autoAlpha: 0, duration: 0.2, ease: "power2.out", immediateRender: false }, r[0] + 0.15);
  });
  (D.f || []).forEach(function (r) {
    var box = el(r[1]); if (!box) return;
    Array.prototype.forEach.call(box.children, function (p) {
      var dx = +p.dataset.dx, dy = +p.dataset.dy, f = +p.dataset.f, rot = +p.dataset.r, s = +p.dataset.s, d = +p.dataset.d;
      tl.fromTo(p, { autoAlpha: 1, x: 0, y: 0, rotation: 0, scale: s }, { x: dx, y: dy, rotation: rot * 0.5, duration: 0.55, ease: "power3.out", immediateRender: false }, r[0] + d);
      tl.fromTo(p, { x: dx, y: dy }, { x: dx * 1.25, y: dy + f, rotation: rot, duration: 0.95, ease: "power1.in", immediateRender: false }, r[0] + d + 0.55);
      tl.fromTo(p, { autoAlpha: 1 }, { autoAlpha: 0, duration: 0.3, immediateRender: false }, r[0] + d + 1.2);
    });
  });
__RIG_JS__
  window.__timelines["__CID__"] = tl;
})();"""


# --------------------------------------------------------------------------- project writer

def build_rigview(model: ShowModel, cfg: dict, side: str = "right"):
    """Text-driven character animation (animate.py + rigs); None when disabled (animation.rigs: false)."""
    if not (cfg.get("animation") or {}).get("rigs", True):
        return None
    from .animate import Plan
    from .rig import build_sheets, load_rig
    from .rigview import RigView
    rigs = {sp: r for sp, fset in model.fs.items() for r in [load_rig(sp, fset)] if r}
    plan = getattr(model, "plan", None) or Plan(model.cues, model.L)
    return RigView(rigs, {sp: build_sheets(r) for sp, r in rigs.items()}, plan, addressee_side=side)


def write_project(model: ShowModel, cfg: dict, minutes: float | None, mix_path: Path | None, log=print) -> dict:
    assets = Assets(model.fs)
    b = Builder(model, assets, cfg, build_rigview(model, cfg))
    limit = minutes * 60.0 if minutes else None
    chunks = model.chunks(limit)
    rdir, cdir = SHOW / "render", SHOW / "compositions"
    for d in (rdir, cdir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    stats = []
    for ch in chunks:
        doc, st = b.chunk_html(ch, standalone=True)
        (rdir / f"{ch.cid}.html").write_text(doc)
        doc2, _ = b.chunk_html(ch, standalone=False)
        (cdir / f"{ch.cid}.html").write_text(doc2)
        stats.append(st)
    total = chunks[-1].t1 if chunks else 0.0
    audio_tag = ""
    if mix_path and mix_path.exists():
        prev = SHOW_GEN / "mix-preview.m4a"
        from . import audio as au
        au.run([au.FFMPEG, "-v", "error", "-y", "-i", str(mix_path), "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "128k",
                str(prev)])
        audio_tag = (f'\n  <audio id="show-audio" src="assets/gen/mix-preview.m4a" data-start="0" '
                     f'data-duration="{total}" data-track-index="10" data-volume="1"></audio>')
    slots = "\n".join(
        f'  <div id="el-{c.cid}" data-composition-id="{c.cid}" data-composition-src="compositions/{c.cid}.html" '
        f'data-start="{c.t0:.6f}" data-duration="{c.dur_attr}" data-track-index="1" data-width="1920" data-height="1080"></div>'
        for c in chunks)
    index = (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n'
             f'<meta name="viewport" content="width=1920, height=1080" />\n<title>GM-Bench Draft Night (preview)</title>\n'
             f'<script src="assets/vendor/gsap-3.14.2.min.js"></script>\n<style>\n'
             f'html, body {{ margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: #0E1B4D; }}\n'
             f'#root {{ position: relative; width: 1920px; height: 1080px; overflow: hidden; background: #0E1B4D; }}\n'
             f'</style>\n</head>\n<body>\n<div id="root" data-composition-id="main" data-start="0" data-width="1920" '
             f'data-height="1080" data-duration="{round(total, 4)}" data-fps="{model.fps}">\n{slots}{audio_tag}\n</div>\n'
             f'<script>\n  window.__timelines["main"] = gsap.timeline({{ paused: true }});\n</script>\n</body>\n</html>\n')
    (SHOW / "index.html").write_text(index)
    manifest = {"schema": "gmbench.compose/1", "run": model.L.run, "fps": model.fps, "duration_s": round(total, 4),
                "minutes_limit": minutes, "chunks": stats}
    log(f"compose: {len(chunks)} chunk(s), {total:.1f}s, "
        f"{sum(s['mouth_sets'] for s in stats)} mouth sets, {sum(s['caption_pages'] for s in stats)} caption pages "
        f"-> {rel(rdir)}")
    return manifest
