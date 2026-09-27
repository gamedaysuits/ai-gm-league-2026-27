"""Per-short packaging and the beat map.

  <clip>/beatmap.md + beatmap.json   why the edit is timed the way it is: every sentence of every GM line with its role
                                     (SETUP / PUNCH / BUTTON / PICK / FILLER / STAT), kept or cut (and why), the kicker,
                                     and the timing (the beat, the reaction cut, the room reaction)
  <clip>/short-<name>.package.json   3 truthful titles, a description with the disclosure, hashtags, the thumbnail
  <clip>/short-<name>.package.txt    the same, ready to paste
  <clip>/short-<name>.thumb.png      the cold open's close-up with its kicker, big

Every title is built from what is actually on screen: who spoke, to whom, about which pick, their own words.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import tags as T
from .paths import SHOW, cache_dir, rel

DISCLOSURE = "AI-generated voices and avatars. Every line is the model's own words, edited for length."


def _model(L, tid: str | None) -> str:
    t = L.teams.get(tid or "")
    if not t:
        return "the Commissioner"
    return "the control bot" if t.is_bot else t.display


def _gm(L, tid: str | None) -> str:
    t = L.teams.get(tid or "")
    if not t:
        return ""
    return "the robot" if t.is_bot else (t.gm_name or t.display)


def _surname(name: str) -> str:
    return name.split()[-1] if name else ""


def write_beatmap(odir: Path, rd: dict, cues: dict, plan, L) -> Path:
    cut_reason = {}
    for tr in rd.get("trims") or []:
        cut_reason.setdefault(tr["id"], {})[tr.get("removed")] = tr.get("reason")
    rows_json = []
    punches = {}
    for p in plan.punches if plan else []:
        punches.setdefault(p["line"], []).append(p)
    placed = {ln["id"]: ln for ln in cues.get("lines") or []}
    sfx = [e for e in cues.get("events") or [] if e["type"] == "punch_beat"]
    out = [f"# Beat map: {odir.name} ({float(cues['duration_s']):.1f} s)", "",
           "Roles by a non-league labeller (validated against the sentences; rules as fallback). Only whole sentences "
           "are kept or cut; the words are the model's own. Timing: a micro-beat before a PUNCH unless the model wrote "
           "its own [pause]; a laugh beat after a PUNCH / BUTTON sized to its spice; the cut to the target's face 80 ms "
           "after the kicker; the room reacts in the beat.", ""]
    for ln in rd["timeline"]:
        b = ln.get("beats")
        if not b:
            continue
        pl = placed.get(ln["id"], {})
        t0 = float(pl.get("start") or 0.0)
        head = f"## [{ln['id']}] {_gm(L, ln['speaker'])} ({_model(L, ln['speaker'])})"
        if ln.get("addressed_to"):
            head += f" to {_gm(L, ln['addressed_to'])} ({_model(L, ln['addressed_to'])})"
        out += [head, f"*{ln['kind']}, starts at {t0:.2f} s; labels: {b.get('label_source') or 'rules'}*", "",
                "| # | role | edit | sentence | timing |", "|---|---|---|---|---|"]
        marks = ((pl.get("beats") or {}).get("marks") or [])
        mi = 0
        for i, (part, lab) in enumerate(zip(b["parts"], b["labels"])):
            disp = T.strip(part)
            kept = i in b["keep"]
            role = lab["role"] + (f" (spice {lab.get('spice')})" if lab["role"] in ("PUNCH", "BUTTON") else "")
            if lab.get("callback"):
                role += " · callback"
            if lab.get("tag"):
                role += " · tag"
            if lab.get("topper"):
                role += " · topper"
            text = disp
            timing = ""
            keep_ = b["keep"]
            with_tag = (lab["role"] == "PUNCH" and kept and (i + 1) in keep_ and (i + 1) < len(b["labels"])
                        and b["labels"][i + 1].get("tag"))
            if with_tag:
                timing = "one unit with its tag (a breath, then ONE laugh beat) ↓"
            elif lab["role"] in ("PUNCH", "BUTTON") and kept and mi < len(marks):
                mk = marks[mi]
                mi += 1
                words = disp.split()
                n = int(mk.get("kicker_words") or lab.get("kicker") or 3)
                text = " ".join(words[:-n]) + (" " if len(words) > n else "") + "**" + " ".join(words[-n:]) + "**"
                ke = t0 + float(mk["kicker_end"])
                cut = next((p for p in punches.get(ln["id"], []) if abs(float(p["kicker_end"]) - ke) < 0.05), None)
                room = next((e for e in sfx if abs(float(e["t"]) - ke) < 0.05), None)
                timing = (f"kicker ends {ke:.2f} s · beat {float(mk.get('beat') or 0) * 1000:.0f} ms"
                          + (" (the mix's gap)" if mk.get("at_end") else "")
                          + (f" · cut to {_gm(L, cut['listener'])} at {float(cut['t']):.2f} s" if cut and cut.get("listener") else "")
                          + (f" · room: {'cheer' if room.get('praise') else ('ohhh + laugh' if int(room.get('spice') or 1) >= 3 else 'laugh')}" if room else ""))
            edit = "KEEP" if kept else ("(not in the cold open)" if ln["kind"] == "hook" else
                                        f"cut ({(cut_reason.get(ln['id'], {}).get(disp) or 'edit').replace('tight edit: ', '')})")
            out.append(f"| {i} | {role} | {edit} | {text.replace('|', '/')} | {timing} |")
            rows_json.append({"line": ln["id"], "i": i, "role": lab["role"], "kept": kept, "sentence": disp,
                              "label": lab, "timing": timing})
        out.append("")
    p = odir / "beatmap.md"
    p.write_text("\n".join(out))
    (odir / "beatmap.json").write_text(json.dumps(rows_json, indent=1, ensure_ascii=False))
    return p


def archivo_ttf() -> Path | None:
    """A static Archivo Black-weight TTF for the thumbnail (decompressed once from the show's woff2)."""
    out = cache_dir("fonts") / "archivo-900.ttf"
    if out.exists():
        return out
    src = SHOW / "assets" / "fonts" / "archivo-latin-standard-normal.woff2"
    code = ("import sys\nfrom fontTools.ttLib import TTFont\nf=TTFont(sys.argv[1])\n"
            "if 'fvar' in f:\n from fontTools.varLib.instancer import instantiateVariableFont\n"
            " axes={a.axisTag:a for a in f['fvar'].axes}\n loc={'wght':900}\n"
            " f=instantiateVariableFont(f,{k:v for k,v in loc.items() if k in axes})\n"
            "f.flavor=None\nf.save(sys.argv[2])\n")
    subprocess.run(["uv", "run", "-q", "--with", "fonttools", "--with", "brotli", "python", "-c", code, str(src), str(out)],
                   capture_output=True, text=True)
    return out if out.exists() else None


def thumbnail(video: Path, t: float, text: str, top: str, out: Path, sub: str = "") -> Path | None:
    """A clean 1080x1920 thumbnail: the speaker's close-up (grabbed before any pop / card lands on it, scaled with
    nearest-neighbour so the pixel art stays crisp), the AI-GENERATED bug, the matchup chip and the kicker, big."""
    from PIL import Image, ImageDraw, ImageFont
    tmp = out.with_suffix(".frame.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "crop=1040:780:20:230", str(tmp)], check=True)
    face = Image.open(tmp).convert("RGB")  # the speaker AND the buddy on the reaction cam, 1:1 (crisp pixels)
    tmp.unlink(missing_ok=True)
    im = Image.new("RGB", (1080, 1920), (14, 27, 77))
    glow = Image.new("RGB", (1080, 1920), (40, 64, 150))
    mask = Image.new("L", (1080, 1920), 0)
    ImageDraw.Draw(mask).ellipse((-200, 200, 1280, 1500), fill=90)
    im = Image.composite(glow, im, mask)
    im.paste(face, (20, 420))
    d = ImageDraw.Draw(im)
    ttf = archivo_ttf()

    def font(sz):
        try:
            return ImageFont.truetype(str(ttf), sz) if ttf else ImageFont.truetype(
                "/System/Library/Fonts/Supplemental/Arial Black.ttf", sz)
        except OSError:
            return ImageFont.load_default()
    fb = font(34)
    bw = d.textlength("AI-GENERATED", font=fb)
    d.rounded_rectangle((40, 44, 40 + 72 + bw + 24, 106), 14, fill=(227, 36, 2))
    d.ellipse((58, 67, 74, 83), fill=(239, 240, 245))
    d.text((88, 55), "AI-GENERATED", font=fb, fill=(255, 255, 255))
    ft = font(80)
    while d.textlength(top, font=ft) > 1000 and ft.size > 40:
        ft = font(ft.size - 4)
    tw = d.textlength(top, font=ft)
    d.rounded_rectangle(((1080 - tw) / 2 - 28, 150, (1080 + tw) / 2 + 28, 150 + ft.size + 34), 18, fill=(227, 36, 2))
    d.text(((1080 - tw) / 2, 164), top, font=ft, fill=(255, 255, 255))
    words = text.upper().split()
    f = font(160)
    lines = []
    while True:
        lines, cur = [], ""
        for w in words:
            cand = (cur + " " + w).strip()
            if d.textlength(cand, font=f) > 1000 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
        if len(lines) <= 2 or f.size <= 90:
            break
        f = font(f.size - 10)
    y = 1290
    for ln_ in lines:
        wdt = d.textlength(ln_, font=f)
        d.text(((1080 - wdt) / 2, y), ln_, font=f, fill=(255, 255, 255), stroke_width=14, stroke_fill=(227, 36, 2))
        y += int(f.size * 1.08)
    if sub:
        fs = font(34)
        sw = d.textlength(sub, font=fs)
        d.text(((1080 - sw) / 2, 1820), sub, font=fs, fill=(201, 207, 230))
    im.save(out)
    return out


def write_package(odir: Path, name: str, rd: dict, cues: dict, plan, L, video: Path) -> dict:
    lines = [ln for ln in cues.get("lines") or []]
    hook = next((ln for ln in lines if ln["kind"] == "hook"), None) or (lines[0] if lines else None)
    picks = {p.pick_no: p for p in L.picks}
    a, b = (hook or {}).get("speaker"), (hook or {}).get("addressed_to")
    pk = picks.get(((hook or {}).get("card") or {}).get("pick_no"))
    target_pick = None
    if b:
        target_pick = next((p for p in L.picks if p.team == b and p.pick_no in (rd.get("short") or {}).get("picks", [])),
                           None) or next((p for p in L.picks if p.team == b and pk and p.pick_no < pk.pick_no), None)
    quote = (hook or {}).get("display_text") or ""
    marks = ((hook or {}).get("beats") or {}).get("marks") or []
    kicker = ""
    if marks and hook:
        from .captions import word_times
        mk = marks[-1]
        ws = [w for w in word_times(hook) if float(hook["start"]) + mk["kicker_start"] - 0.03 <= w["start"]
              <= float(hook["start"]) + mk["kicker_end"]]
        kicker = " ".join(w["w"] for w in ws)
        while len(kicker.split()) > 1 and kicker.split()[0].lower() in ("is", "at", "to", "of", "in", "on", "and", "like"):
            kicker = " ".join(kicker.split()[1:])
    n_ai = sum(1 for t in L.teams.values() if not t.is_bot)
    A, B = _model(L, a), _model(L, b)
    titles = []
    if b and target_pick:
        titles.append(f"{A} roasts {B}'s {_surname(target_pick.player_name)} pick")
    elif b:
        titles.append(f"{A} roasts {B} at the AI fantasy hockey draft")
    titles.append(f"\u201c{quote}\u201d \u2014 {A}, AI fantasy hockey draft")
    titles.append(f"{n_ai} AI models held a fantasy hockey draft party. Here's what they said about each other's picks.")
    nos = (rd.get("short") or {}).get("picks") or []
    took = [f"#{n} {_gm(L, picks[n].team)} ({_model(L, picks[n].team)}) took {picks[n].player_name}" for n in nos if n in picks]
    desc = (f"{n_ai} AI models run a fantasy hockey league, and this is their draft party. "
            + ("; ".join(took) + ". " if took else "")
            + DISCLOSURE + " Every GM wears a real Game Day Suit: gamedaysuits.ca")
    tags = ["#fantasyhockey", "#hockey", "#AI", "#LLM", "#draftparty", "#GameDaySuits"]
    for sp in sorted({ln["speaker"] for ln in lines}):
        t = L.teams.get(sp)
        if t and not t.is_bot:
            tag = "#" + re.sub(r"[^A-Za-z0-9]", "", t.display.split()[0].split("-")[0])
            if tag not in tags:
                tags.append(tag)
    thumb = None
    if hook and marks:
        t_clean = float(hook["start"]) + float(marks[-1]["punch_start"]) + 0.3  # mouth moving, no pop yet
        t_clean = min(t_clean, float(hook["start"]) + float(marks[-1]["kicker_start"]) - 0.05)
        short = lambda tid: (L.teams[tid].display.split()[0].split("-")[0].upper() if tid in L.teams and not L.teams[tid].is_bot
                             else "THE ROBOT")  # noqa: E731
        top = f"{short(a)} ROASTS {short(b)}" if b else short(a)
        thumb = thumbnail(video, t_clean, kicker or quote, top, odir / f"short-{name}.thumb.png",
                          sub=f"{n_ai} AI MODELS · 1 FANTASY HOCKEY LEAGUE")
    pkg = {"titles": titles, "description": desc, "hashtags": tags, "thumbnail": rel(thumb) if thumb else None,
           "disclosure": DISCLOSURE, "video": rel(video)}
    (odir / f"short-{name}.package.json").write_text(json.dumps(pkg, indent=1, ensure_ascii=False))
    txt = ["TITLES", *[f"  {i + 1}. {t}" for i, t in enumerate(titles)], "", "DESCRIPTION", desc, "", "HASHTAGS",
           " ".join(tags), "", f"THUMBNAIL: {rel(thumb) if thumb else '-'}"]
    (odir / f"short-{name}.package.txt").write_text("\n".join(txt) + "\n")
    return pkg



# ------------------------------------------------------------------------------------------ YouTube chapters
def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def write_chapters(odir: Path, stem: str, cues: dict, L) -> tuple[Path, Path]:
    """YouTube chapters for the complete show (one per round + the Report Card, Intro at 0:00) and the description
    sidecar with the disclosure. Times come from the mix (where each segment actually starts)."""
    import re as _re
    chapters: list[tuple[float, str]] = [(0.0, "Intro")]
    for sg in cues.get("segments") or []:
        m = _re.fullmatch(r"round_(\d+)", str(sg.get("id")))
        if m:
            chapters.append((float(sg["start"]), f"Round {m.group(1)}"))
        elif sg.get("id") == "report_card":
            chapters.append((float(sg["start"]), "Report Card + the Delusion Index"))
    keep: list[tuple[float, str]] = []
    for t, name in chapters:  # YouTube: each chapter >= 10 s
        if keep and t - keep[-1][0] < 10.0:
            continue
        keep.append((t, name))
    lines = [f"{_ts(t)} {name}" for t, name in keep]
    n_ai = sum(1 for t in L.teams.values() if not t.is_bot)
    ch = odir / f"{stem}.chapters.txt"
    ch.write_text("\n".join(lines) + "\n")
    desc = odir / f"{stem}.description.txt"
    desc.write_text(
        f"DRAFT NIGHT — {n_ai} AI models draft a real fantasy hockey league (the complete show)\n\n"
        f"{n_ai} AI models, each running a team in a season-long fantasy hockey league, draft live — every pick, "
        f"every comeback, the round-break table talk, then the Report Card and the Delusion Index. The robot is "
        f"Autodraft, the control bot every AI has to beat.\n\n"
        f"AI-generated voices and avatars. Every GM line is the model's own words.\n\n"
        f"Chapters:\n" + "\n".join(lines) + "\n\n#AI #FantasyHockey #NHL #DraftNight #GMBench\n")
    return ch, desc
