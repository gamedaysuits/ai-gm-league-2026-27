"""Post-render QA: the flip-book critique, automated.

    uv run python -m gmbench_media.cli qa --run <run> [--profile P] [--name N] [--clip DIR|MP4 ...] [--names]

`all`, `render`, `short` and `roast` run it on their output automatically (--no-qa skips). A clip is an out/
directory holding rundown.json + cues.json (+ avatar_tracks / envelopes) and its rendered mp4: a short, a highlight
clip or the full show. The page is rebuilt in memory exactly as the render built it (deterministic), so every check
reads the plan the video was made from -- and then looks at the video itself:

  frames       the video holds exactly duration x fps frames (no chunk gained one: no drift)         FAIL
  rig.face     each body frame composites to the SAME face (occluded pixels excluded)              FAIL if any differ
  rig.glance   the glance at the reaction cam uses a sprite that really looks that way, and it
               shows in the render                                                                  WARN
  mouth.open   open vowel shapes in >= 15% of speech frames (else the close-ups read as mumbling)   WARN
  sync.render  rendered close-up mouths vs the planned visemes: best lag must be 0 frames            FAIL
  sync.voice   Whisper word onsets in the voice track vs the alignment driving mouths + captions    FAIL (p50>80, p90>250 ms)
  captions     every karaoke highlight sits on a word boundary (no constant offset)                 FAIL
  gestures     apex within 0.3 s of the trigger word (fist pumps: just after the name close-up)      FAIL
  listeners    the reaction lands 0.05-0.45 s after the punchline is SAID, listener on camera       FAIL / WARN
  shots        no shot under 1.2 s; every rig instance >= 100 px at a whole-number pixel scale      FAIL / WARN
  first5       words from frame one, a face, first speech <= 1 s, first cut <= 7 s (shorts)         WARN
  names        the Whisper name check (pronunciation_report.json; --names re-runs it)                WARN

Sheets for a human look: <clip>/qa/*.png. Numbers: <clip>/qa/qa.json.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import avatars as av
from .animate import APEX_LEAD, MIN_SHOT
from .audio import FFMPEG
from .captions import word_times
from .config import load_config
from .ledger import load_league
from .paths import AVATAR_MANIFEST, OUT, ROOT, cache_dir, rel

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
OPEN = {"rest": 0.0, "closed": 0.0, "smirk": 0.0, "small_open": 1.0, "teeth": 1.0, "round": 1.5, "medium_open": 2.0,
        "laugh": 2.5, "wide_open": 3.0}
OPEN_SHAPES = {"medium_open", "wide_open", "round", "laugh"}
SLOT = {"short": (156, 236), "studio": (92, 108)}  # the big frame's content origin (box-shadow rings, no border)
BOX = 768


def _font(size: int, bold: bool = False):
    for p in (f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


def _rig_json(sp: str) -> tuple[dict, Path] | tuple[None, None]:
    man = json.loads(AVATAR_MANIFEST.read_text()) if AVATAR_MANIFEST.exists() else {}
    rp = ((man.get("teams") or {}).get(sp) or {}).get("rig")
    p = (AVATAR_MANIFEST.parent / rp) if rp else (AVATAR_MANIFEST.parent / sp / "rig" / "rig.json")
    if not p.exists():
        return None, None
    return json.loads(p.read_text()), p.parent


def _dims(video: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.strip()
    w, h = (int(x) for x in out.split(",")[:2])
    return w, h


def find_video(cdir: Path) -> Path | None:
    for rep in ("render_report.json",):
        p = cdir / rep
        if p.exists():
            out = (json.loads(p.read_text()).get("output") or {}).get("path")
            if out and (ROOT / out).exists():
                return ROOT / out
    for p in sorted(cdir.glob("short-*.json")):
        mp4 = json.loads(p.read_text()).get("mp4")
        if mp4 and (ROOT / mp4).exists():
            return ROOT / mp4
    cands = [p for p in cdir.glob("*.mp4") if not re.search(r"(-720p|BEFORE|-video)", p.name)]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def frames(video: Path, f0: int, n: int, fps: int, crop: tuple[int, int, int, int] | None = None,
           scale: float | None = None) -> list[Image.Image]:
    """n consecutive frames starting exactly at frame f0. The seek lands half a frame early and passthrough timing
    keeps ffmpeg from duplicating the first frame to fill that gap (its constant-rate default does, which shifts
    every later frame of the sequence by one)."""
    vf = []
    if crop:
        x, y, w, h = crop
        vf.append(f"crop={w}:{h}:{x}:{y}")
    if scale:
        vf.append(f"scale=trunc(iw*{scale}/2)*2:trunc(ih*{scale}/2)*2:flags=lanczos")
    with tempfile.TemporaryDirectory() as td:
        cmd = [FFMPEG, "-v", "error", "-ss", f"{max(0.0, (f0 - 0.5) / fps):.4f}", "-i", str(video), "-frames:v", str(n),
               "-fps_mode", "passthrough"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        subprocess.run(cmd + [f"{td}/f%05d.png"], check=True)
        return [Image.open(p).convert("RGB").copy() for p in sorted(Path(td).glob("f*.png"))]


def sheet(tiles: list[tuple[Image.Image, str, str]], cols: int, title: str, out: Path) -> Path:
    w = max(t[0].width for t in tiles)
    h = max(t[0].height for t in tiles)
    lab, head = 50, (46 if title else 0)
    cols = max(1, min(cols, len(tiles)))
    rows = (len(tiles) + cols - 1) // cols
    img = Image.new("RGB", (cols * (w + 6), head + rows * (h + lab)), (14, 18, 40))
    d = ImageDraw.Draw(img)
    f1, f2, ft = _font(17, True), _font(15), _font(24, True)
    if title:
        d.text((8, 10), title, fill=(255, 255, 255), font=ft)
    for i, (im, l1, l2) in enumerate(tiles):
        x, y = (i % cols) * (w + 6), head + (i // cols) * (h + lab)
        img.paste(im, (x, y))
        d.text((x + 4, y + h + 4), l1[:60], fill=(255, 220, 90), font=f1)
        d.text((x + 4, y + h + 26), l2[:70], fill=(205, 215, 255), font=f2)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# --------------------------------------------------------------------------- whisper (word onsets)

def word_stamps_many(wavs: list[Path], log=print) -> dict[str, list]:
    """{str(wav): [[word, start, end], ...]} via pronounce.word_stamps (cached). faster-whisper is optional for the
    main environment: when it isn't importable the work runs in `uv run --with faster-whisper` (a subprocess)."""
    try:
        import faster_whisper  # noqa: F401
        from .pronounce import word_stamps
        return {str(w): [list(x) for x in word_stamps(w)] for w in wavs}
    except ImportError:
        cmd = ["uv", "run", "-q", "--with", "faster-whisper", "python", "-m", "gmbench_media.qa", "stamps",
               *[str(w) for w in wavs]]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT / "media")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-800:])
        return json.loads(proc.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- one clip

class ClipQA:
    def __init__(self, run: str, cdir: Path, video: Path | None = None, log=print, whisper: bool = True,
                 names: bool = False):
        self.run_id, self.cdir, self.log, self.use_whisper, self.names = run, cdir, log, whisper, names
        self.video = video or find_video(cdir)
        if not self.video:
            raise SystemExit(f"qa: no rendered mp4 in {rel(cdir)}")
        self.rd = json.loads((cdir / "rundown.json").read_text())
        self.cues = json.loads((cdir / "cues.json").read_text())
        tracks = json.loads((cdir / "avatar_tracks.json").read_text()) if (cdir / "avatar_tracks.json").exists() else {}
        env = json.loads((cdir / "envelopes.json").read_text())
        self.L = load_league(run)
        vw, vh = _dims(self.video)
        self.layout = "short" if vh > vw else "studio"
        self.dims = (vw, vh)
        self.fps = int(self.cues["fps"])
        self.out = cdir / "qa"
        self.out.mkdir(exist_ok=True)
        self.pages_data: list[tuple[float, dict]] = []  # (chunk start, the page's runtime data)
        t0 = time.time()
        if self.layout == "short":
            from .short import build_short_page
            self.cfg = load_config("short", {"script": {"target_minutes": 1}})
            name = cdir.name
            doc, _, self.model, self.rv = build_short_page(name, self.L, self.rd, self.cues, tracks, env, self.cfg)
            self.pages_data.append((0.0, self._page_data(doc)))
            self.docs = [doc]
        else:
            from .compose import Assets, Builder, ShowModel, build_rigview
            prof = self.rd.get("profile") or "full"
            self.cfg = load_config(prof)
            fs = av.load_framesets(self.L, av.all_speakers(self.L))
            self.model = ShowModel(self.L, self.rd, self.cues, tracks, fs, self.cfg, env=env)
            self.rv = build_rigview(self.model, self.cfg)
            comp = cdir / "compose.json"
            limit = (json.loads(comp.read_text()).get("minutes_limit") if comp.exists() else None)
            b = Builder(self.model, Assets(self.model.fs), self.cfg, self.rv)
            self.docs = []
            for ch in self.model.chunks(limit * 60.0 if limit else None):
                doc, _ = b.chunk_html(ch, standalone=True)
                self.pages_data.append((ch.t0, self._page_data(doc)))
                self.docs.append(doc)
        self.build_s = round(time.time() - t0, 1)
        self.plan = self.model.plan
        self.slot = SLOT[self.layout]
        self.lines = self.model.lines
        self.T = float(self.model.T)
        self.last_end = max(float(ln["end"]) for ln in self.lines)
        self.side = -1 if self.layout == "short" else 1
        self.rigs = {}
        for sp, rig in (self.rv.rigs.items() if self.rv else []):
            d, base = _rig_json(sp)
            if d and d.get("kind") != "mock" and rig.kind != "mock":
                self.rigs[sp] = (d, base)

    @staticmethod
    def _page_data(doc: str) -> dict:
        i = doc.find("var D = ")
        if i < 0:
            return {}
        return json.JSONDecoder().raw_decode(doc, i + len("var D = "))[0]

    # ------------------------------------------------------------- timeline helpers
    def focus_at(self, t: float):
        return self.model.value_at(self.model.focus_ev, t)

    def cu_at(self, sp: str, t: float) -> bool:
        """Is `sp` in close-up at t -- as RENDERED: the rig view's close-up instance windows (the page toggles the
        instance on these, so two overlapping shots that switched it off mid-shot show up here)."""
        if self.rv is not None and getattr(self.rv, "cu", None):
            return any(a - 1e-6 <= t < b - 1e-6 for a, b in self.rv.cu.get(sp, []))
        return any(x["t0"] - 1e-6 <= t < x["t1"] - 1e-6 and x["sp"] == sp for x in self.model.shots)

    def track(self, rle: list[list], default: str) -> list[str]:
        total = int(self.cues["total_frames"])
        out = [default] * total
        for k, (f, v) in enumerate(rle):
            f1 = rle[k + 1][0] if k + 1 < len(rle) else total
            out[int(f):int(f1)] = [v] * max(0, int(f1) - int(f))
        return out

    def rect(self, sp: str, key: str, t: float, box_name: str, crop: str, pad: int = 1) -> tuple[int, int, int, int] | None:
        """Screen rect of the mouth or eyes of `sp` in the big frame at time t (medium: crop='full'; close-up: 'close')."""
        d = self.rigs.get(sp, (None,))[0]
        if not d or not self.rv:
            return None
        hb = (d.get("head") or {}).get(box_name)
        if not hb:
            return None
        f = max(0, min(int(self.cues["total_frames"]) - 1, int(round(t * self.fps))))
        px, py = self.rv.state[key]["rb"][f][4:6]
        cp = self.rv.crop_params(sp, BOX, crop)
        k = cp["k"]
        sx, sy = self.slot
        x0 = sx + cp["off"] + k * (px + hb[0] - pad + cp["tx"])
        y0 = sy + cp["off"] + k * (py + hb[1] - pad + cp["ty"])
        x1 = sx + cp["off"] + k * (px + hb[2] + pad + cp["tx"])
        y1 = sy + cp["off"] + k * (py + hb[3] + pad + cp["ty"])
        x0, y0 = max(sx, int(x0)), max(sy, int(y0))
        x1, y1 = min(sx + BOX, int(x1)), min(sy + BOX, int(y1))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def big(self, t: float, scale: float = 0.42) -> Image.Image:
        sx, sy = self.slot
        return frames(self.video, int(round(t * self.fps)), 1, self.fps, (sx, sy, BOX, BOX), scale)[0]

    # ------------------------------------------------------------- checks
    def check_rig_face(self) -> dict:
        bad, n = [], 0
        for sp, (d, base) in self.rigs.items():
            fm = (d.get("head") or {}).get("face_mask")
            x0, y0, x1, y1 = d["head"]["box"]
            mask = None
            if fm and (base / fm).exists():
                a = np.asarray(Image.open(base / fm).convert("L")) > 127
                er = a.copy()
                er[1:, :] &= a[:-1, :]
                er[:-1, :] &= a[1:, :]
                er[:, 1:] &= a[:, :-1]
                er[:, :-1] &= a[:, 1:]
                mask = er
            base_fr = d["gestures"]["idle_breathe"]["frames"][0]
            for eyes, vis in (("open", "rest"), ("blink", "wide_open"), ("angry", "teeth")):
                if eyes not in d.get("eyes", {}) or vis not in d["visemes"]["neutral"]:
                    continue

                def comp(fr):
                    body = Image.open(base / fr["file"]).convert("RGBA")
                    hx, hy = fr.get("head_offset") or (0, 0)
                    if fr.get("face", "rig") != "baked":
                        for p in (d["eyes"][eyes], d["visemes"]["neutral"][vis]):
                            body.alpha_composite(Image.open(base / p).convert("RGBA"), (x0 + hx, y0 + hy))
                    om = np.zeros((y1 - y0, x1 - x0), bool)
                    if fr.get("occluder"):
                        occ = Image.open(base / fr["occluder"]).convert("RGBA")
                        body.alpha_composite(occ)
                        om = np.asarray(occ.crop((x0 + hx, y0 + hy, x1 + hx, y1 + hy)))[..., 3] > 0
                    return np.asarray(body.crop((x0 + hx, y0 + hy, x1 + hx, y1 + hy))).astype(int), om

                ref, rom = comp(base_fr)
                for g, clip in d["gestures"].items():
                    for i, fr in enumerate(clip["frames"]):
                        if fr.get("face", "rig") == "baked":
                            continue
                        c, om = comp(fr)
                        m = ~(om | rom)
                        if mask is not None and mask.shape == m.shape:
                            m &= mask
                        diff = int(((np.abs(c - ref).max(axis=2) > 8) & m).sum())
                        n += 1
                        if diff:
                            bad.append(f"{sp}:{g}[{i}] {eyes}+{vis} {diff}px")
        if not self.rigs:
            return {"status": SKIP, "summary": "no real rigs in this clip"}
        mocks = sorted(set(self.rv.rigs) - set(self.rigs)) if self.rv else []
        extra = f"; placeholders/mock rigs: {', '.join(mocks)}" if mocks else ""
        if bad:
            return {"status": FAIL, "summary": f"{len(bad)} composites differ: {bad[:4]}{extra}", "bad": bad}
        return {"status": PASS, "summary": f"{len(self.rigs)} rigs, {n} composites: the face never redraws{extra}",
                "mock": mocks}

    def check_rig_glance(self) -> dict:
        if not self.rv:
            return {"status": SKIP, "summary": "no rigs"}
        want = "screen-left" if self.side < 0 else "screen-right"
        spans: dict[str, list[float]] = {}
        for sp in self.rigs:
            for k, (f, v) in enumerate(self.plan.eye_track(sp)):
                if v == "look_addressee":
                    spans.setdefault(sp, []).append(f / self.fps)
        missing = sorted(sp for sp in spans if self.rv.glance.get(sp, "open") == "open")
        # the render: eyes at the glance vs 0.35 s before, medium shot, the character in the big frame
        tiles, seen, changed = [], 0, 0
        for sp in sorted(spans):
            if sp in missing:
                continue
            for t in spans[sp][:2]:
                t1 = t + 0.1
                if self.focus_at(t1) != sp or self.cu_at(sp, t1) or self.focus_at(t - 0.35) != sp or self.cu_at(sp, t - 0.35):
                    continue
                r1 = self.rect(sp, sp, t1, "eyes_box", "full", 2)
                r0 = self.rect(sp, sp, t - 0.35, "eyes_box", "full", 2)
                if not r1 or not r0:
                    continue
                a = frames(self.video, int(round(t1 * self.fps)), 1, self.fps, r1)[0]
                b = frames(self.video, int(round((t - 0.35) * self.fps)), 1, self.fps, r0)[0]
                if a.size != b.size:
                    b = b.resize(a.size)
                diff = int((np.abs(np.asarray(a.convert("L"), int) - np.asarray(b.convert("L"), int)) > 30).sum())
                seen += 1
                changed += int(diff >= 10)
                z = max(1, 320 // a.width)
                tiles.append((b.resize((b.width * z, b.height * z), Image.NEAREST), f"{sp} {t - 0.35:.2f}s", "before"))
                tiles.append((a.resize((a.width * z, a.height * z), Image.NEAREST), f"{sp} {t1:.2f}s",
                              f"glance ({self.rv.glance[sp]}), {diff} px changed"))
                break
        if tiles:
            sheet(tiles, 4, f"GLANCES toward the reaction cam ({want})", self.out / "glances.png")
        data = {"want": want, "speakers": sorted(spans), "missing": missing, "rendered": seen, "changed": changed}
        if missing:
            return {"status": WARN, "summary": f"no {want} glance sprite for {', '.join(missing)} "
                                               f"({len(missing)}/{len(spans)}): they hold eyes front", **data}
        if seen and changed < seen:
            return {"status": WARN, "summary": f"glance visible in {changed}/{seen} sampled moments", **data}
        return {"status": PASS, "summary": f"{len(spans)} speakers glance {want}; visible in {changed}/{seen} sampled "
                                           f"moments", **data}

    def check_mouth_open(self) -> dict:
        tot = opn = 0
        per: dict[str, list[int]] = {}
        for ln in self.lines:
            sp = ln["speaker"]
            arr = self.track(self.plan.mouth_track(sp), "rest")
            f0, f1 = int(round(float(ln["start"]) * self.fps)), int(round(float(ln["end"]) * self.fps))
            seg = arr[f0:f1]
            o = sum(1 for v in seg if v in OPEN_SHAPES)
            tot += len(seg)
            opn += o
            per.setdefault(sp, [0, 0])
            per[sp][0] += o
            per[sp][1] += len(seg)
        share = opn / max(1, tot)
        low = sorted(sp for sp, (o, n) in per.items() if n > 90 and o / n < 0.12)
        st = PASS if share >= 0.15 and not low else WARN
        return {"status": st, "summary": f"open vowel shapes in {share:.0%} of speech frames"
                + (f"; low: {', '.join(low)}" if low else ""), "share": round(share, 3)}

    def check_sync_render(self) -> dict:
        wins = [x for x in self.model.shots if x["kind"] == "cu" and x["t1"] - x["t0"] >= 1.0 and x["sp"] in self.rigs
                and any(ln["speaker"] == x["sp"] and float(ln["start"]) < x["t1"] and float(ln["end"]) > x["t0"]
                        for ln in self.lines)]
        if not wins:
            return {"status": SKIP, "summary": "no speaker close-ups with a real rig"}
        # spread the samples through the clip
        if len(wins) > 5:
            wins = [wins[int(i * (len(wins) - 1) / 4)] for i in range(5)]
        pairs, flip = [], None
        for w in wins:
            sp = w["sp"]
            key = f"{sp}-cu" if f"{sp}-cu" in self.rv.state else sp
            f0 = int(math.ceil((w["t0"] + 0.1) * self.fps))
            f1 = int(math.floor((w["t1"] - 0.1) * self.fps))
            r = self.rect(sp, key, f0 / self.fps, "mouth_box", "close", 2)
            if not r or f1 - f0 < 12:
                continue
            imgs = frames(self.video, f0, f1 - f0, self.fps, r)
            px = [np.asarray(im.convert("L"), float) for im in imgs]
            diff = np.array([0.0] + [float(np.abs(px[i] - px[i - 1]).mean()) for i in range(1, len(px))])
            rm = self.rv.state[key]["rm"]  # the mouth sprite actually placed per frame (after mood / fallbacks)
            changes = [i for i in range(1, len(px)) if rm[f0 + i] != rm[f0 + i - 1]]
            if changes:
                pairs.append((changes, diff))
            if flip is None:
                flip = (sp, f0, imgs, self.track(self.plan.mouth_track(sp), "rest"))
        if not pairs:
            return {"status": SKIP, "summary": "close-up mouths could not be measured"}
        # every planned sprite change must show on ITS frame: hit rate of rendered changes at lag L
        res = {}
        n_changes = sum(len(c) for c, _ in pairs)
        for lag in range(-2, 3):
            hits = 0
            for changes, diff in pairs:
                thr = max(1.0, float(np.percentile(diff, 50)) * 2)
                hits += sum(1 for i in changes if 0 <= i + lag < len(diff) and diff[i + lag] > thr)
            res[lag] = hits / max(1, n_changes)
        best = max(res, key=res.get)
        if res[0] >= res[best] - 0.02:
            best = 0
        if flip:  # the flip-book: the first measured close-up, every 2nd frame, labelled with the plan
            sp, f0, imgs, arr = flip
            words = [w for ln in self.lines if ln["speaker"] == sp for w in word_times(ln)]
            tiles = []
            for i in range(0, min(len(imgs), 48), 2):
                t = (f0 + i) / self.fps
                wd = next((q["w"] for q in words if q["start"] <= t < q["said"] + 0.02), "")
                im = imgs[i]
                z = max(1, 220 // im.width)
                tiles.append((im.resize((im.width * z, im.height * z), Image.NEAREST), f"{t:.2f}s {wd}",
                              f"plan: {arr[f0 + i]}"))
            sheet(tiles, 8, f"MOUTH vs WORDS - {sp} close-up (rendered crop; label = word being said + planned mouth)",
                  self.out / "mouth-closeup.png")
        data = {"hit_rate_by_lag": {str(k): round(v, 3) for k, v in res.items()}, "best_lag": best,
                "windows": len(pairs), "mouth_changes": n_changes}
        if best != 0:
            return {"status": FAIL, "summary": f"rendered mouth changes land {best:+d} frame(s) off the plan "
                                               f"({res[best]:.0%} vs {res[0]:.0%} on their own frame)", **data}
        if res[0] < 0.6:
            return {"status": WARN, "summary": f"only {res[0]:.0%} of {n_changes} planned mouth changes visible on "
                                               f"their frame", **data}
        return {"status": PASS, "summary": f"{n_changes} mouth changes in {len(pairs)} close-ups: {res[0]:.0%} on their "
                                           f"own frame (+-1 frame: {res[-1]:.0%} / {res[1]:.0%})", **data}

    def check_sync_voice(self) -> dict:
        if not self.use_whisper:
            return {"status": SKIP, "summary": "--no-whisper"}
        voice = self.cdir / "voice.wav"
        if not voice.exists():
            return {"status": SKIP, "summary": "no voice.wav"}
        man_p = self.cdir / "tts_manifest.json"
        engines = {k: v.get("engine") for k, v in (json.loads(man_p.read_text())["lines"].items() if man_p.exists() else [])}
        real = [ln for ln in self.lines if engines.get(ln["id"]) == "elevenlabs" and word_times(ln)]
        if not real:
            return {"status": SKIP, "summary": "no ElevenLabs lines (dev voices carry synthetic timing)"}
        segs: list[tuple[Path, float, list[dict]]] = []
        if self.T <= 150:
            segs.append((voice, 0.0, real))
        else:  # long shows: up to 6 calls spread through the show, cut from the voice stem
            pick = real if len(real) <= 6 else [real[int(i * (len(real) - 1) / 5)] for i in range(6)]
            st = voice.stat()
            cdir = cache_dir("qa", re.sub(r"[^a-z0-9]+", "-", rel(voice).lower())[-60:] + f"-{st.st_size}")
            for ln in pick:
                a = max(0.0, float(ln["start"]) - 0.25)
                b = float(ln["end"]) + 0.25
                wav = cdir / f"{ln['id']}.wav"
                if not wav.exists():
                    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(voice),
                                    "-ac", "1", "-ar", "16000", str(wav)], check=True)
                segs.append((wav, a, [ln]))
        try:
            stamps = word_stamps_many([s[0] for s in segs], self.log)
        except Exception as e:  # noqa: BLE001
            return {"status": SKIP, "summary": f"whisper unavailable: {str(e)[:120]}"}
        offs, mid = [], []
        for wav, a, lns in segs:
            heard = [(_norm(w), a + float(s)) for w, s, _ in stamps.get(str(wav), []) if _norm(w)]
            for ln in lns:
                ws = word_times(ln)
                for k, w in enumerate(ws):
                    c = _norm(w["w"])
                    if len(c) < 3:
                        continue
                    cand = [t for x, t in heard if x == c and abs(t - w["start"]) < 0.6]
                    if cand:
                        off = min(cand, key=lambda t: abs(t - w["start"])) - w["start"]
                        offs.append(off)
                        if k and w["start"] - ws[k - 1]["said"] <= 0.25:
                            mid.append(off)  # Whisper's word onsets are only trustworthy mid-phrase
        if len(offs) < 10 or len(mid) < 8:
            return {"status": WARN, "summary": f"only {len(offs)} words matched"}
        o, m_ = np.array(offs), np.array(mid)
        med = float(np.median(o))
        p50, p90 = float(np.percentile(np.abs(m_), 50)), float(np.percentile(np.abs(m_), 90))
        data = {"words": len(o), "mid_phrase": len(m_), "median_ms": round(med * 1000), "p50_abs_ms": round(p50 * 1000),
                "p90_abs_ms": round(p90 * 1000), "within_2f": round(float(np.mean(np.abs(m_) <= 2 / self.fps)), 3)}
        st = PASS if abs(med) <= 0.08 and p50 <= 0.08 and p90 <= 0.22 else FAIL
        return {"status": st, "summary": f"{len(o)} words, median {data['median_ms']:+d} ms (no global shift); "
                                         f"mid-phrase |off| p50 {data['p50_abs_ms']} ms, p90 {data['p90_abs_ms']} ms",
                **data}

    def check_captions(self) -> dict:
        """Each karaoke event against ITS OWN word: key w{page}_{k} -> the k-th word of that page; on at its start,
        off at its end. Any shift of the highlight clock shows up exactly."""
        own: dict[str, tuple[float, float]] = {}
        for i, pg in enumerate(self.model.pages):
            k = 0
            for row in pg["rows"]:
                for w in row:
                    own[f"w{i}_{k}"] = (float(w["start"]), float(w["end"]))
                    k += 1
        offs = []
        for t0, D in self.pages_data:  # event times are relative to the page's (chunk's) start
            for ev in D.get("w") or []:
                exp = own.get(ev[1])
                if exp is None:
                    continue
                offs.append(float(ev[0]) + t0 - (exp[0] if ev[3] else exp[1]))
        if not offs:
            return {"status": SKIP, "summary": "no highlight events"}
        o = np.array(offs)
        worst = float(np.abs(o).max())
        st = PASS if worst <= 0.5 / self.fps else FAIL
        return {"status": st, "summary": f"{len(o)} highlight events on their own words: median {np.median(o) * 1000:+.0f} "
                                         f"ms, worst {worst * 1000:.0f} ms", "worst_ms": round(worst * 1000, 1)}

    def check_gestures(self) -> dict:
        log_ = self.plan.gesture_log
        if not log_:
            return {"status": SKIP, "summary": "no gestures"}
        bad, off_cam, tiles = [], [], []
        for g in log_:
            apex = g["t"] + (0.0 if g["g"] == "laugh" else APEX_LEAD)
            if g.get("after_cu"):
                d = apex - g["after_cu"]
                ok = 0.0 <= d <= 0.5
                why = f"{d:+.2f}s after the name close-up"
            else:
                d = apex - g["word_t"]
                ok = abs(d) <= 0.3
                why = f"apex {d:+.2f}s from '{g['word']}'"
            if not ok:
                bad.append(f"{g['sp']} {g['g']} @ {g['t']:.2f}: {why}")
            on = self.focus_at(apex) == g["sp"] and not self.cu_at(g["sp"], apex)
            if not on:
                off_cam.append(f"{g['sp']} {g['g']} @ {apex:.2f}")
            elif ok and len(tiles) < 12 and g["g"] != "laugh":
                tiles.append((self.big(apex + 0.05), f"{apex:.2f}s {g['sp']} {g['g'].upper()}",
                              f"on '{g['word']}'" if not g.get("after_cu") else f"after the '{g['word']}' close-up"))
        if tiles:
            sheet(tiles, 4, "GESTURES ON THE WORDS (apex frames, big frame)", self.out / "gestures.png")
        kinds: dict[str, int] = {}
        for g in log_:
            kinds[g["g"]] = kinds.get(g["g"], 0) + 1
        data = {"n": len(log_), "kinds": kinds, "bad": bad, "off_camera": off_cam}
        if bad:
            return {"status": FAIL, "summary": f"{len(bad)}/{len(log_)} off their word: {bad[:3]}", **data}
        return {"status": PASS, "summary": f"{len(log_)} gestures on their words ({', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))})"
                + (f"; {len(off_cam)} fell during another shot" if off_cam else ""), **data}

    def check_listeners(self) -> dict:
        ps = self.plan.punches
        if not ps:
            return {"status": SKIP, "summary": "no reaction beats"}
        bad, unseen, rows = [], [], []
        for p in ps:
            if p.get("beat_mark"):  # the timing pass: the cut lands 80 ms after the kicker (gate: within 100 ms)
                d = float(p["t"]) - float(p["kicker_end"])
                if not (0.0 <= d <= 0.1 + 1e-6):
                    bad.append(f"{p.get('listener')} @ {p['t']:.2f}: cut {d * 1000:+.0f} ms from the kicker")
                # on camera: a cut to them on the kicker, or their reaction shot already running (a second jab
                # landing while the camera holds on the same face)
                if p.get("listener") and not any(x["kind"] in ("listener", "cu") and x["sp"] == p["listener"]
                                                 and x["t0"] - 0.06 <= p["t"] < x["t1"] - 0.3
                                                 for x in self.model.shots):
                    unseen.append(f"{p['listener']} @ {p['t']:.2f}")
                if len(rows) < 4 and p.get("listener"):
                    t = float(p["t"])
                    rows.append([(self.big(t - 0.2, 0.33), f"{t - 0.2:.2f}s kicker", p.get("sentence", "")[-40:]),
                                 (self.big(t + 0.1, 0.33), f"{t + 0.1:.2f}s CUT: {p['listener']}", "surprise"),
                                 (self.big(t + 0.6, 0.33), f"{t + 0.6:.2f}s", p["reaction"]),
                                 (self.big(t + 1.1, 0.33), f"{t + 1.1:.2f}s", p["reaction"])])
                continue
            words = self.plan._pwords.get(p["line"]) or []
            ev = self.plan._pevent.get(p["line"]) or {}
            if not words:
                bad.append(f"{p['listener']} @ {p['t']:.2f}: no punchline found")
                continue
            said = float(words[-1]["said"])
            d = float(p["t"]) - said
            clamped = abs(float(p["t"]) - (float(ev.get("until", 1e9)) - 0.3)) < 0.02
            if not (0.05 <= d <= 0.45) and not clamped:
                bad.append(f"{p['listener']} @ {p['t']:.2f}: {d:+.2f}s from the punchline")
            cut = any(x["kind"] == "listener" and x["sp"] == p["listener"] and abs(x["t0"] - p["t"]) < 0.06
                      for x in self.model.shots)
            ln = next((x for x in self.lines if x["id"] == p["line"]), {})
            cam = cut or ln.get("addressed_to") == p["listener"] or (self.layout == "studio" and p["line"] in self.model.beat2)
            if not cam:
                unseen.append(f"{p['listener']} @ {p['t']:.2f}")
            if len(rows) < 4 and cut:
                t = float(p["t"])
                rows.append([(self.big(t - 0.2, 0.33), f"{t - 0.2:.2f}s speaker", f"'...{words[-1]['w']}'"),
                             (self.big(t + 0.1, 0.33), f"{t + 0.1:.2f}s CUT: {p['listener']}", "surprise"),
                             (self.big(t + 0.6, 0.33), f"{t + 0.6:.2f}s", p["reaction"]),
                             (self.big(t + 1.1, 0.33), f"{t + 1.1:.2f}s", p["reaction"])])
        if rows:
            sheet([x for r in rows for x in r], 4, "LISTENER REACTIONS ON THE PUNCHLINE", self.out / "listeners.png")
        data = {"n": len(ps), "bad": bad, "unseen": unseen}
        if bad:
            return {"status": FAIL, "summary": f"{len(bad)}/{len(ps)} reactions off the punchline: {bad[:3]}", **data}
        if unseen:
            return {"status": WARN, "summary": f"{len(unseen)} reactions off camera: {unseen[:3]}", **data}
        return {"status": PASS, "summary": f"{len(ps)} reactions 0.05-0.45 s after the punchline, on camera", **data}

    def check_shots(self) -> dict:
        m = self.model
        pts = sorted({t for t, _ in m.focus_ev} | {x["t0"] for x in m.shots} | {x["t1"] for x in m.shots} | {0.0})
        seq = []
        D0 = self.pages_data[0][1] if self.layout == "short" and self.pages_data else {}
        end_on = [float(r[0]) for r in D0.get("v") or [] if r[1] == "end" and r[2]]
        stop = end_on[0] + 0.45 if end_on else self.last_end + 0.3  # shorts: until the end card is opaque
        for t in pts:
            if t >= stop:
                continue
            f = m.value_at(m.focus_ev, t + 1e-6)
            s_ = (f, "CU" if f and self.cu_at(f, t + 1e-6) else "MED")
            if not seq or seq[-1][1] != s_:
                seq.append((t, s_))
        seq.append((stop, None))
        lens = [(seq[i + 1][0] - seq[i][0], seq[i][0], seq[i][1]) for i in range(len(seq) - 1)
                if seq[i + 1][0] - seq[i][0] > 1e-3]
        short_ = [f"{a:.2f}s @ {t:.1f} ({s_[0]} {s_[1]})" for a, t, s_ in lens if a < MIN_SHOT - 0.01]
        frac = [i for i in (self.rv.instances if self.rv else []) if not i["soft"] and float(i["k"]) != int(i["k"])]
        soft = sorted({i["box"] for i in (self.rv.instances if self.rv else []) if i["soft"]})
        big_soft = [b for b in soft if b >= 100]
        data = {"shots": len(lens), "shortest_s": round(min(a for a, _, _ in lens), 3), "short": short_,
                "cu": sum(1 for x in m.shots if x["kind"] == "cu"),
                "listener_cuts": sum(1 for x in m.shots if x["kind"] == "listener"), "soft_boxes": soft}
        summ = (f"{len(lens)} shots ({data['cu']} close-ups, {data['listener_cuts']} reaction cuts), shortest "
                f"{data['shortest_s']:.2f}s; whole-number scale everywhere >= 100 px"
                + (f" (soft chips: {soft} px)" if soft else ""))
        if frac or big_soft:
            return {"status": FAIL, "summary": f"fractional scale: {frac[:3]} {big_soft}", **data}
        if any(a < 1.0 for a, _, _ in lens):
            return {"status": FAIL, "summary": f"shots under 1.0 s: {short_[:4]}", **data}
        if short_:
            return {"status": WARN, "summary": f"{len(short_)} shots under {MIN_SHOT}s: {short_[:4]}", **data}
        return {"status": PASS, "summary": summ, **data}

    def check_first5(self) -> dict:
        ts = [0.1, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        tiles = [(frames(self.video, int(round(t * self.fps)), 1, self.fps, None, 0.25 if self.layout == "short" else 0.2)[0],
                  f"{t:.1f}s", "") for t in ts if t < self.T]
        sheet(tiles, 8, "FIRST SECONDS", self.out / "first5.png")
        first_page = min((pg["show"] for pg in self.model.pages), default=99)
        if (self.rd.get("short") or {}).get("tight"):  # word cards: the first is up from frame one
            D = self.pages_data[0][1]
            tk = [float(r[0]) for r in D.get("v") or [] if str(r[1]).startswith("tk") and r[2]]
            on0 = [float(r[0]) for r in D.get("v") or [] if r[1] == "tk0" and r[2]]
            first_page = on0[0] if on0 else (0.0 if tk or "-tk0" in (self.docs[0] if self.docs else "") else 99.0)
        first_speech = min(float(ln["start"]) for ln in self.lines)
        face = self.focus_at(0.0) is not None
        cuts = [t for t, _ in self.model.focus_ev if t > 0.05] + [x["t0"] for x in self.model.shots if x["t0"] > 0.05] + \
               [x["t1"] for x in self.model.shots if x["t0"] <= 0.05]
        first_cut = min(cuts) if cuts else 99.0
        data = {"caption_s": round(first_page, 2), "first_speech_s": round(first_speech, 2), "face": face,
                "first_cut_s": round(first_cut, 2)}
        summ = (f"caption at {first_page:.2f}s, first word at {first_speech:.2f}s, first cut at {first_cut:.2f}s, "
                f"{'a face' if face else 'no face'} from frame one")
        if self.layout != "short":
            return {"status": PASS, "summary": "(show) " + summ, **data}
        ok = first_page <= 0.2 and first_speech <= 1.0 and face and first_cut <= 7.0
        return {"status": PASS if ok else WARN, "summary": summ, **data}

    def check_names(self) -> dict:
        rep_p = self.cdir / "pronunciation_report.json"
        if self.names:
            args = self._pronounce_args()
            if args:
                subprocess.run(["uv", "run", "-q", "--with", "faster-whisper", "python", "-m", "gmbench_media.pronounce",
                                "check", *args], cwd=ROOT / "media", capture_output=True, text=True)
        if not rep_p.exists():
            return {"status": SKIP, "summary": "no name check yet (--names runs it)"}
        rep = json.loads(rep_p.read_text())
        fails = rep.get("name_failures") or []
        dropped = rep.get("dropped_phrases") or []
        stale = rep_p.stat().st_mtime < (self.cdir / "tts_manifest.json").stat().st_mtime \
            if (self.cdir / "tts_manifest.json").exists() else False
        names = sorted({f.get("name", "?") for f in fails})
        extra = " (report older than the audio; --names re-runs it)" if stale else ""
        if fails or dropped:
            return {"status": WARN, "summary": f"{len(fails)} name calls flagged ({', '.join(names)}), "
                                               f"{len(dropped)} clips missing words{extra}", "names": names}
        return {"status": PASS, "summary": f"{rep.get('name_checks', 0)} name calls heard right{extra}"}

    def _pronounce_args(self) -> list[str] | None:
        parts = rel(self.cdir).split("/")
        if len(parts) < 3:
            return None
        top = parts[2]
        if top.endswith("-shorts") and len(parts) > 3:
            return ["--run", self.run_id, "--short", parts[3]]
        prof = top[len(self.run_id) + 1:] if top.startswith(self.run_id + "-") else None
        args = ["--run", self.run_id] + (["--profile", prof] if prof else [])
        return args + (["--name", parts[3]] if len(parts) > 3 else [])

    # ------------------------------------------------------------- run
    def check_frames(self) -> dict:
        """Chunked renders: every chunk video holds exactly its planned frames (HyperFrames renders
        ceil(duration x fps) frames; one extra frame in a chunk puts every later chunk a frame behind the audio, and
        the final mux trims to the audio length, which would hide it). Single compositions: the whole video."""
        def count(p: Path) -> int | None:
            out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
                                  "stream=nb_read_packets", "-of", "csv=p=0", str(p)], capture_output=True, text=True)
            try:
                return int(out.stdout.strip().split(",")[0])
            except ValueError:
                return None
        vdir = self.cdir / "video"
        if self.layout == "studio" and vdir.exists():
            comp = self.cdir / "compose.json"
            limit = (json.loads(comp.read_text()).get("minutes_limit") if comp.exists() else None)
            bad, n_tot = [], 0
            chunks = self.model.chunks(limit * 60.0 if limit else None)
            for ch in chunks:
                n = count(vdir / f"{ch.cid}.mp4")
                n_tot += n or 0
                if n != ch.frames:
                    bad.append(f"{ch.cid} {n} vs {ch.frames}")
            data = {"chunks": len(chunks), "frames": n_tot, "bad": bad}
            if bad:
                return {"status": FAIL, "summary": f"chunks off their frame count: {bad[:4]} (later chunks drift off the "
                                                   f"audio)", **data}
            return {"status": PASS, "summary": f"{len(chunks)} chunks, {n_tot} frames: every chunk exact (no drift)", **data}
        n = count(self.video)
        want = int(round(self.T * self.fps))
        if n is None:
            return {"status": SKIP, "summary": "could not count frames"}
        if n != want:
            return {"status": FAIL, "summary": f"{n} frames, expected {want} ({n - want:+d})", "frames": n}
        return {"status": PASS, "summary": f"{n} frames = duration x fps exactly", "frames": n}

    def _marks(self) -> list[dict]:
        out = []
        for ln in self.lines:
            for mk in (ln.get("beats") or {}).get("marks") or []:
                out.append({**mk, "line": ln["id"], "speaker": ln["speaker"], "kind": ln["kind"],
                            "ks": float(ln["start"]) + float(mk["kicker_start"]),
                            "ke": float(ln["start"]) + float(mk["kicker_end"]), "line_end": float(ln["end"])})
        return sorted(out, key=lambda x: x["ke"])

    def check_word_cards(self) -> dict:
        """Tight shorts: every word card is up when its first word is spoken (+-1 frame)."""
        if not (self.rd.get("short") or {}).get("tight"):
            return {"status": SKIP, "summary": "line captions (not a tight short)"}
        from .captions import word_times
        D = self.pages_data[0][1]
        on = {r[1]: float(r[0]) for r in D.get("v") or [] if str(r[1]).startswith("tk") and r[2]}
        starts = sorted(w["start"] for ln in self.lines for w in word_times(ln))
        bad = []
        for key, t in on.items():
            if key == "tk0":
                continue  # the first card is up from frame one (muted autoplay), before the voice
            nxt = min((x for x in starts if x >= t - 0.001), default=None)
            if nxt is None or abs(nxt - (t + 0.03)) > 1.5 / self.fps:
                bad.append(f"{key} @ {t:.2f}")
        if bad:
            return {"status": FAIL, "summary": f"{len(bad)} word cards off their word: {bad[:4]}"}
        return {"status": PASS, "summary": f"{len(on) + 1} word cards, each up on its first word"}

    def check_short_gates(self) -> dict:
        """The short's contract: <= 50 s, first word <= 0.5 s, context by 3 s, a punchline at least every ~10 s,
        no host, no dead air > 0.8 s outside punch beats, the model name in every speaker's plate."""
        sh = self.rd.get("short") or {}
        if self.layout != "short" or not sh.get("tight"):
            return {"status": SKIP, "summary": "not a tight short"}
        from .captions import word_times
        fails, warns = [], []
        if self.T > 50.0 + 1e-6:
            fails.append(f"{self.T:.1f} s long (max 50)")
        fw = min((w["start"] for ln in self.lines for w in word_times(ln)), default=99)
        if fw > 0.5:
            fails.append(f"first word at {fw:.2f} s (max 0.5)")
        D = self.pages_data[0][1]
        ctx_on = [float(r[0]) for r in D.get("v") or [] if r[1] == "ctx" and r[2]]
        if not ctx_on or min(ctx_on) > 3.0:
            fails.append("no context strip by 3 s")
        punches = sorted(float(p["t"]) for p in self.plan.punches if p.get("beat_mark"))
        last_word = max((w["said"] for ln in self.lines for w in word_times(ln)), default=0.0)
        pts = [0.0] + punches + [last_word]
        gaps = [(b - a, a) for a, b in zip(pts, pts[1:])]
        worst = max(gaps) if gaps else (0, 0)
        if worst[0] > 13.0:
            fails.append(f"{worst[0]:.1f} s without a punchline (from {worst[1]:.1f} s)")
        elif worst[0] > 10.0:
            warns.append(f"{worst[0]:.1f} s without a punchline (from {worst[1]:.1f} s)")
        if any(ln["speaker"] == "host" for ln in self.lines):
            fails.append("the host is in the short")
        beats = [(m_["ke"], m_["ke"] + float(m_.get("beat") or 0.4) + 0.1) for m_ in self._marks()]
        from .lipsync import strip_tags
        words = sorted((w["start"], w["said"]) for ln in self.lines for w in word_times(ln))
        for ln in self.lines:  # a chuckle or a sigh is sound, not dead air
            al = ln.get("alignment") or {}
            if al.get("chars"):
                for tag, a_, b_ in strip_tags(al)[3]:
                    if tag.lower() not in ("pause",):
                        words.append((float(ln["start"]) + a_, float(ln["start"]) + b_))
        words.sort()
        dead = []
        for (a0, a1), (b0, b1) in zip(words, words[1:]):
            gap = b0 - a1
            if gap > 0.8 and not any(a1 - 0.15 <= k0 <= b0 for k0, _ in beats):
                dead.append(f"{gap:.2f} s @ {a1:.1f}")
        if dead:
            fails.append(f"dead air: {dead[:3]}")
        doc = self.docs[0] if self.docs else ""
        missing = []
        for sp in sorted({f for _, f in self.model.focus_ev if f and f != "host"}):
            i = doc.find(f'-pt-{sp}"')
            j = doc.find('class="pt"', i + 10) if i >= 0 else -1
            seg = doc[i:j if j > 0 else i + 20000] if i >= 0 else ""
            if 'class="model"' not in seg:
                missing.append(sp)
        if missing:
            fails.append(f"no model name on screen for {missing}")
        data = {"duration_s": round(self.T, 2), "first_word_s": round(fw, 2), "context_s": min(ctx_on) if ctx_on else None,
                "longest_without_punch_s": round(worst[0], 2), "dead_air": dead}
        if fails:
            return {"status": FAIL, "summary": "; ".join(fails + warns), **data}
        if warns:
            return {"status": WARN, "summary": "; ".join(warns), **data}
        return {"status": PASS, "summary": f"{self.T:.1f} s; first word {fw:.2f} s; context at {min(ctx_on):.1f} s; "
                                           f"a punchline every <= {worst[0]:.1f} s; no host; no dead air; model names "
                                           f"on every plate", **data}

    def check_comic_gates(self) -> dict:
        """The timing pass: every PUNCH gets a >= 300 ms beat (or a comeback fires), no two punch beats within 1.5 s,
        no FILLER / STAT sentence kept in a short, the name slam never on a punchline."""
        marks = self._marks()
        if not marks:
            return {"status": SKIP, "summary": "no beat map (not a tight edit)"}
        by_id = {ln["id"]: ln for ln in self.lines}
        order = [ln["id"] for ln in self.lines]
        fails = []
        for mk in marks:
            if mk["role"] != "PUNCH":
                continue
            if mk.get("at_end"):
                i = order.index(mk["line"])
                nxt = by_id[order[i + 1]] if i + 1 < len(order) else None
                gap = (float(nxt["start"]) - mk["ke"]) if nxt else 9.9
                if gap < 0.3 - 1e-3 and not (nxt and nxt["kind"] == "comeback"):
                    fails.append(f"{mk['line']}: {gap * 1000:.0f} ms after the punch")
            elif float(mk.get("beat") or 0.0) < 0.3 - 1e-3:
                fails.append(f"{mk['line']}: {float(mk.get('beat') or 0) * 1000:.0f} ms beat")
        ends = [m_["ke"] for m_ in marks]
        close = [f"{a:.2f}/{b:.2f}" for a, b in zip(ends, ends[1:]) if b - a < 1.5]
        if close:
            fails.append(f"punch beats within 1.5 s: {close[:3]}")
        if (self.rd.get("short") or {}).get("tight"):
            bad_roles = []
            for ln in self.rd["timeline"]:
                b = ln.get("beats") or {}
                for i in b.get("keep") or []:
                    if b["labels"][i]["role"] in ("FILLER", "STAT"):
                        bad_roles.append(f"{ln['id']}#{i} {b['labels'][i]['role']}")
            if bad_roles:
                fails.append(f"FILLER/STAT kept: {bad_roles[:3]}")
        slams = [e for e in self.cues.get("events") or [] if e["type"] == "reveal"]
        coll = []
        for e in slams:
            st = float(e.get("slam_t") or e["t"])
            for m_ in marks:
                if m_["ks"] - 0.05 <= st <= m_["ke"] + float(m_.get("beat") or 0.3) - 0.01 or abs(st - m_["ke"]) < 0.4:
                    coll.append(f"{e.get('player')} @ {st:.2f}")
        if coll:
            fails.append(f"slam on a punchline: {coll[:3]}")
        n_p = sum(1 for m_ in marks if m_["role"] == "PUNCH")
        data = {"punches": n_p, "buttons": len(marks) - n_p}
        if fails:
            return {"status": FAIL, "summary": "; ".join(fails), **data}
        return {"status": PASS, "summary": f"{n_p} punches + {len(marks) - n_p} buttons: every punch gets its beat, "
                                           f"beats >= 1.5 s apart, no filler / stat kept, no slam on a punchline", **data}

    CHECKS = ("frames", "rig.face", "rig.glance", "mouth.open", "sync.render", "sync.voice", "captions", "word.cards",
              "gestures", "listeners", "shots", "first5", "short.gates", "comic.gates", "names")

    def run(self) -> dict:
        t0 = time.time()
        res = {}
        for name in self.CHECKS:
            fn = getattr(self, "check_" + name.replace(".", "_"))
            t = time.time()
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001  a QA bug must never sink a render
                r = {"status": WARN, "summary": f"check crashed: {type(e).__name__}: {str(e)[:160]}"}
            r["secs"] = round(time.time() - t, 1)
            res[name] = r
        counts = {k: sum(1 for r in res.values() if r["status"] == k) for k in (PASS, WARN, FAIL, SKIP)}
        rep = {"schema": "gmbench.qa/1", "clip": rel(self.cdir), "video": rel(self.video), "layout": self.layout,
               "dims": list(self.dims), "duration_s": round(self.T, 2), "build_s": self.build_s,
               "secs": round(time.time() - t0 + self.build_s, 1), "counts": counts, "checks": res,
               "sheets": sorted(rel(p) for p in self.out.glob("*.png"))}
        (self.out / "qa.json").write_text(json.dumps(rep, indent=1))
        return rep


def print_report(rep: dict, log=print) -> None:
    c = rep["counts"]
    log(f"qa: {rep['clip']} ({rep['layout']} {rep['dims'][0]}x{rep['dims'][1]}, {rep['duration_s']:.1f}s; "
        f"{rep['secs']:.0f}s of checks)")
    for name, r in rep["checks"].items():
        log(f"  {r['status']:4s}  {name:11s} {r['summary']}")
    log(f"  => {c['FAIL']} FAIL, {c['WARN']} WARN, {c['PASS']} PASS, {c['SKIP']} SKIP; sheets + qa.json in "
        f"{rep['clip']}/qa/")


def clip_dirs_for(args) -> list[Path]:
    """--clip DIR|MP4|out-relative name (repeatable); default: the command's own output directory."""
    out = []
    for c in (args.clip or []):
        p = Path(c)
        for cand in (p, OUT / c, ROOT / c, Path.cwd() / c):
            if cand.exists():
                out.append(cand.parent if cand.suffix == ".mp4" else cand)
                break
        else:
            raise SystemExit(f"qa: no clip at {c}")
    return out


def run_qa(run: str, dirs: list[Path], log=print, whisper: bool = True, names: bool = False) -> list[dict]:
    reps = []
    for d in dirs:
        rep = ClipQA(run, d, log=log, whisper=whisper, names=names).run()
        print_report(rep, log)
        reps.append(rep)
    return reps


def _stamps_main(paths: list[str]) -> None:
    from .pronounce import word_stamps
    print(json.dumps({p: [list(x) for x in word_stamps(Path(p))] for p in paths}))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stamps":
        _stamps_main(sys.argv[2:])
