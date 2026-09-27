"""Deterministic motion plan (global show time) from cues + events + envelopes.

Everything here becomes GSAP keyframe tweens / sets on a paused timeline, so it is seekable and
frame-exact: head-bob + squash-and-stretch from each speaker's amplitude envelope, screen shake on
[shouting], zoom punch-ins and name slams on pick reveals, pixel confetti on big picks, and pose swaps
(hype on calls/shouts, point on chirps, shock for the roasted or sniped GM, celebrate after a pick).
"""

from __future__ import annotations

import hashlib
import math
import re

import numpy as np

POSES = ("hype", "point", "shock", "celebrate")
PRIORITY = {"shock": 3, "celebrate": 2, "hype": 1, "point": 1}


def _rng(key: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def slam_text(player: str) -> str:
    words = [w for w in re.split(r"\s+", player.strip()) if w]
    if len(words) >= 2 and not any(ch.isdigit() for ch in player):
        return words[-1].upper() + "!!!"
    return player.upper() + "!!!"


class MotionPlan:
    def __init__(self, cues: dict, env: dict, league, cfg: dict):
        self.cfg = cfg
        mo = cfg.get("motion", {})
        self.fps = int(cues["fps"])
        self.lines = {ln["id"]: ln for ln in cues["lines"]}
        self.cue_lines = cues["lines"]
        self.events = cues.get("events") or []
        self.picks = {p.pick_no: p for p in league.picks}
        self.league = league
        self.bob_px = float(mo.get("bob_px", 16))
        self.tile_bob_px = float(mo.get("tile_bob_px", 5))
        self.squash = float(mo.get("squash", 0.06))
        self.shake_px = float(mo.get("shake_px", 12))
        self.zoom = float(mo.get("zoom", 1.07))
        self.n_confetti = int(mo.get("confetti", 44))
        self.env = env.get("speakers", {})
        self.hype_lines = {e["line"] for e in self.events if e["type"] in ("shout",) or
                           (e["type"] == "call" and e.get("hype"))}
        self.bobs = self._bobs()
        self.shakes = self._shakes()
        self.zooms = self._zooms()
        self.slams = self._slams()
        self.bursts = self._bursts()
        self.poses = self._poses()

    # -- envelope-driven body motion
    def _bobs(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        step = 3  # sample every 3 frames (10 Hz) and let easeEach interpolate
        for sp, segs in self.env.items():
            for seg in segs:
                ln = self.lines.get(seg["line"])
                if not ln:
                    continue
                v = np.asarray(seg["v"], dtype=np.float32)
                if len(v) < 4:
                    continue
                # smooth + peak-normalise so quiet and loud voices both move
                k = np.convolve(v, np.ones(3, dtype=np.float32) / 3, mode="same")
                k = k / (np.percentile(k, 95) + 1e-3)
                k = np.clip(k, 0, 1.25)
                hype = 1.5 if ln["id"] in self.hype_lines else 1.0
                phase = int(hashlib.sha1(sp.encode()).hexdigest()[:4], 16) % 7
                ys, rs, sxs, sys_ = [0.0], [0.0], [1.0], [1.0]
                idx = list(range(step, len(k) - 1, step))
                for j, i in enumerate(idx):
                    e = float(k[i])
                    t = i / self.fps
                    ys.append(round(-e ** 0.8 * hype, 3))
                    rs.append(round(math.sin(2 * math.pi * 1.6 * t + phase) * e * hype, 3))
                    peak = max(0.0, e - 0.72) / 0.5
                    sys_.append(round(1 + self.squash * peak * hype, 4))
                    sxs.append(round(1 - self.squash * 0.6 * peak * hype, 4))
                ys.append(0.0), rs.append(0.0), sxs.append(1.0), sys_.append(1.0)
                t0 = seg["f0"] / self.fps
                dur = (len(v)) / self.fps
                out.setdefault(sp, []).append({"t": round(t0, 4), "dur": round(dur, 4), "y": ys, "r": rs, "sx": sxs,
                                               "sy": sys_, "line": ln["id"]})
        return out

    def _shakes(self) -> list[dict]:
        out: list[dict] = []
        cand = [(e["t"], 0.7) for e in self.events if e["type"] == "shout"]
        cand += [(e["t"], 1.0 if e.get("big") else 0.6) for e in self.events if e["type"] == "reveal"]
        cand += [(e["t"], 0.8) for e in self.events if e["type"] == "title"]
        cand += [(e["t"], 0.9 if e.get("top") else 0.55) for e in self.events if e["type"] == "stamp"]
        last = -9.0
        for t, amp in sorted(cand):
            if t - last < 0.6:
                continue
            rng = _rng(f"shake:{t:.3f}")
            n = 9
            decay = [1 - i / (n - 1) for i in range(n)]
            xs = [0.0] + [round(float(rng.uniform(-1, 1)) * self.shake_px * amp * d, 2) for d in decay[1:-1]] + [0.0]
            ys = [0.0] + [round(float(rng.uniform(-1, 1)) * self.shake_px * 0.7 * amp * d, 2) for d in decay[1:-1]] + [0.0]
            out.append({"t": round(t, 4), "dur": 0.42, "x": xs, "y": ys})
            last = t
        return out

    def _zooms(self) -> list[dict]:
        out = []
        clocks = {e.get("pick_no"): e for e in self.events if e["type"] == "clock"}
        for e in self.events:
            if e["type"] != "reveal":
                continue
            c = clocks.get(e["pick_no"])
            push = None
            if c and 0.5 < e["t"] - c["t"] < 12:
                push = {"t": round(c["t"], 4), "dur": round(e["t"] - c["t"] - 0.06, 4), "to": 1.025}
            out.append({"t": round(e["t"] - 0.03, 4), "peak": self.zoom if e.get("big") else 1 + (self.zoom - 1) * 0.6,
                        "hold": 0.9, "from": 1.025 if push else 1.0, "push": push, "kind": "reveal"})
        for e in self.events:
            if e["type"] == "stamp":
                out.append({"t": round(e["t"] - 0.03, 4), "peak": self.zoom if e.get("top") else 1 + (self.zoom - 1) * 0.5,
                            "hold": 0.7, "from": 1.0, "push": None, "kind": "stamp"})
            elif e["type"] == "cutaway":  # snap onto the roasted GM's face
                out.append({"t": round(e["t"], 4), "peak": 1 + (self.zoom - 1) * 0.7, "hold": max(0.2, e["until"] - e["t"] - 0.8),
                            "from": 1.0, "push": None, "kind": "cutaway"})
        out.sort(key=lambda z: z["t"])
        return out

    def _slams(self) -> list[dict]:
        out = []
        for e in self.events:
            if e["type"] != "reveal":
                continue
            p = self.picks.get(e["pick_no"])
            if not p:
                continue
            from .nhl import pos_display
            flag = {"reach": " · REACH?!", "steal": " · STEAL!"}.get(e.get("flag") or "", "")
            out.append({"t": round(float(e.get("slam_t") or e["t"]), 4), "key": f"slam-{p.pick_no}",
                        "text": slam_text(p.player_name),
                        "sub": f"{pos_display(p.position)} · {p.nhl_team} · PICK {p.pick_no}{flag}", "team": p.team,
                        "pick_no": p.pick_no, "reach": bool(e.get("flag"))})
        for e in self.events:  # report card: the letter slams for the bottom and the top of the class
            if e["type"] == "stamp" and (e.get("top") or e.get("bottom")):
                t = self.league.teams.get(e.get("team"))
                who = (t.franchise_name if t and t.has_persona else (t.display if t else "")).upper()
                out.append({"t": round(e["t"], 4), "key": f"slam-rc-{e['team']}", "text": e["letter"] + "!!!",
                            "sub": ("TOP OF THE CLASS · " if e.get("top") else "BOTTOM OF THE CLASS · ") + who,
                            "team": e.get("team"), "pick_no": None, "reach": bool(e.get("bad"))})
        out.sort(key=lambda x: x["t"])
        return out

    def _bursts(self) -> list[dict]:
        out = []
        for e in self.events:
            if e["type"] == "reveal" and e.get("big"):
                p = self.picks.get(e["pick_no"])
                rng = _rng(f"burst:{e['pick_no']}")
                parts = []
                for i in range(self.n_confetti):
                    ang = float(rng.uniform(-math.pi, 0))  # upward half-circle
                    spd = float(rng.uniform(260, 760))
                    parts.append({"dx": round(math.cos(ang) * spd, 1), "dy": round(math.sin(ang) * spd * 0.8, 1),
                                  "fall": round(float(rng.uniform(220, 520)), 1), "r": round(float(rng.uniform(-540, 540)), 1),
                                  "s": round(float(rng.uniform(0.7, 1.4)), 2), "c": int(rng.integers(0, 4)),
                                  "d": round(float(rng.uniform(0, 0.12)), 3)})
                out.append({"t": round(e["t"] + 0.02, 4), "key": f"burst-{e['pick_no']}", "team": p.team if p else None,
                            "parts": parts})
        for e in self.events:
            if e["type"] == "stamp" and e.get("top"):
                rng = _rng(f"burst:rc:{e['team']}")
                parts = [{"dx": round(math.cos(a) * sp_, 1), "dy": round(math.sin(a) * sp_ * 0.8, 1),
                          "fall": round(float(rng.uniform(220, 520)), 1), "r": round(float(rng.uniform(-540, 540)), 1),
                          "s": round(float(rng.uniform(0.7, 1.4)), 2), "c": int(rng.integers(0, 4)),
                          "d": round(float(rng.uniform(0, 0.12)), 3)}
                         for a, sp_ in ((float(rng.uniform(-math.pi, 0)), float(rng.uniform(260, 760)))
                                        for _ in range(self.n_confetti))]
                out.append({"t": round(e["t"] + 0.02, 4), "key": f"burst-rc-{e['team']}", "team": e.get("team"),
                            "parts": parts})
        out.sort(key=lambda b: b["t"])
        return out

    # -- poses
    HOST_KIND_POSE = {"host_clock": "point", "host_intro": "point", "host_question": "point", "host_delusion": "point",
                      "host_grade": "point", "host_pick": "hype", "host_speed": "hype", "host_grade_blitz": "hype",
                      "host_close": "celebrate", "host_round_end": "celebrate", "auto_note": "shock"}

    def _host_poses(self, add) -> None:
        """The Commissioner's poses are deliberate: point on '...is ON THE CLOCK' (and introductions), hype on every
        pick-name call, shock on reaches and steals (the record-scratch moments), celebrate on round ends and the
        show open."""
        from .sfx import phrase_time
        for ln in self.cue_lines:
            if ln["speaker"] != "host":
                continue
            k, a, b = ln["kind"], ln["start"], ln["end"]
            pose = self.HOST_KIND_POSE.get(k)
            if ln["id"] in ("open-01", "hl-intro"):
                pose = "celebrate"
            elif ln["id"] == "open-02":  # "...I'm the Commissioner... LET'S DROP THE PUCK!"
                add("host", phrase_time(ln, "drop the puck", 0.8) - 0.35, b + 0.9, "celebrate")
                continue
            elif k == "host_round" or (pose is None and "[shouting]" in (ln.get("tts_text") or "")):
                pose = "hype"
            if pose:
                tail = {"celebrate": 0.9, "hype": 0.5, "point": 0.25, "shock": 0.6}[pose]
                add("host", a, b + tail, pose)
        for e in self.events:
            if e["type"] == "reveal" and e.get("flag"):
                add("host", e["t"] - 0.05, e["t"] + 2.2, "shock")
            elif e["type"] == "stamp":
                pose = "celebrate" if (e.get("good") or e.get("top")) else ("shock" if e.get("bad") else None)
                if pose:
                    add("host", e["t"] - 0.05, e["t"] + 2.0, pose)

    def _poses(self) -> dict[str, list[tuple[float, str]]]:
        iv: dict[str, list[tuple[float, float, str]]] = {}

        def add(sp, a, b, pose):
            if sp and b > a:
                iv.setdefault(sp, []).append((round(a, 4), round(b, 4), pose))

        teams = set(self.league.teams)
        self._host_poses(add)
        reacts = {e["line"]: e for e in self.events if e["type"] == "react"}
        for e in self.events:
            ty = e["type"]
            if ty == "call":  # two-beat call: point at the previous GM through the reaction, then hype the pick
                r = reacts.get(e["line"])
                if r:
                    add(e["speaker"], e["t"], r["until"], "point")
                add(e["speaker"], r["until"] if r else e["t"], e["until"], "hype")
            elif ty == "react" and e.get("team") in teams:
                add(e["team"], e["t"] + 0.25, e["until"] + 0.5, e["pose"])
            elif ty == "shout" and e["speaker"] != "host":
                add(e["speaker"], e["t"], e["until"], "hype")
            elif ty == "roast":
                add(e["speaker"], e["t"], e["until"], "point")
                tgt = e.get("target")
                if tgt in teams and tgt != e["speaker"] and not e.get("cutaway"):
                    add(tgt, e["t"] + 0.35, min(e["until"], e["t"] + 2.4), "shock")
            elif ty == "snipe":
                add(e["team"], e["t"], e["until"], "shock")
            elif ty == "celebrate":
                add(e["team"], e["t"], e["until"], "celebrate")
            elif ty == "cutaway":
                add(e["team"], e["t"] - 0.05, e["until"] + 0.15, e["pose"])
            elif ty == "stamp" and e.get("team") in teams:
                pose = "celebrate" if (e.get("good") or e.get("top")) else ("shock" if e.get("bad") else None)
                if pose:
                    add(e["team"], e["t"], e["t"] + 2.2, pose)
        out: dict[str, list[tuple[float, str]]] = {}
        for sp, ivs in iv.items():
            cuts = sorted({x for a, b, _ in ivs for x in (a, b)})
            seq: list[tuple[float, str]] = []
            cur = "none"
            for t in cuts:
                active = [(PRIORITY[p], a, p) for a, b, p in ivs if a <= t < b]
                pose = max(active)[2] if active else "none"
                if pose != cur:
                    seq.append((t, pose))
                    cur = pose
            out[sp] = seq
        return out

    def pose_at(self, sp: str, t: float) -> str:
        cur = "none"
        for tt, p in self.poses.get(sp, []):
            if tt <= t:
                cur = p
            else:
                break
        return cur
