# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0", "pillow>=10.0", "numpy>=1.26", "scipy>=1.11"]
# ///
"""GM avatar generator: base portrait + mouth/blink edits -> face-locked lip-flap frames.

Run from the repo root (uv installs the script's own dependencies):

  uv run media/avatars/generate.py --personas runs/live/exports/personas.json --style pixel --budget 6.0
  uv run media/avatars/generate.py --personas runs/md-rehearsal/exports/personas.json --teams opus grok deepseek --budget 3
  uv run media/avatars/generate.py --personas ... --teams host autodraft        # the two fixed characters
  uv run media/avatars/generate.py --personas ... --teams opus --reprocess        # redo face-lock only, no API calls
  uv run media/avatars/generate.py --sheet-only                                   # rebuild the contact sheet
  uv run media/avatars/generate.py --personas ... --rig --budget 25               # layered rigs (after base frames)

Per character: one base image (persona avatar_description + exact suit + house framing,
fabric swatch photo attached as a colour reference), then three edits of it (mouth slightly
open, mouth wide open, eyes closed) with strict lip-sync prompts. Each edit is QA'd for
drift; a bad one is retried once and the better attempt kept. Frames are face-locked so only
the mouth/eye region ever changes. Outputs (768x768 PNG) go to media/assets/avatars/<team>/:
base, mid, wide, blink (+ mid_blink, wide_blink for blinks mid-word), meta.json and raw/.
Every call's cost is logged; the run stops starting new work at --budget.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import shutil
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from dotenv import dotenv_values
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import facelock  # noqa: E402
import prompts  # noqa: E402
import sheet  # noqa: E402

ROOT = HERE.parents[1]
ENV_PATH = ROOT / ".env"
DEFAULT_OUT = ROOT / "media" / "assets" / "avatars"
DEFAULT_FABRICS = ROOT / "data" / "fabrics.json"
DEFAULT_SWATCH_ROOT = Path.home() / "local projects" / "Game Day Suits Website" / "game-day-suits" / "public"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
GEN_URL = "https://openrouter.ai/api/v1/generation"
PRIMARY = "google/gemini-3-pro-image"
FALLBACK = "openai/gpt-5.4-image-2"
HOLD = {PRIMARY: 0.15, FALLBACK: 0.30}  # per-call hold against the budget (Nano Banana Pro bills $0.138-0.142)
QA_MODELS = ("google/gemini-3-flash-preview", "google/gemini-3.8-flash")  # cheap vision check (~$0.001);
# 3.8-flash is a thinking model: at small max_tokens its JSON comes back truncated, so it is only the fallback
QA_HOLD = 0.02
EDITS = facelock.EDITS
PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


# ============================================================================ budget
class Budget:
    def __init__(self, cap: float):
        self.cap, self.spent, self.held = cap, 0.0, 0.0
        self._lock = threading.Lock()

    def hold(self, amount: float) -> bool:
        with self._lock:
            if self.spent + self.held + amount > self.cap + 1e-9:
                return False
            self.held += amount
            return True

    def settle(self, held: float, actual: float) -> None:
        with self._lock:
            self.held -= held
            self.spent += actual

    def remaining(self) -> float:
        with self._lock:
            return self.cap - self.spent - self.held


# ============================================================================ OpenRouter
def _key() -> str:
    key = dotenv_values(ENV_PATH).get("OPENROUTER_API_KEY")
    if not key:
        sys.exit(f"OPENROUTER_API_KEY missing from {ENV_PATH}")
    return key


def _fetch_cost(client: httpx.Client, headers: dict, gen_id: str) -> float | None:
    for delay in (2, 4, 8):
        time.sleep(delay)
        try:
            r = client.get(GEN_URL, params={"id": gen_id}, headers=headers)
            if r.status_code == 200 and r.json().get("data", {}).get("total_cost") is not None:
                return float(r.json()["data"]["total_cost"])
        except (httpx.HTTPError, ValueError):
            pass
    return None


def call_model(model: str, content: list, budget: Budget, image: bool = True, image_size: str | None = None) -> dict:
    """One chat-completions call (image output, or text for QA). Never raises on API errors.
    image_size: "1K" (default) | "2K" (same price on gemini-3-pro-image: used by the rig stage's 2x2 sheets)."""
    hold = HOLD.get(model, 0.30) if image else QA_HOLD
    if not budget.hold(hold):
        return {"ok": False, "error": "budget", "cost": 0.0, "model": model}
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
               "X-Title": "GDS AI GM League avatars"}
    body = {"model": model, "messages": [{"role": "user", "content": content}], "usage": {"include": True}}
    if image:
        body.update(modalities=["image", "text"], image_config={"aspect_ratio": "1:1", **({"image_size": image_size} if image_size else {})})
    else:
        body.update(max_tokens=1500, temperature=0)
    t0 = time.time()
    out: dict = {"ok": False, "cost": 0.0, "model": model}
    try:
        with httpx.Client(timeout=httpx.Timeout(360.0, connect=30.0)) as client:
            for attempt in (1, 2):
                try:
                    r = client.post(API_URL, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    out["error"] = f"network: {type(exc).__name__}"
                    if attempt == 1:
                        time.sleep(5)
                        continue
                    break
                if r.status_code in (429, 500, 502, 503, 504) and attempt == 1:
                    time.sleep(6)
                    continue
                try:
                    data = r.json()
                except ValueError:
                    data = {"error": r.text[:500]}
                if r.status_code != 200 or "choices" not in data:
                    out["error"] = json.dumps(data.get("error", data))[:600]
                    break
                usage = data.get("usage") or {}
                cost = usage.get("cost")
                if cost is None and data.get("id"):
                    cost = _fetch_cost(client, headers, data["id"])
                msg = data["choices"][0].get("message", {})
                images = msg.get("images") or []
                out.update(cost=float(cost or 0.0), cost_reported=cost is not None, generation_id=data.get("id"),
                           model_served=data.get("model"), usage=usage,
                           text=(msg.get("content") or "")[:1500] if isinstance(msg.get("content"), str) else None,
                           finish=data["choices"][0].get("finish_reason"))
                if not image:
                    out["ok"] = bool(out["text"])
                    if not out["ok"]:
                        out["error"] = "empty text"
                    break
                for img in images:
                    url = (img.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        head, b64 = url.split(",", 1)
                        out["bytes"] = base64.b64decode(b64)
                        out["ext"] = {"image/jpeg": "jpg", "image/webp": "webp"}.get(head[5:].split(";")[0], "png")
                        out["ok"] = True
                        break
                if not out["ok"]:
                    out["error"] = "no image in response"
                break
    finally:
        out["elapsed_s"] = round(time.time() - t0, 1)
        budget.settle(hold, out.get("cost", 0.0))
    return out


def call_image(model: str, content: list, budget: Budget, image_size: str | None = None) -> dict:
    return call_model(model, content, budget, image=True, image_size=image_size)


def data_url(path: Path) -> str:
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


# ============================================================================ personas
def load_people(personas_path: Path | None, fabrics_path: Path) -> tuple[dict, dict]:
    teams = {}
    if personas_path:
        data = json.loads(personas_path.read_text())
        teams = {t["team"]: t for t in (data.get("teams", data) if isinstance(data, dict) else data)}
    fabrics = {}
    if fabrics_path.exists():
        fabrics = {f["id"]: f for f in json.loads(fabrics_path.read_text())}
    return teams, fabrics


def resolve(team: str, teams: dict, fabrics: dict) -> tuple[dict, dict, dict | None]:
    if team in prompts.FIXED:
        entry = copy.deepcopy(prompts.FIXED[team])
    else:
        entry = copy.deepcopy(teams.get(team) or {})
    persona = entry.get("persona") or {}
    if not (persona.get("avatar_description") or "").strip():
        raise ValueError(f"{team}: no persona / avatar_description in the personas file")
    persona.setdefault(
        "franchise_name", " ".join(x for x in (persona.get("franchise_city"), persona.get("franchise_nickname")) if x)
    )
    fabric = entry.get("fabric") or fabrics.get((persona.get("suit") or {}).get("fabric_id", ""))
    return entry, persona, fabric


def swatch_bytes(fabric: dict | None, swatch_root: Path) -> bytes | None:
    """Centre crop of the cloth photo (no edges/labels), 512px JPEG."""
    if not fabric or not fabric.get("swatch"):
        return None
    p = swatch_root / fabric["swatch"].lstrip("/")
    if not p.exists():
        return None
    im = Image.open(p).convert("RGB")
    w, h = im.size
    cw, ch = int(w * 0.7), int(h * 0.7)
    im = im.crop(((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch))
    im.thumbnail((512, 512))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ============================================================================ per-team run
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_call(args, team: str, variant: str, res: dict, prompt: str, retry: str | None, file: str | None) -> dict:
    rec = {
        "ts": now(), "team": team, "style": args.style, "variant": variant, "retry": retry,
        "model": res.get("model"), "model_served": res.get("model_served"), "cost": round(res.get("cost", 0.0), 6),
        "cost_reported": res.get("cost_reported"), "elapsed_s": res.get("elapsed_s"),
        "generation_id": res.get("generation_id"), "ok": res.get("ok"), "error": res.get("error"),
        "text": res.get("text"), "file": file,
    }
    with PRINT_LOCK:
        with open(args.out / "costs.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        with open(args.out / team / "raw" / "calls.jsonl", "a") as f:
            f.write(json.dumps({**rec, "prompt": prompt}) + "\n")
    return rec


def generate(args, team: str, variant: str, model: str, content: list, prompt: str, budget: Budget,
             retry: str | None = None) -> dict | None:
    raw = args.out / team / "raw"
    res = call_image(model, content, budget)
    file = None
    if res["ok"]:
        n = 1
        while any(raw.glob(f"{args.style}_{variant}_{n}.*")):
            n += 1
        path = raw / f"{args.style}_{variant}_{n}.{res['ext']}"
        path.write_bytes(res["bytes"])
        file = path.name
    rec = record_call(args, team, variant, res, prompt, retry, file)
    status = f"saved raw/{file}" if file else f"FAILED ({res.get('error')})"
    log(f"  [{team}] {variant:5s}{' retry:' + retry if retry else ''} {model.split('/')[-1]} "
        f"${res.get('cost', 0):.3f} {res.get('elapsed_s', 0):.0f}s -> {status}   (run total ${budget.spent:.2f})")
    if not file:
        return None
    return {"file": file, "model": model, "cost": rec["cost"], "retry": retry, "elapsed_s": rec["elapsed_s"],
            "generation_id": rec["generation_id"], "ts": rec["ts"]}


def _parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip()
    if "{" in t and "}" in t:
        t = t[t.index("{") : t.rindex("}") + 1]
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def vision_check(args, team: str, path: Path, kind: str, budget: Budget) -> dict | None:
    """Cheap VLM look at one frame: mouth closed / open amount, eyes open, any text or logos."""
    if args.no_check:
        return None
    content = [{"type": "image_url", "image_url": {"url": data_url(path)}},
               {"type": "text", "text": prompts.qa_prompt(kind)}]
    for model in QA_MODELS:
        res = call_model(model, content, budget, image=False)
        parsed = _parse_json(res.get("text")) if res.get("ok") else None
        record_call(args, team, "qa", {**res, "text": (res.get("text") or "")[:300]}, prompts.qa_prompt(kind), None, path.name)
        if parsed is not None:
            parsed.update(model=model, cost=round(res.get("cost", 0.0), 6), file=path.name)
            log(f"  [{team}] check {path.name}: mouth_closed={parsed.get('mouth_closed')} "
                f"open={parsed.get('mouth_open_amount')} eyes_open={parsed.get('eyes_open')} "
                f"text_or_logo={parsed.get('text_or_logo')} ({parsed.get('notes', '')})")
            return parsed
    return None


def team_cost(raw: Path, style: str, since: str | None) -> float:
    total = 0.0
    log_path = raw / "calls.jsonl"
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            r = json.loads(line)
            if r.get("style") != style or (since is not None and r.get("ts", "") < since):
                continue
            f = r.get("file") or ""
            if str(r.get("variant", "")).startswith("pose_") or "_pose_" in f or f.endswith("_closed.png"):
                continue  # gesture poses are costed separately (meta.poses_cost)
            total += r.get("cost") or 0.0
    return round(total, 4)


def save_png(img: Image.Image, path: Path, style: str) -> None:
    if style == "pixel":
        p = img.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
        if np.array_equal(np.asarray(p.convert("RGB")), np.asarray(img.convert("RGB"))):
            p.save(path, optimize=True)
            return
    img.convert("RGB").save(path, optimize=True)


def persona_sig(persona: dict, fabric: dict | None) -> str:
    key = {k: persona.get(k) for k in ("avatar_description", "suit", "primary_color", "secondary_color", "gm_name")}
    key["fabric"] = (fabric or {}).get("id")
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def run_label(personas_path: Path | None) -> str:
    """runs/<name>/exports/personas.json -> <name>"""
    if not personas_path:
        return "fixed"
    parts = personas_path.resolve().parts
    return parts[parts.index("runs") + 1] if "runs" in parts and parts.index("runs") + 1 < len(parts) else personas_path.stem


def archive_team(args, team: str, label: str) -> Path:
    """A team's persona changed (e.g. a new Media Day): keep the old avatar for before/after, start fresh."""
    dst = args.out / "_archive" / label / team
    if dst.exists():
        dst = dst.with_name(f"{team}_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(args.out / team), str(dst))
    log(f"  [{team}] persona changed -> previous avatar archived to {dst.relative_to(args.out)}")
    return dst


def run_team(args, team: str, teams: dict, fabrics: dict, budget: Budget) -> dict:
    entry, persona, fabric = resolve(team, teams, fabrics)
    out = args.out / team
    raw = out / "raw"
    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    sig = persona_sig(persona, fabric)
    label = "fixed" if team in prompts.FIXED else run_label(args.personas)
    if meta.get("files") and meta.get("persona_sig", sig) != sig and not args.reprocess:
        archive_team(args, team, meta.get("personas_run") or "previous")
        meta = {}
    raw.mkdir(parents=True, exist_ok=True)
    if args.force or meta.get("style") != args.style:
        meta = {"started_at": now()}
    meta.update(persona_sig=sig, personas_run=label)
    attempts: dict[str, list] = meta.get("attempts") or {}
    chosen: dict[str, int] = meta.get("chosen") or {}

    swatch = None if args.no_swatch else swatch_bytes(fabric, args.swatch_root)
    base_text, kind = prompts.base_prompt(args.style, persona, fabric, with_swatch=swatch is not None)
    summary = {"team": team, "style": args.style, "face_type": kind, "status": "ok", "cost": 0.0}

    missing = [v for v in ("base", *EDITS) if not attempts.get(v)]
    if args.reprocess:
        if missing:
            return {**summary, "status": f"cannot reprocess: missing {missing}"}
    elif missing and budget.remaining() < len(missing) * HOLD.get(args.model, 0.30) - 1e-9:
        log(f"  [{team}] skipped: budget left ${budget.remaining():.2f} < {len(missing)} calls")
        return {**summary, "status": "skipped (budget)"}

    # ---------------- base
    if not attempts.get("base"):
        if swatch:
            (raw / "swatch.jpg").write_bytes(swatch)
        content = [{"type": "text", "text": base_text}]
        if swatch:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(swatch).decode()}})
        a = None
        for model in (args.model, args.model, FALLBACK):
            a = generate(args, team, "base", model, content, base_text, budget)
            if a or budget.remaining() < HOLD.get(model, 0.3):
                break
        if not a:
            return {**summary, "status": "failed: base image"}
        attempts["base"] = [a]
        chosen["base"] = 0
    # ---------------- base QA (vision model): no text/logos, mouth fully closed; one fix each
    swatch_part = ([{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(swatch).decode()}}]
                   if swatch else [])
    b = attempts["base"][chosen.get("base", 0)]
    if not args.reprocess and "check" not in b:
        b["check"] = vision_check(args, team, raw / b["file"], kind, budget)
    chk = b.get("check") or {}
    if not args.reprocess and chk.get("text_or_logo") is True and not any(a.get("regen") for a in attempts["base"]):
        log(f"  [{team}] base shows text/logo -> regenerating the base once")
        text2 = base_text + (" ABSOLUTELY NO TEXT OR LOGOS: no letters, numbers, crests, pins with marks, or signage "
                             "anywhere - not on the suit, tie, background or boards.")
        a = generate(args, team, "base", args.model, [{"type": "text", "text": text2}, *swatch_part], text2, budget, "text")
        if a:
            a["regen"] = True
            a["check"] = vision_check(args, team, raw / a["file"], kind, budget)
            attempts["base"].append(a)
            chosen["base"] = len(attempts["base"]) - 1
            for v in EDITS:  # edits of the old base no longer apply
                if attempts.get(v):
                    meta.setdefault("discarded", []).extend(attempts.pop(v))
                    chosen.pop(v, None)
            b, chk = a, a.get("check") or {}
    if not args.reprocess and chk.get("mouth_closed") is False and not b.get("fix"):
        log(f"  [{team}] base mouth is not closed -> close-mouth fix edit")
        text3 = prompts.close_prompt(args.style, kind)
        a = generate(args, team, "base", args.model,
                     [{"type": "image_url", "image_url": {"url": data_url(raw / b["file"])}}, {"type": "text", "text": text3}],
                     text3, budget, "close-mouth")
        if a:
            a.update(fix="close-mouth", source=b["file"])
            a["check"] = vision_check(args, team, raw / a["file"], kind, budget)
            attempts["base"].append(a)
            chosen["base"] = len(attempts["base"]) - 1
    base_file = raw / attempts["base"][chosen.get("base", 0)]["file"]

    # ---------------- edits (parallel)
    def edit(variant: str, retry: str | None = None) -> tuple[str, dict | None, str]:
        text = prompts.edit_prompt(args.style, kind, variant, retry)
        content = [{"type": "image_url", "image_url": {"url": data_url(base_file)}}, {"type": "text", "text": text}]
        return variant, generate(args, team, variant, args.model, content, text, budget, retry), text

    todo = [v for v in EDITS if not attempts.get(v)]
    if todo:
        with ThreadPoolExecutor(len(todo)) as ex:
            for v, a, _ in ex.map(edit, todo):
                if a:
                    attempts.setdefault(v, []).append(a)
                    chosen[v] = len(attempts[v]) - 1
    if any(not attempts.get(v) for v in EDITS):
        meta.update(attempts=attempts, chosen=chosen, style=args.style)
        meta_path.write_text(json.dumps(meta, indent=2))
        return {**summary, "status": "failed: edit missing (budget or API)"}

    def files() -> dict[str, Path]:
        return {v: raw / attempts[v][chosen[v]]["file"] for v in EDITS}

    # ---------------- QA + one retry per bad edit
    res = facelock.process(args.style, base_file, files(), allow_swap=False)
    bad = {v: res["metrics"][v]["retry"] for v in EDITS if res["metrics"][v]["bad"] and len(attempts[v]) < 2}
    if bad and not args.no_retry and not args.reprocess:
        for v, why in bad.items():
            log(f"  [{team}] {v} flagged: {'; '.join(res['metrics'][v]['reasons'])} -> retrying once")
        with ThreadPoolExecutor(len(bad)) as ex:
            results = list(ex.map(lambda kv: edit(kv[0], kv[1]), bad.items()))
        for v, a, _ in results:
            if not a:
                continue
            attempts[v].append(a)
            trial = {**files(), v: raw / a["file"]}
            alt = facelock.process(args.style, base_file, trial, allow_swap=False)
            old, new = res["metrics"][v], alt["metrics"][v]
            keep_new = facelock.score(new) < facelock.score(old)
            log(f"  [{team}] {v}: attempt 1 score {facelock.score(old):.2f} vs retry {facelock.score(new):.2f}"
                f" -> keeping {'retry' if keep_new else 'attempt 1'}")
            if keep_new:
                chosen[v] = len(attempts[v]) - 1
                res = alt
    final = facelock.process(args.style, base_file, files(), allow_swap=True)

    # ---------------- write outputs
    frames = {}
    for k, img in final["frames"].items():
        save_png(img, out / f"{k}.png", args.style)
        frames[k] = f"{k}.png"
    for v, img in final["unlocked"].items():
        save_png(img, raw / f"{args.style}_{v}_unlocked.png", args.style)
    save_png(facelock.overlay(final["frames"]["base"], final["masks"]), raw / f"{args.style}_masks.png", args.style)

    flags = []
    for v in EDITS:
        m = final["metrics"][v]
        if m["bad"]:
            flags.append(f"{v}: {'; '.join(m['reasons'])}")
    if final["swapped_mid_wide"]:
        flags.append("mid/wide swapped (the 'slightly open' edit opened wider than the 'wide' edit)")
    bchk = attempts["base"][chosen["base"]].get("check") or {}
    if bchk.get("mouth_closed") is False:
        flags.append("base: mouth may still be open (vision check)")
    if bchk.get("text_or_logo") is True:
        flags.append("base: text/logo detected (vision check) - review before publishing")
    if bchk.get("weapon_visible") is True:
        flags.append("base: weapon detected (vision check) - regenerate before publishing")

    # report-only vision check of the final (locked) frames: does the lip-flap read?
    frame_checks = meta.get("frame_checks") or {}
    signature = json.dumps({v: attempts[v][chosen[v]]["file"] for v in ("base", *EDITS)}, sort_keys=True)
    if not args.reprocess and not args.no_check and (meta.get("frame_checks_for") != signature or not frame_checks):
        with ThreadPoolExecutor(3) as ex:
            got = list(ex.map(lambda v: (v, vision_check(args, team, out / f"{v}.png", kind, budget)), EDITS))
        frame_checks = {v: c for v, c in got if c}
    fc = frame_checks
    if fc.get("mid", {}).get("mouth_open_amount") == 0:
        flags.append("mid: vision check reads the mouth as closed (flap may be subtle)")
    if fc.get("wide", {}).get("mouth_open_amount") == 0:
        flags.append("wide: vision check reads the mouth as closed")
    if fc.get("blink", {}).get("eyes_open") is True:
        flags.append("blink: vision check reads the eyes as open")

    cost = team_cost(raw, args.style, meta.get("started_at"))  # every billed call, incl. QA fallbacks
    meta.update(
        team=team, style=args.style, face_type=kind, generated_at=now(),
        gm_name=persona.get("gm_name", team), franchise_name=persona.get("franchise_name", ""),
        franchise_abbrev=persona.get("franchise_abbrev", ""), display=entry.get("display", team),
        llm=entry.get("model"), primary_color=persona.get("primary_color"), secondary_color=persona.get("secondary_color"),
        fabric={k: (fabric or {}).get(k) for k in ("id", "color_name", "pattern_desc", "color_hex", "summary")},
        swatch_reference=bool(swatch), base_prompt=base_text, attempts=attempts, chosen=chosen, cost_usd=cost,
        metrics=final["metrics"], swapped_mid_wide=final["swapped_mid_wide"], info=final["info"], flags=flags,
        base_check=bchk, frame_checks=frame_checks, frame_checks_for=signature,
        files=frames, image_model=attempts["base"][chosen["base"]]["model"],
    )
    meta_path.write_text(json.dumps(meta, indent=2))
    return {**summary, "cost": cost, "flags": flags, "metrics": final["metrics"],
            "retries": sum(max(0, len(attempts[v]) - 1) for v in EDITS)}


# ============================================================================ gesture poses (--poses)
POSE_ORDER = tuple(prompts.POSES)


def vision_compare(args, team: str, ref: Path, img: Path, prompt: str, budget: Budget) -> dict | None:
    """Two-image vision check: reference portrait vs a new pose (identity / outfit / face / pose)."""
    if args.no_check:
        return None
    content = [{"type": "image_url", "image_url": {"url": data_url(ref)}},
               {"type": "image_url", "image_url": {"url": data_url(img)}}, {"type": "text", "text": prompt}]
    for model in QA_MODELS:
        res = call_model(model, content, budget, image=False)
        parsed = _parse_json(res.get("text")) if res.get("ok") else None
        record_call(args, team, "qa", {**res, "text": (res.get("text") or "")[:300]}, prompt, None, img.name)
        if parsed is not None:
            parsed.update(model=model, cost=round(res.get("cost", 0.0), 6), file=img.name)
            log(f"  [{team}] check {img.name}: character={parsed.get('same_character')} outfit={parsed.get('same_outfit')} "
                f"face={parsed.get('face_visible')} pose={parsed.get('pose_ok')} mouth={parsed.get('mouth_open_amount')} "
                f"text={parsed.get('text_or_logo')} ({parsed.get('notes', '')})")
            return parsed
    return None


def pose_failures(chk: dict | None, pose: str) -> list[str]:
    if not chk:
        return []
    fails = [k for k in ("same_character", "same_outfit", "same_style", "single_scene", "face_visible", "pose_ok")
             if chk.get(k) is False]
    if chk.get("text_or_logo") is True:
        fails.append("text_or_logo")
    if chk.get("weapon_visible") is True:
        fails.append("weapon")
    if pose in ("hype", "shock") and chk.get("mouth_open_amount") == 0:
        fails.append("mouth_not_open")
    return fails


def pose_rank(a: dict) -> tuple:
    """Lower is better: a pose with a weapon in it never beats one without."""
    return ("weapon" in a["fails"], len(a["fails"]))


def run_poses(args, team: str, teams: dict, fabrics: dict, budget: Budget) -> dict:
    entry, persona, fabric = resolve(team, teams, fabrics)
    out = args.out / team
    raw, pdir = out / "raw", out / "poses"
    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta.get("style") != args.style or not (meta.get("attempts") or {}).get("base"):
        return {"team": team, "status": "no base in this style yet (run without --poses first)", "cost": 0.0}
    pdir.mkdir(exist_ok=True)
    kind = meta.get("face_type") or prompts.face_type(persona["avatar_description"])
    base_file = raw / meta["attempts"]["base"][meta["chosen"]["base"]]["file"]
    poses_meta: dict = {} if args.force else (meta.get("poses") or {})
    discarded = meta.get("poses_discarded") or []
    for pose in args.redo_poses or []:  # regenerate these poses from scratch (old raw files stay in raw/)
        if pose in poses_meta:
            discarded.append({"pose": pose, **poses_meta.pop(pose)})
    todo = list(args.pose_list)  # generation steps below skip what already exists; outputs are rebuilt
    spent0 = budget.spent
    hold = HOLD.get(args.model, 0.30)
    if not args.reprocess:
        need = sum(2 if prompts.POSES[p]["talk"] and not args.skip_closed else 1
                   for p in todo if not (poses_meta.get(p) or {}).get("attempts"))
        if need and budget.remaining() < need * hold - 1e-9:
            log(f"  [{team}] poses skipped: budget left ${budget.remaining():.2f} < {need} calls")
            return {"team": team, "status": "poses skipped (budget)", "cost": 0.0}

    def qa_prompt(pose: str) -> str:
        return prompts.pose_qa_prompt(kind, pose, persona)

    def make_pose(pose: str, retry: bool = False):
        text = prompts.pose_prompt(args.style, kind, pose, persona, retry)
        content = [{"type": "image_url", "image_url": {"url": data_url(base_file)}}, {"type": "text", "text": text}]
        a = generate(args, team, f"pose_{pose}", args.model, content, text, budget, "strict" if retry else None)
        if a:
            a["check"] = vision_compare(args, team, base_file, raw / a["file"], qa_prompt(pose), budget)
            a["fails"] = pose_failures(a["check"], pose)
        return pose, a

    def make_closed(pose: str, retry: str | None = None):
        pm = poses_meta[pose]
        open_file = raw / pm["attempts"][pm["chosen"]]["file"]
        region = prompts.FACE[kind]["mouth_region"]
        text = prompts.close_prompt(args.style, kind)
        if retry == "drift":
            text += prompts.RETRY["drift"].format(region=region)
        elif retry:
            text += prompts.RETRY_CLOSE.format(region=region)
        content = [{"type": "image_url", "image_url": {"url": data_url(open_file)}}, {"type": "text", "text": text}]
        a = generate(args, team, f"pose_{pose}_closed", args.model, content, text, budget, retry)
        if a:
            pair = facelock.lock_pair(args.style, open_file, raw / a["file"])
            a["metrics"] = pair["metrics"]
            locked = raw / (Path(a["file"]).stem + "_locked.png")
            save_png(pair["frames"]["closed"], locked, args.style)
            a["check"] = vision_check(args, team, locked, kind, budget)
        return pose, a

    def closed_problem(c: dict) -> str | None:
        """None if the chosen closed-mouth partner is fine, else the retry kind."""
        a = c["attempts"][c["chosen"]]
        chk = a.get("check") or c.get("check") or {}
        if a["metrics"]["bad"]:
            return a["metrics"]["retry"]
        return "weak" if chk.get("mouth_closed") is False else None

    def closed_rank(a: dict) -> tuple:
        still_open = (a.get("check") or {}).get("mouth_closed") is False
        return (still_open, facelock.score(a["metrics"]))

    if not args.reprocess:
        # 1) the poses (parallel), each vision-checked against the base; one stricter retry if it fails
        fresh = [p for p in todo if not (poses_meta.get(p) or {}).get("attempts")]
        if fresh:
            with ThreadPoolExecutor(len(fresh)) as ex:
                for pose, a in ex.map(make_pose, fresh):
                    pm = poses_meta.setdefault(pose, {"attempts": []})
                    if a:
                        pm["attempts"].append(a)
                        pm["chosen"] = len(pm["attempts"]) - 1
        serious = {"same_character", "same_outfit", "single_scene", "face_visible", "text_or_logo", "weapon"}

        def worth_retry(fails: list) -> bool:
            return bool(fails) and (not args.lean or bool(serious & set(fails)))

        bad = [p for p in todo if (poses_meta.get(p) or {}).get("attempts")
               and worth_retry(poses_meta[p]["attempts"][poses_meta[p]["chosen"]].get("fails") or [])
               and len(poses_meta[p]["attempts"]) < 2]
        if bad and not args.no_retry:
            for p in bad:
                log(f"  [{team}] pose {p} failed review {poses_meta[p]['attempts'][-1]['fails']} -> retrying once")
            with ThreadPoolExecutor(len(bad)) as ex:
                for pose, a in ex.map(lambda p: make_pose(p, True), bad):
                    if a:
                        pm = poses_meta[pose]
                        pm["attempts"].append(a)
                        if pose_rank(a) < pose_rank(pm["attempts"][pm["chosen"]]):
                            pm["chosen"] = len(pm["attempts"]) - 1
        # 2) closed-mouth partner for the talking poses (lip-flap in pose), drift-checked; one retry
        talk = [p for p in todo if prompts.POSES[p]["talk"] and (poses_meta.get(p) or {}).get("attempts")
                and not (poses_meta[p].get("closed") or {}).get("attempts") and not args.skip_closed]
        if talk:
            with ThreadPoolExecutor(len(talk)) as ex:
                for pose, a in ex.map(make_closed, talk):
                    if a:
                        poses_meta[pose]["closed"] = {"attempts": [a], "chosen": 0}
        redo = {p: closed_problem(poses_meta[p]["closed"]) for p in todo if (poses_meta.get(p) or {}).get("closed")
                and len(poses_meta[p]["closed"]["attempts"]) < 2}
        redo = {p: why for p, why in redo.items() if why and (not args.lean or why == "drift")}
        if redo and not args.no_retry:
            for p, why in redo.items():
                log(f"  [{team}] pose {p} closed-mouth partner: {why} -> retrying once")
            with ThreadPoolExecutor(len(redo)) as ex:
                results = list(ex.map(lambda kv: make_closed(kv[0], kv[1]), redo.items()))
            for pose, a in results:
                if a:
                    c = poses_meta[pose]["closed"]
                    first = c["attempts"][c["chosen"]]
                    first.setdefault("check", c.get("check"))
                    c["attempts"].append(a)
                    if closed_rank(a) < closed_rank(first):
                        c["chosen"] = len(c["attempts"]) - 1
                    log(f"  [{team}] pose {pose} closed: keeping attempt {c['chosen'] + 1}")

    # 3) write outputs: poses/<pose>.png (+ <pose>_closed.png, only the mouth differs)
    for pose in todo:
        pm = poses_meta.get(pose) or {}
        if not pm.get("attempts"):
            continue
        open_file = raw / pm["attempts"][pm["chosen"]]["file"]
        files = {}
        c = pm.get("closed") or {}
        if c.get("attempts"):
            pair = facelock.lock_pair(args.style, open_file, raw / c["attempts"][c["chosen"]]["file"])
            save_png(pair["frames"]["open"], pdir / f"{pose}.png", args.style)
            save_png(pair["frames"]["closed"], pdir / f"{pose}_closed.png", args.style)
            save_png(facelock.overlay(pair["frames"]["open"], {"mouth": pair["mask"], "eyes": Image.new("L", pair["mask"].size, 0)}),
                     raw / f"{args.style}_pose_{pose}_mask.png", args.style)
            c["metrics"] = pair["metrics"]
            c["check"] = c["attempts"][c["chosen"]].get("check") or c.get("check")
            if not args.reprocess and not args.no_check and not c["check"]:
                c["check"] = vision_check(args, team, pdir / f"{pose}_closed.png", kind, budget)
            files = {"open": f"poses/{pose}.png", "closed": f"poses/{pose}_closed.png"}
        else:
            single = facelock.process_single(args.style, open_file)
            save_png(single["frame"], pdir / f"{pose}.png", args.style)
            files = {"open": f"poses/{pose}.png"}
        pm["files"] = files
        pm["desc"] = prompts.pose_desc(kind, pose, persona)
    flags = []
    for pose in [p for p in POSE_ORDER if p in poses_meta or p in args.pose_list]:
        pm = poses_meta.get(pose) or {}
        if not pm.get("files"):
            flags.append(f"pose {pose}: missing")
            continue
        fails = pm["attempts"][pm["chosen"]].get("fails") or []
        if fails:
            flags.append(f"pose {pose}: review says {', '.join(fails)}")
        c = pm.get("closed") or {}
        if c.get("metrics", {}).get("bad"):
            flags.append(f"pose {pose} closed: {'; '.join(c['metrics']['reasons'])}")
        if (c.get("check") or {}).get("mouth_closed") is False:
            flags.append(f"pose {pose} closed: vision check says the mouth is still open")
    cost = round(meta.get("poses_cost", 0.0) + (budget.spent - spent0), 4)
    meta.update(poses=poses_meta, poses_cost=cost, poses_flags=flags, poses_discarded=discarded,
                poses_celebration=(persona.get("celebration") or None))
    meta_path.write_text(json.dumps(meta, indent=2))
    return {"team": team, "status": "ok", "cost": cost, "flags": flags, "face_type": kind,
            "poses": {p: (poses_meta.get(p) or {}).get("files") for p in args.pose_list}}


def update_manifest(out_root: Path) -> None:
    teams = {}
    for p in sorted(out_root.glob("*/meta.json")):
        m = json.loads(p.read_text())
        if not m.get("files"):
            continue
        teams[m["team"]] = {k: m.get(k) for k in ("style", "face_type", "gm_name", "franchise_name", "display",
                                                   "primary_color", "secondary_color", "cost_usd", "flags",
                                                   "generated_at", "image_model")}
        teams[m["team"]]["files"] = {k: f"{m['team']}/{v}" for k, v in m["files"].items()}
        teams[m["team"]]["metrics"] = {v: {k: m["metrics"][v][k] for k in ("outside_pct", "dropped_pct", "mask_pct", "strong_pct", "bad")}
                                       for v in EDITS}
        poses = {}
        for pose, pm in (m.get("poses") or {}).items():
            for kind_, f in (pm.get("files") or {}).items():
                poses[pose if kind_ == "open" else f"{pose}_closed"] = f"{m['team']}/{f}"
        if poses:
            teams[m["team"]]["poses"] = poses
            teams[m["team"]]["poses_cost_usd"] = m.get("poses_cost")
            teams[m["team"]]["poses_flags"] = m.get("poses_flags")
        rig_json = out_root / m["team"] / "rig" / "rig.json"  # layered rig (gds-rig/1), real or mock
        if rig_json.exists():
            r = json.loads(rig_json.read_text())
            teams[m["team"]].update(rig=f"{m['team']}/rig/rig.json", rig_kind=r.get("kind"),
                                    rig_status=(r.get("qa") or {}).get("status"))
    (out_root / "manifest.json").write_text(json.dumps({"updated": now(), "teams": teams}, indent=2))


# ============================================================================ rig stage (--rig)
def run_rigs(args) -> None:
    import rig as rig_mod
    import rig_build

    teams, _ = load_people(args.personas, args.fabrics)
    ids = args.teams or ["host", *[t for t, e in teams.items() if (e.get("persona") or {}).get("avatar_description")], "autodraft"]
    budget = Budget(args.budget)
    log(f"rigs: teams={' '.join(ids)} model={args.rig_model} budget=${args.budget:.2f} out={args.out}")
    results = []
    for team in ids:
        tdir = args.out / team
        meta_path = tdir / "meta.json"
        if not meta_path.exists() or not json.loads(meta_path.read_text()).get("files"):
            log(f"- {team}: no base frames yet (run the base stage first) - mock rig only")
            results.append({"team": team, "status": "no base", "cost": 0.0, "flags": []})
            continue
        for sheet_name in args.rig_redo:
            for f in (tdir / "rig" / "raw").glob(f"{sheet_name}_[0-9]*.*"):
                f.rename(f.with_name(f"discarded_{f.name}"))
        log(f"- {team}")
        try:
            r = rig_build.build_team(args, team, budget, args.rig_model)
        except Exception as exc:  # one broken character never stops the others
            import traceback
            traceback.print_exc()
            r = {"team": team, "status": f"error: {type(exc).__name__}: {exc}", "cost": 0.0, "flags": []}
            if not (tdir / "rig" / "rig.json").exists():
                rig_mod.build_mock(tdir)
        results.append(r)
        log(f"  [{team}] rig {r['status']}  ${r.get('cost', 0):.3f} this run (team rig total ${r.get('total_cost', 0):.3f})")
    for team in ids:  # the show always gets a rig: a mock from the old frames where no real rig could be made
        tdir = args.out / team
        if (tdir / "meta.json").exists() and json.loads((tdir / "meta.json").read_text()).get("files") \
                and not (tdir / "rig" / "rig.json").exists():
            rig_mod.build_mock(tdir)
            log(f"  [{team}] mock rig written (no real rig yet)")
    update_manifest(args.out)
    log("\nteam        status    frames  head==  cost(run)  flags")
    for r in results:
        log(f"{r['team']:11s} {r['status'][:9]:9s} {r.get('frames', 0):6d}  {str(r.get('head_identical', '-')):6s}  "
            f"${r.get('cost', 0):.3f}   {len(r.get('flags') or [])}")
        for f in (r.get("flags") or [])[:12]:
            log(f"{'':14s}! {f}")
    log(f"\nspent this run: ${budget.spent:.4f} of ${args.budget:.2f}   (all rig calls are in costs.jsonl as variant rig_*)")
    log("previews: media/assets/avatars/<team>/rig/preview.gif + preview.png")


# ============================================================================ cli
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--personas", type=Path, help="personas.json from a Media Day run")
    ap.add_argument("--style", choices=["pixel", "painted"], default="pixel")
    ap.add_argument("--teams", nargs="*", help="team ids (default: every persona + host + autodraft)")
    ap.add_argument("--budget", type=float, default=6.0, help="hard cap in USD for this invocation")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--fabrics", type=Path, default=DEFAULT_FABRICS)
    ap.add_argument("--swatch-root", type=Path, default=DEFAULT_SWATCH_ROOT)
    ap.add_argument("--model", default=PRIMARY)
    ap.add_argument("--no-swatch", action="store_true", help="don't attach the fabric photo as a reference")
    ap.add_argument("--no-retry", action="store_true")
    ap.add_argument("--no-check", action="store_true", help="skip the cheap vision-model checks")
    ap.add_argument("--force", action="store_true", help="regenerate even if this style already exists for a team")
    ap.add_argument("--reprocess", action="store_true", help="no API calls: redo face-lock/QA from saved raw images")
    ap.add_argument("--sheet-only", action="store_true", help="only rebuild the contact sheet")
    ap.add_argument("--poses", action="store_true", help="gesture-pose stage (needs the base frames; runs after them)")
    ap.add_argument("--pose-list", nargs="*", default=list(POSE_ORDER), choices=list(POSE_ORDER))
    ap.add_argument("--redo-poses", nargs="*", default=[], choices=list(POSE_ORDER),
                    help="throw away these poses for the listed teams and generate them again")
    ap.add_argument("--skip-closed", action="store_true", help="don't make closed-mouth partners for hype/point")
    ap.add_argument("--lean", action="store_true",
                    help="budget mode: retry poses only for identity/outfit/face/text failures, closed partners only for drift")
    ap.add_argument("--rig", action="store_true",
                    help="layered rig stage (media/assets/avatars/RIG_SCHEMA.md): face sheets + gesture sheets per team; "
                         "needs the base frames; idempotent and resumable (cached in <team>/rig/raw/)")
    ap.add_argument("--rig-model", default="pro", help="image model for the rig stage: pro | flash | <openrouter id>")
    ap.add_argument("--rig-redo", nargs="*", default=[],
                    help="discard these cached rig sheets for the listed teams and make them again "
                         "(silhouette face_mouth_a face_mouth_b face_eyes_a face_eyes_b body_a body_b body_c body_d)")
    args = ap.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.rig and not args.sheet_only:
        run_rigs(args)
    elif not args.sheet_only:
        teams, fabrics = load_people(args.personas, args.fabrics)
        ids = args.teams or ["host", *[t for t, e in teams.items() if (e.get("persona") or {}).get("avatar_description")], "autodraft"]
        budget = Budget(args.budget)
        log(f"avatars: style={args.style} teams={' '.join(ids)} budget=${args.budget:.2f} out={args.out}")
        results = []
        for team in ids:
            log(f"- {team}")
            try:
                r = run_team(args, team, teams, fabrics, budget)
                if args.poses and r.get("status") == "ok":
                    pr = run_poses(args, team, teams, fabrics, budget)
                    r["poses_status"], r["poses_cost"], r["poses_flags"] = pr["status"], pr.get("cost", 0.0), pr.get("flags") or []
                results.append(r)
            except ValueError as exc:
                log(f"  [{team}] skipped: {exc}")
                results.append({"team": team, "status": f"skipped: {exc}", "cost": 0.0})
        update_manifest(args.out)
        log("\nteam        face    cost    status / QA")
        for r in results:
            qa = ""
            if r.get("metrics"):
                qa = "  ".join(f"{v}: out {r['metrics'][v]['outside_pct']:.2f}% drop {r['metrics'][v]['dropped_pct']:.2f}%"
                               f"{' BAD' if r['metrics'][v]['bad'] else ''}" for v in EDITS)
            log(f"{r['team']:11s} {r.get('face_type', ''):7s} ${r.get('cost', 0):.3f}  {r['status']}  {qa}")
            for f in r.get("flags") or []:
                log(f"{'':28s}! {f}")
            if "poses_status" in r:
                log(f"{'':28s}poses: {r['poses_status']} (${r.get('poses_cost', 0):.3f})")
                for f in r.get("poses_flags") or []:
                    log(f"{'':28s}! {f}")
        log(f"\nspent this run: ${budget.spent:.4f} of ${args.budget:.2f}")
    path = sheet.build(args.out)
    log(f"contact sheet: {path}")


if __name__ == "__main__":
    main()
