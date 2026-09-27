"""Rig instances in the page + their frame-exact runtime events.

Each on-screen instance of a character (the big speaker, the bench chip, the reaction cam, the PiP ...) is the same
four-layer rig at the rig's native size inside a CSS-scaled, pixelated container:

    .rgv (display box, overflow hidden)
      .rgc (native canvas; transform: scale / crop -- static, never animated)
        .rb  body sprite sheet     background-position = the current gesture frame
        .rf  face group            left/top = head box + the frame's head_offset
          .re  eye/brow patch sheet
          .rm  mouth viseme sheet
        .ro  occluder sheet        (hands/cans over the face)

All instances of a character share one timeline of GSAP sets (by class), so the face on the bench chip lip-syncs with
the big portrait. Nothing moves the camera: motion comes from the characters.
"""

from __future__ import annotations

from .animate import Plan
from .rig import Rig, Sheets, cell_xy, look_directions

# Rig sets are frame-aligned (f / fps) and written rounded to 4 decimals: 1091/30 = 36.36666... becomes 36.3667, just
# AFTER frame 1091's timestamp, so that set would land a frame late (measured: half the mouth changes). Every set goes
# 2 ms early -- far under half a frame, so it can never land a frame early either.
SEEK_EPS = 0.002
EYE_MAP = {"half_lid": "half", "open": "open", "blink": "blink", "happy": "happy", "angry": "angry",
           "surprised": "surprised", "skeptical": "skeptical"}
MOOD_OF_EYES = {"happy": "happy", "angry": "angry", "surprised": "surprised"}


def _expand(rle: list[list], total: int, default: str) -> list[str]:
    out = [default] * total
    for k, (f, v) in enumerate(rle):
        f1 = rle[k + 1][0] if k + 1 < len(rle) else total
        for i in range(max(0, int(f)), min(total, int(f1))):
            out[i] = v
    return out


class RigView:
    def __init__(self, rigs: dict[str, Rig], sheets: dict[str, Sheets], plan: Plan, addressee_side: str = "right"):
        self.rigs, self.sheets, self.plan = rigs, sheets, plan
        self.fps = plan.fps
        self.total = plan.total
        self.side = addressee_side
        # the glance toward the reaction cam uses whichever look sprite REALLY looks that way (measured), else none
        want = 1 if addressee_side == "right" else -1
        self.glance: dict[str, str] = {}
        for sp, rig in rigs.items():
            dirs = look_directions(rig) if rig.kind != "mock" else {}
            hit = [st for st in ("look_right", "look_left") if dirs.get(st) == want]
            self.glance[sp] = hit[0] if hit else ("open" if dirs else ("look_right" if want > 0 else "look_left"))
        self.state: dict[str, dict[str, list]] = {}
        self.cu: dict[str, list[tuple[float, float]]] = {}  # close-up windows per character (plan.shot_list)
        self.instances: list[dict] = []  # every instance written into a page (QA: whole-number scaling)
        for sp in rigs:
            self.state[sp] = self._frames(sp)
            wins = plan.cu_windows(sp) if hasattr(plan, "cu_windows") else []
            if wins:  # the close-up instance: same face timeline, the body held still (a 1 px bob is 6-8 px there)
                self.cu[sp] = wins
                self.state[f"{sp}-cu"] = self._frames(sp, still=True)

    # -------------------------------------------------------------- per-frame state
    def _body_frames(self, sp: str) -> list[tuple[str, int]]:
        rig = self.rigs[sp]
        out = [("idle_breathe", 0)] * self.total
        talk = self.plan.talking.get(sp, [])
        for a, b, g in self.plan.body_track(sp):
            clip = rig.clip(g)
            if clip.frames and all(f["face"] == "baked" for f in clip.frames):
                # a baked frame freezes the face: while this character talks, keep it to a quick accent
                if any(x < b and y > a for x, y in talk) and b - a > 0.45:
                    b = a + 0.45
            n = len(clip.frames)
            fa, fb = int(round(a * self.fps)), min(self.total, int(round(b * self.fps)))
            dur = max(1, fb - fa) / self.fps
            hold = clip.hold if clip.hold is not None else n - 1
            hold = max(0, min(n - 1, int(hold)))
            ph = clip.phases if (clip.phases and not clip.loop and n > 1) else None
            if ph:  # explicit in / hold (cycled at hold_fps: a held pose never freezes) / out
                t_in, t_out = len(ph["in"]) / clip.fps, len(ph["out"]) / clip.fps
            else:
                t_in = (hold + 1) / clip.fps
                n_out = hold if clip.exit == "reverse" else ((n - 1 - hold) if clip.exit == "forward" else 0)
                t_out = n_out / clip.fps
            if not clip.loop and t_in + t_out > dur and n > 1:  # short slot: compress the in/out to fit
                k = dur / (t_in + t_out)
                t_in, t_out = t_in * k, t_out * k
            for f in range(fa, fb):
                dt = (f - fa) / self.fps
                if clip.loop or n == 1:
                    idx = int(dt * clip.fps) % n
                elif ph:
                    if dt < t_in and ph["in"]:
                        idx = ph["in"][min(len(ph["in"]) - 1, int(dt / max(t_in, 1e-6) * len(ph["in"])))]
                    elif dt >= dur - t_out and ph["out"]:
                        k = int((dt - (dur - t_out)) / max(t_out, 1e-6) * len(ph["out"]))
                        idx = ph["out"][min(len(ph["out"]) - 1, k)]
                    else:
                        hf = clip.hold_fps or 0.0
                        idx = ph["hold"][int((dt - t_in) * hf) % len(ph["hold"])] if hf else ph["hold"][0]
                elif dt < t_in:
                    idx = min(hold, int(dt / max(t_in, 1e-6) * (hold + 1)))
                elif dt < dur - t_out:
                    idx = hold
                else:
                    k = int((dt - (dur - t_out)) / max(t_out, 1e-6) * n_out) if n_out else 0
                    idx = max(0, hold - 1 - k) if clip.exit == "reverse" else min(n - 1, hold + 1 + k)
                out[f] = (clip.name, idx)
        return out

    def _frames(self, sp: str, still: bool = False) -> dict[str, list]:
        rig, sh = self.rigs[sp], self.sheets[sp]
        body = [("idle_breathe", 0)] * self.total if still else self._body_frames(sp)
        exp = self.plan.export_speaker(sp) if hasattr(self.plan, "export_speaker") else None
        mouth = _expand((exp or {}).get("mouth") or self.plan.mouth_track(sp), self.total, "rest")
        eyes = _expand((exp or {}).get("eyes") or self.plan.eye_track(sp), self.total, "open")
        rb, rm, re_ = [], [], []
        W, H = sh.native
        x0, y0 = sh.head_box[0], sh.head_box[1]
        for f in range(self.total):
            clip, idx = body[f]
            fr = rig.gestures[clip].frames[idx]
            bx, by = cell_xy(sh.body_index[(clip, idx)], sh.cols["body"], (W, H))
            ox, oy = cell_xy(sh.occ_index.get((clip, idx), 0), sh.cols["occ"], (W, H))
            hx, hy = fr["head_offset"]
            baked = fr["face"] == "baked"
            st = eyes[f]
            if st == "look_addressee":
                st = self.glance.get(sp, "open")
            st = rig.eye(EYE_MAP.get(st, st))
            mood = MOOD_OF_EYES.get(st, "neutral")
            mood, vis = rig.viseme(mood, mouth[f])
            mc = 0 if baked else sh.mouth_index.get((mood, vis), 0)
            ec = 0 if baked else sh.eye_index.get(st, 0)
            mx, my = cell_xy(mc, sh.cols["mouth"], sh.patch_size)
            ex, ey = cell_xy(ec, sh.cols["eyes"], sh.patch_size)
            rb.append((bx, by, ox, oy, x0 + hx, y0 + hy))
            rm.append((mx, my))
            re_.append((ex, ey))
        return {"rb": rb, "rm": rm, "re": re_}

    # -------------------------------------------------------------- DOM + events
    def crop_params(self, sp: str, box: int, crop: str = "full") -> dict:
        """How an instance frames the rig: screen = offset + k * (canvas + t) with t = (tx, ty) (tx/ty are applied
        after the scale; off only for a centred 'full'). k is a whole number unless soft (tiny chips).
        crop='full' the whole canvas (the medium shot); 'close' the close-up (twice the medium's whole-number zoom:
        x8 for 192-native rigs, x6 for 256); 'face' the head fills the box (chips, host box); 'bust' head + hands."""
        sh = self.sheets[sp]
        W, H = sh.native
        x0, y0, x1, y1 = sh.head_box
        if crop == "close":
            k = max(2, 2 * (box // W)) if box >= W else max(2, -(-box // min(W, H)))
            region = box / k
            cx = (x0 + x1) / 2
            top = y0 - max(0.0, region - (y1 - y0)) * 0.4  # eyes a little above centre, the chin and collar below
            tx = -int(round(min(max(0, cx - region / 2), max(0, W - region))))
            ty = -int(round(min(max(0, top), max(0, H - region))))
            return {"k": k, "tx": tx, "ty": ty, "off": 0, "soft": False}
        if crop in ("face", "bust"):
            head = max(x1 - x0, y1 - y0)
            k = int(box // (head * (1.35 if crop == "face" else 2.05)))
            if k < 1 and box >= 100:  # a big head for this box: a tighter crop, still whole-number pixels
                k = 1
            if k >= 1:  # whole-number zoom: every art pixel is k x k screen pixels
                k = max(k, -(-box // min(W, H)))  # ...and never smaller than what fills the box (no empty strip)
                region = box / k
                cx = (x0 + x1) / 2
                top = y0 - region * 0.1 if crop == "bust" else (y0 + y1) / 2 + (y1 - y0) * 0.08 - region / 2
                tx = -int(round(min(max(0, cx - region / 2), max(0, W - region))))
                ty = -int(round(min(max(0, top), max(0, H - region))))
                return {"k": k, "tx": tx, "ty": ty, "off": 0, "soft": False}
            side = head * 1.6  # smaller than the face at 1x (bench chips): a smooth downscale beats dropped pixels
            return {"k": box / side, "tx": -int(round(max(0, (x0 + x1) / 2 - side / 2))),
                    "ty": -int(round(max(0, (y0 + y1) / 2 - side / 2))), "off": 0, "soft": True}
        k = box // W if box >= W else 0
        if k >= 1:  # centre a whole-number scale in the slot
            return {"k": k, "tx": 0, "ty": 0, "off": (box - k * W) // 2, "soft": False}
        return {"k": box / W, "tx": 0, "ty": 0, "off": 0, "soft": True}

    def html(self, sp: str, a: str, box: int, t: float, cls: str = "", crop: str = "full", key: str | None = None) -> str:
        """An instance of `sp` in a `box`-px square (framing: crop_params). key: the state/class key (f"{sp}-cu"
        for the close-up instance, which holds its body still)."""
        sh = self.sheets[sp]
        W, H = sh.native
        key = key if key in self.state else sp
        f = max(0, min(self.total - 1, int(round(t * self.fps))))
        bx, by, ox, oy, px, py = self.state[key]["rb"][f]
        mx, my = self.state[key]["rm"][f]
        ex, ey = self.state[key]["re"][f]
        pw, ph = sh.patch_size
        cp = self.crop_params(sp, box, crop)
        k = cp["k"]
        if cp["soft"]:
            tf = f"scale({k:.5f})" + (f" translate({cp['tx']}px,{cp['ty']}px)" if (cp["tx"] or cp["ty"]) else "")
        elif crop == "full":
            tf = f"scale({k})" if not cp["off"] else f"translate({cp['off']}px,{cp['off']}px) scale({k})"
        else:
            tf = f"scale({k}) translate({cp['tx']}px,{cp['ty']}px)"
        soft = " soft" if cp["soft"] else ""
        self.instances.append({"sp": sp, "key": key, "box": box, "crop": crop, "k": k, "soft": cp["soft"]})
        return (f'<div class="rgv rg-{key} {cls}{soft}" data-layout-allow-overflow style="width:{box}px;height:{box}px">'
                f'<div class="rgc" style="width:{W}px;height:{H}px;transform:{tf}">'
                f'<div class="rb" style="width:{W}px;height:{H}px;background-image:url({a}{sh.url["body"]});'
                f'background-position:{bx}px {by}px"></div>'
                f'<div class="rf" style="left:{px}px;top:{py}px;width:{pw}px;height:{ph}px">'
                f'<div class="re" style="background-image:url({a}{sh.url["eyes"]});background-position:{ex}px {ey}px"></div>'
                f'<div class="rm" style="background-image:url({a}{sh.url["mouth"]});background-position:{mx}px {my}px">'
                f'</div></div>'
                f'<div class="ro" style="width:{W}px;height:{H}px;background-image:url({a}{sh.url["occ"]});'
                f'background-position:{ox}px {oy}px"></div></div></div>')

    def events(self, sps: set[str], t0: float, t1: float) -> dict[str, list]:
        """Runtime arrays for [t0, t1): rb = [t, sp, bx, by, ox, oy, px, py], rm/re = [t, sp, x, y]."""
        f0, f1 = int(round(t0 * self.fps)), min(self.total, int(round(t1 * self.fps)))
        out = {"rb": [], "rm": [], "re": []}
        for sp in sorted(sps):
            if sp not in self.state:
                continue
            st = self.state[sp]
            if sp.endswith("-cu"):  # the close-up instance only needs its sets while it is on screen
                wins = [(max(f0, int(round(a * self.fps))), min(f1, int(round(b * self.fps))))
                        for a, b in self.cu.get(sp[:-3], [])]
                wins = [(a, b) for a, b in wins if b > a]
                for key in ("rb", "rm", "re"):
                    arr = st[key]
                    for a, b in wins:
                        if a > f0:  # re-sync the full state as it cuts in (it was hidden, possibly stale)
                            out[key].append([round(a / self.fps - t0 - SEEK_EPS, 4), sp, *arr[a]])
                        prev = arr[a]
                        for f in range(a + 1, b):
                            v = arr[f]
                            if v != prev:
                                out[key].append([round(f / self.fps - t0 - SEEK_EPS, 4), sp, *v])
                                prev = v
                continue
            for key in ("rb", "rm", "re"):
                arr = st[key]
                prev = arr[f0] if f0 < len(arr) else None
                for f in range(f0 + 1, f1):
                    v = arr[f]
                    if v != prev:
                        out[key].append([round(f / self.fps - t0 - SEEK_EPS, 4), sp, *v])
                        prev = v
        for key in out:
            out[key].sort(key=lambda r: r[0])
        return out


RIG_CSS = """
.rgv { position: absolute; left: 0; top: 0; overflow: hidden; }
.rgc { position: absolute; left: 0; top: 0; transform-origin: 0 0; image-rendering: pixelated; }
.rb, .ro { position: absolute; left: 0; top: 0; background-repeat: no-repeat; image-rendering: pixelated; }
.rf { position: absolute; }
.rgv.soft .rgc, .rgv.soft .rb, .rgv.soft .ro, .rgv.soft .re, .rgv.soft .rm { image-rendering: auto; }
.re, .rm { position: absolute; left: 0; top: 0; width: 100%; height: 100%; background-repeat: no-repeat;
  image-rendering: pixelated; }
"""

RIG_JS = """
  // character rigs: body frame / face patches, one timeline of sets per character (all instances follow)
  var RG = {};
  (D.rs || []).forEach(function (sp) {
    RG[sp] = { b: S.querySelectorAll(".rg-" + sp + " .rb"), o: S.querySelectorAll(".rg-" + sp + " .ro"),
               f: S.querySelectorAll(".rg-" + sp + " .rf"), m: S.querySelectorAll(".rg-" + sp + " .rm"),
               e: S.querySelectorAll(".rg-" + sp + " .re") };
  });
  (D.rb || []).forEach(function (r) {
    var g = RG[r[1]]; if (!g || !g.b.length) return;
    tl.set(g.b, { backgroundPosition: r[2] + "px " + r[3] + "px" }, r[0]);
    tl.set(g.o, { backgroundPosition: r[4] + "px " + r[5] + "px" }, r[0]);
    tl.set(g.f, { left: r[6] + "px", top: r[7] + "px" }, r[0]);
  });
  (D.rm || []).forEach(function (r) { var g = RG[r[1]]; if (g && g.m.length) tl.set(g.m, { backgroundPosition: r[2] + "px " + r[3] + "px" }, r[0]); });
  (D.re || []).forEach(function (r) { var g = RG[r[1]]; if (g && g.e.length) tl.set(g.e, { backgroundPosition: r[2] + "px " + r[3] + "px" }, r[0]); });
"""
