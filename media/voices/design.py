#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "httpx>=0.27",
#   "python-dotenv>=1.0",
#   "numpy>=1.26",
#   "praat-parselmouth>=0.4.3",
#   "resemblyzer>=0.1.4",
#   "setuptools<70",
#   "faster-whisper>=1.0",
# ]
# ///
"""Cast one persistent ElevenLabs voice per GM (plus the host): TIMBRE from the card, ENERGY at render time.

Why: Voice Design bakes energy words into the voice itself. "Booming / roar / arena" cards all came out as the same
~250-390 Hz shouter, so the cast was indistinct. Now the base voice is the character's natural register and
texture; the yelling comes only from v3 tags ([shouting], [excited]...) and stability when lines are rendered.

Pipeline
  1. Casting spec per GM from its OWN card (voice_description + avatar + personality) via a cheap NON-competing
     model on OpenRouter (default cohere/command-a-plus; deterministic parsing as fallback): gender presentation,
     age, register, acceptable registers, texture, light regional flavour, one signature quality. Energy words are
     stripped; the design prompt is composed from the spec only (natural at rest, able to shout).
  2. Cast plan: registers are spread across the whole cast (host included). Where the cast clusters, the least-
     committed cards move one step within the registers they accept (e.g. baritone -> bass-baritone).
  3. Voice Design (eleven_ttv_v3) with the timbre prompt; preview = the GM's catchphrase read plainly + one own-words
     line with [shouting] (must lift >= MIN_LIFT). Pick by register match (median F0 of the plain part vs the band), then
     distinctness (speaker cosine of the plain part vs every voice already cast: <= --max-sim between GMs,
     <= --host-max-sim against the host), then expressiveness (how far the [excited] line lifts pitch/effort).
  4. One redesign if nothing passes; then the ElevenLabs Voice Library (/v1/shared-voices filtered by the spec,
     standard-rate voices only): top ~10 by the same register/distinctness checks on their preview audio.
  4b. ACCENT is a first-class casting attribute: the spec carries the accent the card describes, anchored on the
     GM's hometown (e.g. "rural Alberta (Canadian prairie) accent"); the design prompt opens with it; the Voice
     Library is searched Canadian-first and non-Canadian labels are dropped; the fit judge weighs accent heavily.
     A LISTENING check (audio-input model on OpenRouter) compares each real eleven_v3 render with a General
     American reference voice reading the SAME words, both orders; accent score 0-10, gate --accent-gate (6).
  5. Anchor: the preview text rendered with eleven_v3 at stability 0.3 (garble check -> 0.5 fallback) ->
     anchors/<team>.mp3 (-16 LUFS). voices.json records source (design|library), spec, plan, measurements.
     Idempotent (skip teams already cast unless --force); --delete-replaced removes superseded GMB-* voices.

usage (repo root; uv installs the pinned deps in an isolated script env):
  uv run media/voices/design.py --personas runs/<run>/exports/personas.json [--teams ...] [--dry-run]
        [--force] [--delete-replaced] [--budget 12000]
Secrets come from <repo>/.env (ELEVENLABS_API_KEY, OPENROUTER_API_KEY) and are never printed or logged.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent            # media/voices
ROOT = HERE.parents[1]                             # repo root
VOICES_JSON = HERE / "voices.json"
EMBEDDINGS_JSON = HERE / "embeddings.json"
ANCHORS = HERE / "anchors"
CASTING = HERE / "casting"
WORK = HERE / "work"
LOGS = HERE / "logs"
USAGE_LOG = LOGS / "usage.jsonl"
RUNS_LOG = LOGS / "runs.jsonl"
ENV_FILE = ROOT / ".env"

API = "https://api.elevenlabs.io"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DESIGN_MODEL = "eleven_ttv_v3"
TTS_MODEL = "eleven_v3"
CASTING_MODEL = "cohere/command-a-plus"          # per-GM specs: cheap and not a league competitor
PLAN_MODEL = "mistralai/mistral-small-3.2-24b-instruct"  # whole-cast plan: non-reasoning, JSON-reliable, non-competing
# Accent check: an audio-input model LISTENS. Absolute 0-10 "how Canadian" scores were unreliable (library test set
# AUC 0.68; a strict prompt called Alberta designs General American), but a PAIRWISE judgement of two voices reading
# the same words, both orders, picked the Alberta-designed voice 8/8 at confidence 9-10. So each candidate is judged
# against a General American reference voice reading its own line.
ACCENT_JUDGE_MODEL = "google/gemini-3.8-flash"
ACCENT_GATE = 6.0
AMERICAN_REFERENCE_ID = "wBXNqKUATyqu0RtYt25i"   # 'Adam' (Voice Library, General American): former host, kept as fallback
CANADIAN_ACCENT = re.compile(r"canad|alberta|saskatch|manitoba|prairie|maritime|ontario|qu[eé]bec|newfoundland|"
                             r"cape breton|nova scotia|new brunswick|british columbia|yukon|nunavut|northwest territories",
                             re.I)
# (command-a-plus spent its whole token budget reasoning on the 12-card cast plan and returned no content)
BASE_SETTINGS = {"similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}
STABILITY_LEVELS = (0.3,)                        # 0.0 garbled the shoutiest voices; 0.3 default, 0.5 fallback
FALLBACK_STABILITY = 0.5
OUTPUT_FORMAT = "mp3_44100_128"
ANCHOR_SEED = 20260928
DEFAULT_BUDGET = 12000
DEFAULT_MAX_SIM = 0.80        # GM vs GM (Resemblyzer cosine of the plain part)
DEFAULT_HOST_MAX_SIM = 0.75   # host vs any GM
REGISTER_TOL_ST = 2.0         # plain-read median F0 may sit this far outside the planned band
MAX_WER = 0.25
MAX_WER_RENDER = 0.2
# Character error rate (letters only, fillers out, names kept): prairie slang and nicknames ("Saucer it, eh" ->
# "Sausser-It-A") pushed WER to 0.47-0.69 on clean previews while CER stayed 0.12-0.25; babble and dropped lines
# still fail it. A clip is intelligible when EITHER WER or CER is under its gate.
MAX_CER = 0.25
FILLERS = {"eh", "uh", "um", "ah", "oh", "a", "hmm"}
LAUGH_TOKENS = {"ha", "haha", "hahaha", "heh", "hah", "ah", "oh", "ho", "hoo", "woo", "whoa"}

# The base voice is natural at rest, but it must be ABLE to go big: designing "at a relaxed volume" gave voices that
# barely moved on [shouting] (+0.4 to +1.5 semitones on real on-air calls). Describing the range and demonstrating
# it in the preview (plain read + one shouted own-words line) kept the resting register (e.g. 69-81 Hz for a
# bass-baritone) while the shout lifted +12 to +17 semitones.
RANGE_CLAUSE = ("Natural speaking voice with a wide dynamic range: relaxed and conversational at rest, capable of a "
                "full, powerful shout. Clear studio recording.")
MIN_LIFT = 6.0  # preview: shouted line vs plain read, semitones + effort dB/3; below this the voice can't go big
# The host is a Game-7 play-by-play voice: deep PA timbre at rest, the biggest lift in the cast on [shouting].
HOST_RANGE_CLAUSE = ("Warm, relaxed and conversational at rest; on big moments the same deep voice rises into a huge, "
                     "full-throated championship play-by-play call. Clear studio recording.")
HOST_MIN_LIFT = 12.0

REGISTERS = ["bass", "bass-baritone", "baritone", "high baritone", "tenor", "high tenor",
             "contralto", "alto", "mezzo-soprano", "soprano"]
# normal-speaking median F0 bands (Hz) per register
BANDS = {"bass": (70, 98), "bass-baritone": (82, 110), "baritone": (95, 128), "high baritone": (110, 145),
         "tenor": (125, 165), "high tenor": (145, 190), "contralto": (150, 190), "alto": (165, 205),
         "mezzo-soprano": (180, 225), "soprano": (200, 260)}
REGISTER_PHRASE = {"bass": "Deep bass", "bass-baritone": "Low bass-baritone", "baritone": "Baritone",
                   "high baritone": "Light, high baritone", "tenor": "Tenor", "high tenor": "High, light tenor",
                   "contralto": "Low contralto", "alto": "Alto", "mezzo-soprano": "Mezzo-soprano", "soprano": "Soprano"}
ENERGY_WORDS = re.compile(
    r"\b(booming|boom\w*|roar\w*|shout\w*|yell\w*|scream\w*|arena\w*|hype\w*|rapid[- ]fire|game[- ]?7|loud\w*|"
    r"explosive|explod\w*|detonat\w*|erupt\w*|bellow\w*|energetic|energy|high-energy|hyper\w*|throat-shredding|"
    r"rafters|barn|crowd\w*|fans|cranked|full-blown|full-chested|arena-shaking|arena-cracking|"
    r"hyped|excitable|excited|shock jock|pro-wrestling|wrestling)\b", re.I)

HOST = {
    "team": "host", "gm_name": "the Commissioner", "voice_name": "GMB-host",
    "brief": ("The Commissioner hosting the draft party: a deep, resonant Canadian hockey play-by-play voice with a "
              "western Canadian accent; energy comes from tags at render time, never from the base voice."),
    "spec": {"gender_presentation": "male", "age": "early 50s", "register": "bass-baritone",
             "register_commitment": "explicit", "acceptable_registers": ["bass", "bass-baritone"],
             "texture": ["resonant", "warm", "round", "polished"], "regional_flavour": "western Canadian accent",
             "accent": "western Canadian (Alberta) accent", "accent_region": "Alberta", "accent_strength": "noticeable",
             "accent_plain": "Alberta (western Canadian prairie) accent",
             "signature_quality": "classic Canadian hockey radio broadcaster clarity with a big, rich chest resonance"},
    "plain": ("Good evening, folks, and welcome out to the draft party. I'm the Commissioner. Every word you hear from "
              "a general manager tonight comes straight out of that model, no doubt about it."),
    "excited": "[shouting] SMOKY LAKE, YOU'RE ON THE CLOCK, BUD!",
    "library_search": ["sports announcer", "announcer", "play-by-play", "commentator", "broadcaster", "radio", "deep"],
}

META_SENTENCE = re.compile(r"PG-?13|hockey only|never personal|never the person", re.I)
# Library voices that imitate or are cloned from a real, named person or a specific game/film character are out
# (a first pass picked a clone of a named voice actor's famous video-game announcer).
REAL_PERSON = re.compile(r"voiced by|originally|impression|imitat\w*|inspired by|in the style of|sound-?alike|parody|"
                         r"tribute|celebrity|famous|legendary .* voice|\b(?:19|20)\d\d\b|®|™|tournament|darth|vader|"
                         r"clone of|cloned from|as heard in|the voice of|voice by|narrated by|by hey its|gotham|batman|joker", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
TAG_RE = re.compile(r"\[[^\[\]]{1,40}\]")

BOOK_KEY = "elevenlabs"  # media/gmbench_media/tts.py reads book["elevenlabs"][speaker] -> voice_id + settings
BOOK_DOC = ("Voice book for media/gmbench_media/tts.py. Speakers: 'host' plus league team ids. elevenlabs[speaker] -> "
            "voice_id + settings (eleven_v3), casting spec (timbre), source design|library and an anchor clip. Energy "
            "is NOT in these voices: render big moments with v3 tags ([shouting], [excited], [laughs]). Fallback "
            "engine: Fish S2.1 Pro via OpenRouter /audio/speech with input_references = [anchor.path as "
            "data:audio/mpeg;base64, anchor.text as transcript]. Written by media/voices/design.py.")


# ----------------------------------------------------------------------------------------- small utils
def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def env_key(name: str) -> str:
    key = (dotenv_values(ENV_FILE).get(name) or "").strip()
    if not key:
        raise SystemExit(f"{name} missing from {rel(ENV_FILE)}")
    return key


def load_key() -> str:
    return env_key("ELEVENLABS_API_KEY")


def scrub(text: str, *keys: str | None) -> str:
    for k in keys:
        if k:
            text = text.replace(k, "***")
    return text


def read_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def write_json_atomic(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)


def log_usage(rec: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG.open("a") as f:
        f.write(json.dumps({"ts": now(), **rec}, ensure_ascii=False) + "\n")


def load_book() -> dict:
    book = read_json(VOICES_JSON, {})
    if "voices" in book and BOOK_KEY not in book:  # early format {"voices": {...}}
        book[BOOK_KEY] = book.pop("voices")
    for k in ("engine", "tts_model", "usage"):
        book.pop(k, None)
    book = {"_doc": BOOK_DOC, "schema": "gmb-voices/3", "eleven_model": TTS_MODEL,
            **{k: v for k, v in book.items() if k not in ("_doc", "schema", "eleven_model")}}
    book.setdefault(BOOK_KEY, {})
    return book


def sentences(s: str | None) -> list[str]:
    return [x.strip() for x in SENT_SPLIT.split((s or "").strip()) if x.strip()]


def with_period(s: str) -> str:
    s = s.strip()
    return s if s[-1:] in ".!?…" else s + "."


def shout_case(s: str) -> str:
    """Upper-case the words for emphasis but leave any [v3 tags] untouched."""
    return "".join(p if p.startswith("[") else p.upper() for p in re.split(r"(\[[^\[\]]{1,40}\])", s))


def exclaim(s: str) -> str:
    s = s.strip().rstrip(".…")
    return s if s[-1:] in "!?" else s + "!"


def spoken(text: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", text)).strip()


def strip_energy(text: str) -> str:
    t = ENERGY_WORDS.sub("", text or "")
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = re.sub(r"([,;:])\s*([,;:.])", r"\2", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;:-")
    return t


def voice_name(team: str, gm_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9 .\-]", "", gm_name).strip()
    return f"GMB-{team}-{re.sub(r'\s+', ' ', clean)}"[:90]


def seed_for(team: str, attempt: int) -> int:
    return (int(hashlib.sha1(team.encode()).hexdigest()[:8], 16) + 7919 * (attempt - 1)) % 2_000_000_000


def st_dist(f0: float | None, band: tuple[float, float]) -> float:
    import math

    if not f0:
        return 99.0
    if f0 < band[0]:
        return 12 * math.log2(band[0] / f0)
    if f0 > band[1]:
        return 12 * math.log2(f0 / band[1])
    return 0.0


def st_from_center(f0: float | None, band: tuple[float, float]) -> float:
    import math

    return abs(12 * math.log2(f0 / math.sqrt(band[0] * band[1]))) if f0 else 99.0


# ------------------------------------------------------------------------------------ personas -> jobs
def no_label_colons(text: str) -> str:
    """eleven_v3 drops 'SOME WORDS:' like a script speaker label (seen on 2 of 13 anchors); use a comma instead."""
    return re.sub(r"\s*:\s+", ", ", text)


PLACEHOLDER = re.compile(r"\[(?:player|player name|name|pick|team|prospect)\]\s*,?\s*", re.I)
PLAIN_MIN_CHARS = 95  # ~6 s of plain read: enough speech for the listening accent check


def call_line(text: str | None) -> str | None:
    """The card's signature call as a clean shout line: no [Player] placeholders, tags or trailing 'select:'."""
    if not text:
        return None
    t = spoken(PLACEHOLDER.sub("", text))
    if "..." in t and len(t.rsplit("...", 1)[1].split()) >= 3:  # a drum-roll build-up: shout the payoff only
        t = t.rsplit("...", 1)[1]
    t = re.sub(r"^[\s,;:\u2014-]+", "", t)
    t = re.sub(r"\s*:\s*$", "", t).strip()
    return t[0].upper() + t[1:] if len(t) > 3 else None


def preview_parts(g: dict) -> tuple[str, str]:
    """(plain, excited): the catchphrase read plainly (tags stripped) + own first-person words (tagline, bio; the
    third-person trash-talk style last), then ONE shouted line: the GM's own signature call when the card has one.
    Only the GM's card words are used."""
    plain = f"I'm {g['gm_name']}."
    if g.get("catchphrase"):
        plain += " " + with_period(spoken(g["catchphrase"]))
    extra = [spoken(s) for s in sentences(g.get("tagline")) + sentences(g.get("bio")) + sentences(g.get("trash_talk_style"))
             if not META_SENTENCE.search(s) and len(spoken(s)) > 12]
    i, used = 0, set()
    while len(plain) < PLAIN_MIN_CHARS and i < len(extra):
        if len(plain) + len(extra[i]) <= 175:  # skip long bio sentences: every extra character is paid ~6 times
            plain += " " + with_period(extra[i])
            used.add(i)
        i += 1
    i = next((k for k in range(len(extra)) if k not in used), len(extra))
    big = call_line(g.get("signature_call")) or (extra[i] if i < len(extra) else
                                                 (spoken(g.get("catchphrase") or "") or g["gm_name"]))
    excited = f"[shouting] {shout_case(exclaim(big))}"
    while len(plain) + 1 + len(excited) < 100:  # Voice Design needs >= 100 chars
        plain += " " + with_period(spoken(g.get("catchphrase") or g["gm_name"]))
    return no_label_colons(plain), no_label_colons(excited)


def load_jobs(personas_path: Path, teams_filter: list[str] | None) -> tuple[list[dict], set[str]]:
    d = json.loads(personas_path.read_text())
    teams = d.get("teams", []) if isinstance(d, dict) else d
    models = {t.get("model") for t in teams if t.get("model")}
    jobs = []
    for t in teams:
        team = t.get("team")
        if not team or (teams_filter and team not in teams_filter):
            continue
        p = t.get("persona") or {}
        desc = (p.get("voice_description") or "").strip()
        if not p or not desc:
            warn(f"{team}: no Media Day persona / voice_description in {rel(personas_path)} -- skipped")
            continue
        g = {"team": team, "gm_name": (p.get("gm_name") or team).strip(), "catchphrase": p.get("catchphrase"),
             "trash_talk_style": p.get("trash_talk_style"), "tagline": p.get("tagline"), "bio": p.get("bio"),
             "signature_call": p.get("signature_call")}
        plain, excited = preview_parts(g)
        jobs.append({"team": team, "gm_name": g["gm_name"], "voice_name": voice_name(team, g["gm_name"]),
                     "description": desc, "avatar": (p.get("avatar_description") or "").strip(),
                     "hometown": (p.get("hometown") or "").strip(), "favorite_nhl_team": p.get("favorite_nhl_team"),
                     "personality": p.get("personality") or [], "plain": plain, "excited": excited,
                     "text": f"{plain} {excited}",
                     "source": {"personas": rel(personas_path), "model": t.get("model"), "display": t.get("display")}})
    if teams_filter:
        for tf in teams_filter:
            if tf != "host" and not any(t.get("team") == tf for t in teams):
                warn(f"{tf}: not in {rel(personas_path)} -- skipped")
    return jobs, models


def host_job() -> dict:
    return {"team": "host", "gm_name": HOST["gm_name"], "voice_name": HOST["voice_name"], "description": HOST["brief"],
            "avatar": "", "personality": [], "plain": HOST["plain"], "excited": HOST["excited"],
            "text": f"{HOST['plain']} {HOST['excited']}", "spec": dict(HOST["spec"]),
            "source": {"personas": None, "model": None, "display": "The Commissioner (host)"}}


# ------------------------------------------------------------------------------------- casting specs
SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "gender_presentation": {"type": "string", "enum": ["male", "female", "androgynous"]},
        "age": {"type": "string"},
        "register": {"type": "string", "enum": REGISTERS},
        "register_commitment": {"type": "string", "enum": ["explicit", "implied", "open"]},
        "acceptable_registers": {"type": "array", "items": {"type": "string", "enum": REGISTERS}},
        "texture": {"type": "array", "items": {"type": "string"}},
        "regional_flavour": {"type": "string"},
        "accent": {"type": "string"},
        "accent_region": {"type": "string"},
        "accent_strength": {"type": "string", "enum": ["light", "noticeable", "broad"]},
        "signature_quality": {"type": "string"},
    },
    "required": ["gender_presentation", "age", "register", "register_commitment", "acceptable_registers",
                 "texture", "regional_flavour", "accent", "accent_region", "accent_strength", "signature_quality"],
    "additionalProperties": False,
}
CASTING_SYSTEM = (
    "You are a voice casting director for an animated sitcom. From a character's own voice description, avatar and "
    "personality, write a TIMBRE-ONLY casting spec: the voice at rest, as it speaks normally. Ignore volume, energy "
    "and performance words (booming, roar, shout, arena, hype, rapid-fire, loud, explosive, Game 7): those are "
    "delivered later with performance tags and must NOT shape the base voice. Keep the character's intent: honour "
    "any register, texture, age and light regional flavour the description asks for. register = the single best "
    "fit; acceptable_registers = the registers that would still honour the description (explicit requests: 1-2; "
    "implied: 2-3; open: up to 4). texture = 2-4 plain timbre words (e.g. gravelly, raspy, smooth, velvety, nasal, "
    "breathy, reedy, brassy, warm, dark, bright, round). ACCENT IS A FIRST-CLASS ATTRIBUTE: accent = the accent the "
    "character describes, stated plainly and anchored on their hometown, e.g. 'rural Alberta (Canadian prairie) "
    "accent', 'Saskatchewan farm-country (Canadian prairie) accent', 'Cape Breton (Maritime Canadian) accent'; natural, "
    "never a caricature; accent_region = the region only (e.g. 'east-central Alberta'); accent_strength = light, "
    "noticeable or broad as the description implies (default noticeable). regional_flavour = the same as accent. "
    "signature_quality = one short timbre phrase that makes this voice instantly recognisable (not about volume, "
    "energy or accent). No real people.")


def deterministic_spec(job: dict) -> dict:
    d = f"{job['description']} {job.get('avatar', '')}".lower()
    fem = bool(re.search(r"\b(feminine|female|woman|she|her|mezzo|alto|soprano|contralto)\b", d))
    masc = bool(re.search(r"\b(baritone|bass|masculine|male|man|he|his|him|tenor)\b", d))
    gender = "androgynous" if fem == masc else ("female" if fem else "male")
    reg = next((r for r in ("contralto", "mezzo-soprano", "soprano", "alto", "bass-baritone", "baritone", "tenor", "bass")
                if r in d), "alto" if gender == "female" else "baritone")
    if "baritone" in reg and re.search(r"\b(deep|cathedral|low)\b", d):
        reg = "bass-baritone"
    tex = [w for w in ("gravelly", "raspy", "smooth", "velvety", "nasal", "breathy", "warm", "bright", "brassy", "rich",
                       "husky", "grainy", "dark", "round") if w in d][:4]
    age = re.search(r"(early|mid|late)[- ](twenties|thirties|forties|fifties|sixties|\d0s)|\b\d0s\b", d)
    flav = re.search(r"(light|faint|slight|hint of)[^.;,]{0,40}(canadian|prairie|maritime|east coast|pacific|northern|"
                     r"atlantic|mountain)[^.;,]{0,20}", d)
    i = REGISTERS.index(reg)
    acc = [r for r in REGISTERS[max(0, i - 1): i + 2] if (REGISTERS.index(r) < 6) == (i < 6)]
    home = job.get("hometown") or ""
    prov = {"AB": "Alberta", "SK": "Saskatchewan", "MB": "Manitoba", "BC": "British Columbia", "ON": "Ontario",
            "NS": "Nova Scotia", "NB": "New Brunswick", "NL": "Newfoundland", "PE": "Prince Edward Island"}
    pv = next((v for k, v in prov.items() if home.upper().endswith(k)), "")
    accent = (f"rural {pv} (Canadian {'prairie' if pv in ('Alberta', 'Saskatchewan', 'Manitoba') else 'regional'}) accent"
              if pv else (flav.group(0).strip() if flav else ""))
    return {"gender_presentation": gender, "age": age.group(0) if age else "adult", "register": reg,
            "register_commitment": "implied", "acceptable_registers": acc, "texture": tex or ["warm"],
            "regional_flavour": accent, "accent": accent, "accent_region": pv, "accent_strength": "noticeable",
            "signature_quality": ""}


def gender_cue(text: str) -> str | None:
    d = f" {text.lower()} "
    fem = len(re.findall(r"\b(feminine|female|woman|she|her|mezzo|alto|soprano|contralto)\b", d))
    masc = len(re.findall(r"\b(masculine|male|man|he|his|him|baritone|bass|tenor)\b", d))
    return "female" if fem > masc else "male" if masc > fem else None


def clean_spec(spec: dict, card: str = "") -> dict:
    s = dict(spec)
    cue = gender_cue(card) if card else None
    if cue and s.get("gender_presentation") != cue:
        s["gender_presentation"] = cue  # the card's own words win over the model's guess
    s["texture"] = [w for w in (strip_energy(t) for t in s.get("texture", [])) if w][:4]
    if card and not s.get("diverged"):  # keep texture words the card supports (by stem) when at least two survive
        low = card.lower()
        grounded = [w for w in s["texture"] if w.lower()[: max(3, min(5, len(w) - 1))] in low]
        if len(grounded) >= 2:
            s["texture"] = grounded
    s["signature_quality"] = strip_energy(s.get("signature_quality", ""))
    s["regional_flavour"] = strip_energy(s.get("regional_flavour", ""))
    s["accent"] = strip_energy(s.get("accent") or s.get("regional_flavour") or "")
    s.setdefault("accent_region", "")
    if s.get("accent_strength") not in ("light", "noticeable", "broad"):
        s["accent_strength"] = "noticeable"
    acc = [r for r in s.get("acceptable_registers", []) if r in REGISTERS]
    if s["register"] not in acc:
        acc = [s["register"], *acc]
    s["acceptable_registers"] = acc
    return s


def llm_spec(job: dict, key: str, model: str) -> dict:
    user = (f"Character: {job['gm_name']}\nHometown: {job.get('hometown') or 'not given'}\n"
            f"Voice description (the character's own words): {job['description']}\n"
            f"Avatar: {job.get('avatar', '')}\nPersonality: {', '.join(job.get('personality') or [])}")
    body = {"model": model, "temperature": 0.2, "seed": 7, "max_tokens": 2500, "reasoning": {"max_tokens": 600},
            "messages": [{"role": "system", "content": CASTING_SYSTEM}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "casting_spec", "strict": True, "schema": SPEC_SCHEMA}}}
    r = httpx.post(OPENROUTER, json=body, timeout=90,
                   headers={"Authorization": f"Bearer {key}", "X-Title": "GMB voice casting"})
    if r.status_code != 200:
        raise RuntimeError(f"casting model HTTP {r.status_code}: {scrub(r.text, key)[:200]}")
    ch = r.json()["choices"][0]
    content = ch["message"].get("content")
    if not content:
        raise RuntimeError(f"casting model returned no content (finish_reason={ch.get('finish_reason')})")
    m = re.search(r"\{.*\}", content, re.S)
    return json.loads(m.group(0) if m else content)


def casting_spec(job: dict, args, competitors: set[str]) -> dict:
    """Cached per (card text, model): the same card never costs twice."""
    if job.get("spec"):
        return {**clean_spec(job["spec"]), "_source": "fixed"}
    sig = hashlib.sha1(json.dumps([job["description"], job.get("avatar"), job.get("personality"), job.get("hometown"),
                                   args.casting_model, "accent-v1"],
                                  sort_keys=True).encode()).hexdigest()[:16]
    cache = CASTING / f"{job['team']}.json"
    c = read_json(cache, {})
    want = "deterministic" if args.no_llm else args.casting_model
    if c.get("sig") == sig and (c.get("spec") or {}).get("_source") == want:
        return clean_spec(c["spec"], f"{job['description']} {job.get('avatar', '')}")  # current cleaning rules
    spec, src = None, "deterministic"
    if not args.no_llm:
        if args.casting_model in competitors:
            raise SystemExit(f"--casting-model {args.casting_model} is a league competitor; pick a non-competing model")
        try:
            spec, src = llm_spec(job, env_key("OPENROUTER_API_KEY"), args.casting_model), args.casting_model
        except Exception as e:  # noqa: BLE001 - fall back to parsing
            warn(f"{job['team']}: casting model failed ({e}); using deterministic parsing")
    spec = clean_spec(spec or deterministic_spec(job), f"{job['description']} {job.get('avatar', '')}")
    spec["_source"] = src
    write_json_atomic(cache, {"sig": sig, "team": job["team"], "spec": spec, "created_at": now()})
    return spec


LOWER_CUES = re.compile(r"\b(deep|deeper|cathedral\S*|massive|rumbl\w*|diesel|low|lower|dark|bass|chest\w*|resonan\w*|"
                        r"leviathan|cavern\w*|huge|barrel\S*|growl\w*|abyss\w*|sonar)\b", re.I)
HIGHER_CUES = re.compile(r"\b(bright|brighter|brass\w*|ringing|reedy|nasal|thin|tenor|cutting|zippy|youthful|"
                         r"cracks? (?:slightly )?at the top)\b", re.I)


def lean(desc: str) -> int:
    """Which way the card itself pulls the register: >0 higher, <0 lower (count of pitch cues in its own words)."""
    return len(HIGHER_CUES.findall(desc)) - len(LOWER_CUES.findall(desc))


def plan_cast(jobs: list[dict], fixed: dict[str, str]) -> None:
    """Spread registers across the cast (host and kept voices count). Strong leaners take the register their words
    pull toward (up to two steps from the draft); neutral cards keep their register while there is room; the rest
    move one step in the direction their own words allow. Capacity: 2 per register, 3 for baritone."""
    count: dict[str, int] = {}
    for r in fixed.values():
        count[r] = count.get(r, 0) + 1
    cap = lambda r: 3 if r == "baritone" else 2

    def ladder(j) -> list[str]:
        s = j["spec"]
        scale = FEMALE_SCALE if s["register"] in FEMALE_SCALE else MALE_SCALE
        i, d = scale.index(s["register"]), j["lean"]
        step = 1 if d > 0 else -1 if d < 0 else 0
        out = [s["register"]]
        if step:
            for k in (1, 2) if abs(d) >= 3 else (1,):
                if 0 <= i + step * k < len(scale):
                    out.append(scale[i + step * k])
        else:
            out += [scale[x] for x in (i - 1, i + 1) if 0 <= x < len(scale)]
        return out

    for j in jobs:
        j["lean"] = lean(j["description"])
    strong = [j for j in jobs if abs(j["lean"]) >= 3]
    neutral = [j for j in jobs if j["lean"] == 0]
    medium = [j for j in jobs if 0 < abs(j["lean"]) < 3]
    for group, prefer_far in ((strong, True), (neutral, False), (medium, False)):
        for j in sorted(group, key=lambda j: -abs(j["lean"])):
            opts = ladder(j)
            if prefer_far:
                opts = list(reversed(opts))
            free = [r for r in opts if count.get(r, 0) < cap(r)]
            best = free[0] if free else min(opts, key=lambda r: count.get(r, 0))
            j["planned_register"] = best
            j["plan_note"] = ("as described" if best == j["spec"]["register"] else
                              f"nudged {j['spec']['register']} -> {best} (card leans {'lower' if j['lean'] < 0 else 'higher' if j['lean'] > 0 else 'neither way'}; spreading the cast)")
            count[best] = count.get(best, 0) + 1


def diversify_textures(jobs: list[dict]) -> None:
    """No leading texture word for more than 2 characters: lead with each character's rarer words."""
    freq: dict[str, int] = {}
    for j in jobs:
        for w in j["spec"]["texture"]:
            freq[w.lower()] = freq.get(w.lower(), 0) + 1
    lead: dict[str, int] = {}
    for j in sorted(jobs, key=lambda j: len(j["spec"]["texture"])):
        tex = sorted(j["spec"]["texture"], key=lambda w: freq[w.lower()])
        for w in tex:
            if lead.get(w.lower(), 0) < 2:
                tex.remove(w)
                tex.insert(0, w)
                break
        lead[tex[0].lower()] = lead.get(tex[0].lower(), 0) + 1
        j["spec"]["texture"] = tex[:3]


MALE_SCALE = REGISTERS[:6]
FEMALE_SCALE = REGISTERS[6:]
CAST_SYSTEM = (
    "You are the voice casting director for an animated sitcom about hockey GMs. Every voice must be instantly "
    "distinguishable from every other, like a great cartoon cast, while staying true to what each character asked "
    "for. You get each character's own voice description and a draft timbre spec. Return a cast plan. Rules: "
    "(1) keep each character's gender presentation, age and requested regional flavour. "
    "(2) register may move at most ONE step from the draft register along bass < bass-baritone < baritone < "
    "high baritone < tenor < high tenor (or contralto < alto < mezzo-soprano < soprano), and only in a direction the "
    "description supports: deep, massive, rumbling, cathedral, dark -> lower; bright, light, reedy, brassy, ringing, "
    "cracks at the top -> higher. (3) spread the cast: at most {cap} characters per register ({cap_baritone} for "
    "baritone); the host is fixed at {host_register} and counts toward that limit. (4) texture: 2-3 plain timbre "
    "words taken from or close to the character's own description; make textures differ across the cast (no leading "
    "texture word for more than 2 characters). (5) signature_quality: one short, vivid, timbre-only phrase unique to "
    "the character (no volume or energy words). (6) never use energy/performance words (booming, roar, shout, arena, "
    "hype, loud, explosive). reason: why this register/texture, in a few words.")


def cast_plan_llm(jobs: list[dict], host_register: str | None, key: str, model: str) -> dict[str, dict]:
    teams = [j["team"] for j in jobs]
    n_male = sum(j["spec"]["gender_presentation"] != "female" for j in jobs) + (1 if host_register else 0)
    cap = max(2, -(-n_male // 6))
    schema = {"type": "object", "additionalProperties": False, "required": ["cast"], "properties": {"cast": {
        "type": "array", "items": {"type": "object", "additionalProperties": False,
                                   "required": ["team", "register", "texture", "signature_quality", "reason"],
                                   "properties": {"team": {"type": "string", "enum": teams},
                                                  "register": {"type": "string", "enum": REGISTERS},
                                                  "texture": {"type": "array", "items": {"type": "string"}},
                                                  "signature_quality": {"type": "string"},
                                                  "reason": {"type": "string"}}}}}}
    cards = [{"team": j["team"], "name": j["gm_name"], "own_voice_description": j["description"],
              "draft": {k: j["spec"][k] for k in ("gender_presentation", "age", "register", "texture",
                                                  "regional_flavour", "signature_quality")}} for j in jobs]
    system = CAST_SYSTEM.format(cap=cap, cap_baritone=cap + 1, host_register=host_register or "n/a")
    system += (' Answer with JSON only: {"cast": [{"team": "<team id>", "register": "<one of ' + ", ".join(REGISTERS)
               + '>", "texture": ["word", "word"], "signature_quality": "...", "reason": "..."}, ...]} with one entry '
               'per team: ' + ", ".join(teams) + ".")
    body = {"model": model, "temperature": 0.2, "seed": 7, "max_tokens": 4000,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps({"host": {"register": host_register}, "cast": cards})}],
            "response_format": {"type": "json_object"}}
    r = httpx.post(OPENROUTER, json=body, timeout=180, headers={"Authorization": f"Bearer {key}", "X-Title": "GMB voice casting"})
    if r.status_code != 200:
        raise RuntimeError(f"cast plan HTTP {r.status_code}: {scrub(r.text, key)[:200]}")
    content = r.json()["choices"][0]["message"].get("content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    return {c["team"]: c for c in json.loads(m.group(0) if m else content)["cast"]}


def apply_cast_plan(jobs: list[dict], plan: dict[str, dict]) -> None:
    """Validate the model's plan: <= 1 register step, same scale, energy words stripped; else keep the draft."""
    for j in jobs:
        s, c = j["spec"], plan.get(j["team"])
        scale = FEMALE_SCALE if s["register"] in FEMALE_SCALE else MALE_SCALE
        reg, note = s["register"], "as described"
        if c and c["register"] in scale and abs(scale.index(c["register"]) - scale.index(s["register"])) <= 1:
            reg = c["register"]
            if reg != s["register"]:
                note = f"nudged {s['register']} -> {reg}: {strip_energy(c.get('reason', ''))[:120]}"
        if c:
            tex = [w for w in (strip_energy(t) for t in c.get("texture", [])) if w][:3]
            sig = strip_energy(c.get("signature_quality", ""))
            if tex:
                s["texture"] = tex
            if sig:
                s["signature_quality"] = sig
        j["planned_register"], j["plan_note"] = reg, note


ACCENT_WORD = {"light": "light, natural", "noticeable": "clear, natural", "broad": "thick, natural"}
STRONGER = {"light": "noticeable", "noticeable": "broad", "broad": "broad"}
PRAIRIE = re.compile(r"alberta|saskatch|manitoba|prairie", re.I)


def accent_phrase(spec: dict) -> str:
    """The accent stated plainly for Voice Design and the listening judge: the card's region, no town names,
    e.g. 'rural east-central Alberta (Canadian prairie) accent'. Heritage cadences (e.g. 'Ukrainian-prairie') are
    left to the card: stating them to Voice Design risks a foreign-accent caricature."""
    if spec.get("accent_plain"):
        return spec["accent_plain"]
    acc, region = (spec.get("accent") or "").strip(), (spec.get("accent_region") or "").strip()
    if not acc:
        return ""
    if PRAIRIE.search(f"{acc} {region}"):
        prov = next((p for p in ("Alberta", "Saskatchewan", "Manitoba") if p.lower() in f"{region} {acc}".lower()), "")
        reg = region if prov and prov.lower() in region.lower() else prov
        reg = re.sub(r"\b(North|South|East|West|Central|Northeast|Northwest|Southeast|Southwest|Northern|Southern|"
                     r"Eastern|Western|Northwestern|Northeastern|Southwestern|Southeastern|East-central|West-central|"
                     r"Rural)\b", lambda m: m.group(0).lower(), reg)
        reg = re.sub(r"^rural\s+", "", reg).strip()
        return f"rural {reg or 'Alberta'} (Canadian prairie) accent"
    return acc if "accent" in acc.lower() else f"{acc} accent"


LOW_PITCH_PHRASE = {"bass": "Very deep, low-pitched bass", "bass-baritone": "Deep, low-pitched bass-baritone",
                    "contralto": "Deep, low-pitched contralto"}


def timbre_prompt(spec: dict, register: str, range_clause: str = RANGE_CLAUSE, accent_first: bool = False) -> str:
    """Accent first (Voice Design weighs the opening words most) -- except for the low registers: with the accent
    first, 'Deep bass' designs came out at 122-179 Hz for a 70-98 Hz band (and the host at 150-210 Hz for 82-110);
    gender/age + an explicitly low-pitched register first brought them to 100-115 Hz with the accent still passing."""
    g = {"male": "Male", "female": "Female", "androgynous": "Androgynous"}[spec["gender_presentation"]]
    accent = (f"Native Canadian English with a {ACCENT_WORD[spec.get('accent_strength', 'noticeable')]} "
              f"{accent_phrase(spec)}, never a caricature." if spec.get("accent") else None)
    low = register in LOW_PITCH_PHRASE and not accent_first
    reg = (f"{LOW_PITCH_PHRASE[register] if register in LOW_PITCH_PHRASE and accent else REGISTER_PHRASE[register]} "
           f"speaking voice with a "
           f"{', '.join(spec['texture']) or 'natural'} texture.")
    parts = ([f"{g}, {spec['age']}.", reg] + ([accent] if accent else [])) if low else \
            ([accent] if accent else []) + [f"{g}, {spec['age']}.", reg]
    sig = re.sub(r"\b(bass-baritone|bass|baritone|tenor|contralto|alto|mezzo-soprano|mezzo|soprano)\b", "voice",
                 spec.get("signature_quality") or "", flags=re.I)
    sig = re.sub(r"\b(deep|deeper|high|low|lower)\b(?!-)\s*", "", sig, flags=re.I).strip()  # the register phrase decides
    sig = re.sub(r"\s*(?:,|\b(?:with|and))?\s*(?:a (?:little|touch of|bit of)\s+)?(?:(?:rink|arena|room|hall|barn)\s+)?"
                 r"(?:echo|reverb)\w*", "", sig, flags=re.I).strip(" ,")  # acoustics, not timbre: they add reverb
    if sig:
        parts.append(with_period(sig[0].upper() + sig[1:]))
    if spec.get("regional_flavour") and not spec.get("accent"):
        parts.append(with_period(f"{spec['regional_flavour'][0].upper()}{spec['regional_flavour'][1:]}, nothing exaggerated"))
    parts.append(range_clause)
    return " ".join(parts)


# ------------------------------------------------------------------------------------- audio analysis
def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True)


def duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def to_wav(src: Path, sr: int = 16000) -> Path:
    out = Path(tempfile.mkdtemp()) / (src.stem + ".wav")
    ffmpeg("-y", "-i", str(src), "-ac", "1", "-ar", str(sr), str(out))
    return out


def norm_words(text: str) -> list[str]:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text).lower()
    text = re.sub(r"[—–\-]", " ", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return [w.strip("'") for w in text.split() if w.strip("'")]


def wer(ref: list[str], hyp: list[str]) -> float:
    n, m = len(ref), len(hyp)
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ref[i - 1] != hyp[j - 1]))
        prev = cur
    return prev[m] / max(n, 1)


class Analyzer:
    """Whisper (optional), Praat pitch/effort, Resemblyzer embeddings -- on the plain and the [excited] parts."""

    def __init__(self, stt_model: str = "base"):
        import numpy as np
        import parselmouth
        from resemblyzer import VoiceEncoder, preprocess_wav

        self.np, self.pm, self.pre = np, parselmouth, preprocess_wav
        self.enc = VoiceEncoder("cpu", verbose=False)
        self.stt = None
        try:
            from faster_whisper import WhisperModel

            self.stt = WhisperModel(stt_model, device="cpu", compute_type="int8", local_files_only=True)
        except Exception as e:  # noqa: BLE001
            warn(f"Whisper '{stt_model}' unavailable ({type(e).__name__}); skipping WER, splitting by text length")

    def cos(self, a, b) -> float:
        np = self.np
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    def _part(self, snd, t0: float, t1: float) -> dict:
        np = self.np
        seg = snd.extract_part(from_time=max(0.0, t0), to_time=min(snd.get_total_duration(), t1), preserve_times=False)
        pitch = seg.to_pitch_ac(time_step=0.01, pitch_floor=60, pitch_ceiling=520)
        sel = pitch.selected_array
        f0 = sel["frequency"][(sel["frequency"] > 0) & (sel["strength"] >= 0.45)]
        x = seg.values[0]
        win = int(0.1 * seg.sampling_frequency)
        n = len(x) // win
        out = {"seconds": round(seg.get_total_duration(), 2), "f0_median_hz": None, "f0_st_std": None, "alpha_db": None}
        if f0.size > 10:
            st = 12 * np.log2(f0 / np.median(f0))
            st = np.where(st > 9, st - 12, np.where(st < -9, st + 12, st))
            out["f0_median_hz"] = round(float(np.median(f0)), 1)
            out["f0_st_std"] = round(float(np.std(st)), 2)
        if n:
            frames = x[: n * win].reshape(n, win)
            db = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)
            spec = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1)) ** 2
            freqs = np.fft.rfftfreq(win, 1 / seg.sampling_frequency)
            keep = db > db.max() - 30
            lo = spec[keep][:, (freqs >= 50) & (freqs < 1000)].sum()
            hi = spec[keep][:, (freqs >= 1000) & (freqs < 5000)].sum()
            out["alpha_db"] = round(float(10 * np.log10(hi / max(lo, 1e-12))), 2) if keep.any() else None
        return out

    def analyze(self, audio: Path, plain: str, full: str, ignore: set[str] = frozenset()) -> dict:
        """Register (plain part), lift of the [excited] part, WER over the whole text, plain-part embedding."""
        np = self.np
        wav = to_wav(audio)
        snd = self.pm.Sound(str(wav))
        dur = snd.get_total_duration()
        n_plain = len(norm_words(spoken(plain)))
        t_split = dur * len(spoken(plain)) / max(1, len(spoken(full)))
        out = {"dur_s": round(dur, 2), "wer": None, "transcript": None}
        if self.stt:
            segs, _ = self.stt.transcribe(str(wav), language="en", beam_size=5, word_timestamps=True, vad_filter=False,
                                          condition_on_previous_text=False)
            segs = list(segs)
            words = [w for s in segs for w in (s.words or [])]
            hyp = " ".join(s.text.strip() for s in segs)
            out["transcript"] = hyp
            ref = [w for w in norm_words(spoken(full)) if w not in ignore]
            hw = [w for w in norm_words(hyp) if w not in LAUGH_TOKENS and w not in ignore]
            out["wer"] = round(wer(ref, hw), 3)
            # body WER: drop the "I'm <name>." intro from both sides, re-sync the transcript on the next word
            ref_all = norm_words(spoken(full))
            hyp_all = [w for w in norm_words(hyp) if w not in LAUGH_TOKENS]
            intro = norm_words(spoken(plain.split(".")[0])) if plain.lower().startswith("i'm ") else []
            body = ref_all[len(intro):]
            k = next((i for i, w in enumerate(hyp_all[: len(intro) + 6]) if body and w == body[0]), None)
            out["wer_body"] = round(wer(body, hyp_all[k:]), 3) if (intro and k is not None) else out["wer"]
            letters = lambda ws: "".join(w for w in ws if w not in FILLERS).replace("'", "")
            ref_l, hyp_l = letters(ref_all), letters(hyp_all)
            out["cer"] = round(wer(list(ref_l), list(hyp_l)), 3)
            if len(words) > n_plain >= 3:
                # re-sync on the first words of the shouted part (Whisper may merge/split words in the plain part)
                hwords = [norm_words(w.word) for w in words]
                exc = norm_words(spoken(full))[n_plain: n_plain + 2]
                idx = next((i for i in range(max(0, n_plain - 6), min(len(words), n_plain + 6))
                            if exc and hwords[i] and hwords[i][0] == exc[0]
                            and (len(exc) < 2 or i + 1 >= len(words) or (hwords[i + 1] and hwords[i + 1][0] == exc[1]))), None)
                t_split = float(words[idx if idx is not None else n_plain].start)
        plain_m, exc_m = self._part(snd, 0, t_split), self._part(snd, t_split, dur)
        out.update({"plain": plain_m, "excited": exc_m, "split_s": round(t_split, 2)})
        lift = 0.0
        if plain_m["f0_median_hz"] and exc_m["f0_median_hz"]:
            lift += 12 * float(np.log2(exc_m["f0_median_hz"] / plain_m["f0_median_hz"]))
        if plain_m["alpha_db"] is not None and exc_m["alpha_db"] is not None:
            lift += (exc_m["alpha_db"] - plain_m["alpha_db"]) / 3
        out["lift"] = round(lift, 2)
        x = snd.values[0][: int(t_split * snd.sampling_frequency)]
        out["_emb"] = self.enc.embed_utterance(self.pre(x.astype(np.float32), source_sr=int(snd.sampling_frequency)))
        out["chars_per_s"] = round(len(spoken(full)) / max(0.3, dur), 1)
        Path(wav).unlink(missing_ok=True)
        return out

    def embed_file(self, audio: Path, until_s: float | None = None):
        np = self.np
        wav = to_wav(audio)
        snd = self.pm.Sound(str(wav))
        x = snd.values[0]
        if until_s:
            x = x[: int(until_s * snd.sampling_frequency)]
        Path(wav).unlink(missing_ok=True)
        return self.enc.embed_utterance(self.pre(x.astype(np.float32), source_sr=int(snd.sampling_frequency)))

    def plain_f0(self, audio: Path, until_s: float | None = None) -> dict:
        wav = to_wav(audio)
        snd = self.pm.Sound(str(wav))
        m = self._part(snd, 0, until_s or snd.get_total_duration())
        Path(wav).unlink(missing_ok=True)
        return m


# ------------------------------------------------------------------------------------------ ElevenLabs
class BudgetExceeded(Exception):
    pass


class ApiError(Exception):
    pass


class ElevenLabs:
    def __init__(self, key: str, budget: int):
        self.key = key
        self.http = httpx.Client(timeout=httpx.Timeout(240.0, connect=20.0), headers={"xi-api-key": key})
        self.budget = budget
        self.spent = 0
        self.protected: set[str] = {AMERICAN_REFERENCE_ID}  # reference / fallback voices: never deleted

    def _req(self, method: str, path: str, team: str, chars: int = 0, note: str | None = None, **kw) -> httpx.Response:
        if chars and self.spent + chars > self.budget:
            raise BudgetExceeded(f"{team}: needs {chars} chars, {self.budget - self.spent} left of --budget {self.budget}")
        r = None
        for attempt in range(3):
            t0 = time.time()
            r = self.http.request(method, API + path, **kw)
            log_usage({"team": team, "method": method, "endpoint": path.split("?")[0], "note": note,
                       "chars": chars if r.status_code == 200 else 0, "status": r.status_code,
                       "latency_s": round(time.time() - t0, 2), "request_id": r.headers.get("request-id"),
                       "error": None if r.status_code < 400 else scrub(r.text, self.key)[:300]})
            if r.status_code in (409, 429, 500, 502, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if r.status_code == 200 and chars:
            self.spent += chars
        return r

    def subscription(self) -> dict:
        r = self.http.get(API + "/v1/user/subscription")
        r.raise_for_status()
        d = r.json()
        return {k: d.get(k) for k in ("tier", "character_count", "character_limit", "voice_slots_used", "voice_limit")}

    def get_voice(self, voice_id: str) -> dict | None:
        r = self.http.get(f"{API}/v1/voices/{voice_id}")
        return r.json() if r.status_code == 200 else None

    def find_voice(self, name: str) -> str | None:
        r = self.http.get(API + "/v2/voices", params={"search": name, "page_size": 100, "voice_type": "personal",
                                                        "include_total_count": "false"})
        if r.status_code != 200:
            return None
        return next((v.get("voice_id") for v in r.json().get("voices", []) if v.get("name") == name), None)

    def design(self, team: str, description: str, text: str, seed: int) -> dict:
        body = {"voice_description": description, "model_id": DESIGN_MODEL, "text": text, "seed": seed,
                "should_enhance": False}
        r = self._req("POST", f"/v1/text-to-voice/design?output_format={OUTPUT_FORMAT}", team, chars=len(text),
                      note="design", json=body)
        if r.status_code != 200:
            raise ApiError(f"{team}: design failed {r.status_code}: {scrub(r.text, self.key)[:300]}")
        return r.json()

    def create(self, team: str, name: str, description: str, generated_voice_id: str, others: list[str]) -> str:
        body = {"voice_name": name, "voice_description": description[:1000], "generated_voice_id": generated_voice_id,
                "labels": {"project": "gmbench", "team": team}, "played_not_selected_voice_ids": others}
        r = self._req("POST", "/v1/text-to-voice", team, note="create", json=body)
        if r.status_code != 200:
            raise ApiError(f"{team}: create voice failed {r.status_code}: {scrub(r.text, self.key)[:300]}")
        return r.json()["voice_id"]

    def shared_voices(self, team: str, **params) -> list[dict]:
        r = self._req("GET", "/v1/shared-voices", team, note="library search", params=params)
        return r.json().get("voices", []) if r.status_code == 200 else []

    def add_shared(self, team: str, public_owner_id: str, voice_id: str, name: str) -> str:
        r = self._req("POST", f"/v1/voices/add/{public_owner_id}/{voice_id}", team, note="library add",
                      json={"new_name": name})
        if r.status_code != 200:
            raise ApiError(f"{team}: adding library voice failed {r.status_code}: {scrub(r.text, self.key)[:300]}")
        return r.json()["voice_id"]

    def tts(self, team: str, voice_id: str, text: str, settings: dict, seed: int, note: str) -> bytes:
        body = {"text": text, "model_id": TTS_MODEL, "voice_settings": settings, "seed": seed, "language_code": "en"}
        r = self._req("POST", f"/v1/text-to-speech/{voice_id}?output_format={OUTPUT_FORMAT}", team, chars=len(text),
                      note=note, json=body)
        if r.status_code != 200:
            raise ApiError(f"{team}: TTS failed {r.status_code}: {scrub(r.text, self.key)[:300]}")
        return r.content

    def delete_voice(self, team: str, voice_id: str) -> int:
        if voice_id in self.protected:
            warn(f"{team}: {voice_id} is a protected reference/fallback voice; NOT deleting")
            return 0
        return self._req("DELETE", f"/v1/voices/{voice_id}", team, note="delete replaced").status_code


# ----------------------------------------------------------------------------- accent check (listening)
# Exactly the wording validated 8/8 (extra instructions -- 'ignore voice quality', a caricature flag -- made the
# model answer by position on one pair, so they are out).
ACCENT_PAIR = ("Two clips of different speakers reading the SAME words. Judging PRONUNCIATION ONLY (vowels such as "
               "Canadian raising in 'about', 'out', 'house'; the vowel in 'sorry'; intonation), which speaker sounds "
               "more like {target}? "
               'Answer JSON: {{"more_canadian": "A" or "B" or "same", "confidence": 0-10, "why": "..."}}')


class KeepExisting(Exception):
    """The host recast found no candidate good enough: keep the current (fallback) voice."""


def canadian_target(spec: dict | None) -> bool:
    return bool(CANADIAN_ACCENT.search((spec or {}).get("accent") or ""))


def accent_target_phrase(spec: dict) -> str:
    """What the judge listens for (validated wording for the prairies; it can't tell Alberta sub-regions apart)."""
    if PRAIRIE.search(f"{spec.get('accent', '')} {spec.get('accent_region', '')} {spec.get('accent_plain', '')}"):
        return "a rural Alberta / Canadian prairie accent"
    return f"a {accent_phrase(spec).removesuffix(' accent')} / Canadian accent"


def _num(x) -> float:
    try:
        return max(0.0, min(10.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


class AccentJudge:
    """Listens (audio-input model on OpenRouter). Pairwise: the candidate vs a General American reference voice reading
    the SAME words, both orders (A/B swapped). score = 5 + (signed confidence of order 1 + order 2) / 4, clamped 0-10:
    candidate picked as the more Canadian speaker at confidence c -> +c, reference picked -> -c, 'same' -> 0. Passes
    when the candidate wins BOTH orders and score >= gate. No answer in either order -> score None (unverified)."""

    def __init__(self, model: str, gate: float):
        self.model, self.gate = model, gate
        self.key = env_key("OPENROUTER_API_KEY")
        self.calls = 0

    @staticmethod
    def clip_b64(path: Path, until_s: float | None) -> str:
        tmp = Path(tempfile.mkdtemp()) / "c.mp3"
        cut = ["-t", f"{until_s:.2f}"] if until_s else []
        ffmpeg("-y", "-i", str(path), *cut, "-ac", "1", "-b:a", "64k", str(tmp))
        data = base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink(missing_ok=True)
        return data

    CONTEXT = "Context: two synthetic voice-actor takes of a friendly comedy line from a fantasy-hockey show (no real people). "

    def ask(self, a: str, b: str, target: str) -> dict:
        def body_for(preface: str) -> dict:
            content = [{"type": "text", "text": preface + ACCENT_PAIR.format(target=target)},
                       {"type": "text", "text": "Clip A:"}, {"type": "input_audio", "input_audio": {"data": a, "format": "mp3"}},
                       {"type": "text", "text": "Clip B:"}, {"type": "input_audio", "input_audio": {"data": b, "format": "mp3"}}]
            return {"model": self.model, "temperature": 0, "max_tokens": 800, "messages": [{"role": "user", "content": content}]}

        body = body_for("")
        for _ in range(3):
            self.calls += 1
            try:
                r = httpx.post(OPENROUTER, json=body, timeout=120,
                               headers={"Authorization": f"Bearer {self.key}", "X-Title": "GMB accent check"})
                ch = (r.json().get("choices") or [{}])[0]
                if ch.get("finish_reason") == "content_filter":  # the model's safety filter misfired on a hockey line
                    self.refusals = getattr(self, "refusals", 0) + 1
                    body = body_for(self.CONTEXT)
                    continue
                c = (ch.get("message") or {}).get("content") or ""
                m = re.search(r"\{.*\}", c, re.S)
                if m:
                    d = json.loads(m.group(0))
                    if d.get("more_canadian") in ("A", "B", "same"):
                        return d
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, AttributeError):
                pass
            body["max_tokens"] = 1600
        return {}


    def compare(self, cand: Path, cand_until: float | None, ref: Path, ref_until: float | None, spec: dict) -> dict:
        """Both orders; a split verdict (the model answering by position) gets a second round of both orders."""
        from concurrent.futures import ThreadPoolExecutor

        target = accent_target_phrase(spec)
        c64, r64 = self.clip_b64(cand, cand_until), self.clip_b64(ref, ref_until)
        orders: list[dict] = []
        for _round in range(2):
            with ThreadPoolExecutor(2) as ex:
                answers = list(ex.map(lambda first: self.ask(*((c64, r64) if first else (r64, c64)), target), (True, False)))
            for first, d in zip((True, False), answers):
                me = "A" if first else "B"
                orders.append({"candidate_is": me, "picked": d.get("more_canadian"), "confidence": _num(d.get("confidence")),
                               "why": str(d.get("why") or "")[:240]})
            if sum(o["picked"] == o["candidate_is"] for o in orders) != len(orders) / 2:  # not a split: done
                break
        v = accent_verdict(orders, self.gate)
        return {"target": target, **v, "gate": self.gate,
                "judge": self.model,
                "method": "pairwise vs a General American reference voice reading the same words, both orders",
                "orders": orders}


def reference_clip(el: "ElevenLabs", job: dict, ref_id: str) -> Path:
    """The General American reference voice reading the job's plain line (the same words as the candidate's plain
    part). Cached per team + text: costs len(plain) characters once."""
    tag = hashlib.sha1(f"{ref_id}|{job['plain']}".encode()).hexdigest()[:10]
    f = WORK / job["team"] / f"ref_american_{tag}.mp3"
    if not f.is_file():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(el.tts(job["team"], ref_id, job["plain"], {"stability": 0.5, **BASE_SETTINGS}, ANCHOR_SEED,
                             note="accent reference (General American, same words)"))
    return f


def accent_check(el: "ElevenLabs", an: "Analyzer", job: dict, audio: Path, until_s: float | None) -> dict | None:
    """Listening check of one clip's plain part; None when the target isn't Canadian or the check is off."""
    judge = getattr(an, "accent_judge", None)
    if judge is None or not canadian_target(job.get("spec")):
        return None
    ref = reference_clip(el, job, an.reference_id)
    return judge.compare(audio, until_s, ref, None, job["spec"])


def accent_verdict(orders: list[dict], gate: float) -> dict:
    """Verdict over every answered order. The candidate must win in BOTH positions (as clip A and as clip B: a model
    answering by position can't pass), win at least two thirds of the answers, and score >= gate, where
    score = 5 + mean signed confidence / 2 (candidate picked at confidence c: +c; reference picked: -c; 'same': 0)."""
    ans = [o for o in orders if o["picked"]]
    if len(ans) < 2:
        return {"score": None, "wins": f"0/{len(orders)}", "passed": False, "orders": orders}
    signed = sum(o["confidence"] if o["picked"] == o["candidate_is"] else
                 (-o["confidence"] if o["picked"] in ("A", "B") else 0.0) for o in ans)
    wins = sum(o["picked"] == o["candidate_is"] for o in ans)
    both = all(any(o["picked"] == o["candidate_is"] == pos for o in ans) for pos in ("A", "B"))
    score = round(max(0.0, min(10.0, 5 + signed / (2 * len(ans)))), 2)
    return {"score": score, "wins": f"{wins}/{len(ans)}", "passed": bool(both and wins * 3 >= 2 * len(ans) and score >= gate),
            "orders": orders}


def pool_accent(a: dict, b: dict, gate: float) -> dict:
    """Two independent comparisons of the same clip, pooled (the judge is noisy on borderline accents)."""
    return {**a, **accent_verdict(a["orders"] + b["orders"], gate), "pooled": 2}


def screen_accent(el: "ElevenLabs", an: "Analyzer", job: dict, cands: list[dict]) -> None:
    """Listen to every in-band, intelligible design preview not yet screened (preview audio is already paid for)."""
    from concurrent.futures import ThreadPoolExecutor

    if getattr(an, "accent_judge", None) is None or not canadian_target(job.get("spec")):
        return
    todo = [c for c in cands if c["source"] == "design" and "accent_preview" not in c and c["register_ok"]
            and c["intelligible"]]
    if not todo:
        return
    reference_clip(el, job, an.reference_id)  # render once before the parallel judging
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda c: accent_check(el, an, job, ROOT / c["file"], c.get("split_s")), todo))
    for c, r in zip(todo, res):
        c["accent_preview"] = r
        print(f"   accent (preview a{c.get('attempt')}p{c.get('index')}): score {r and r['score']} "
              f"wins {r and r['wins']} {'PASS' if r and r['passed'] else 'fail'}", flush=True)


def accent_rank(c: dict) -> int:
    """0 passed the preview listening check, 1 not checked (e.g. library preview with other words), 2 failed."""
    a = c.get("accent_preview")
    return 1 if not a or a.get("score") is None else 0 if a.get("passed") else 2


# ------------------------------------------------------------------------------------ anchor mastering
def master_anchor(src: Path, dst: Path) -> dict:
    """Trim silence, two-pass loudnorm to -16 LUFS (linear, peak-limiter pre-stage when needed), mono MP3."""
    tmp = Path(tempfile.mkdtemp())
    trimmed = tmp / "t.wav"
    ffmpeg("-y", "-i", str(src), "-af",
           "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08,areverse,"
           "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.25,areverse",
           "-ac", "1", "-ar", "44100", "-c:a", "pcm_f32le", str(trimmed))

    def measure(p: Path) -> dict:
        r = ffmpeg("-i", str(p), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-")
        return json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S).group(0))

    m = measure(trimmed)
    src2 = trimmed
    gain = -16.0 - float(m["input_i"])
    if float(m["input_tp"]) + gain + 2.0 > 0:
        src2 = tmp / "l.wav"
        ffmpeg("-y", "-i", str(trimmed), "-af", f"volume={gain:.2f}dB,alimiter=limit={10 ** (-2 / 20):.4f}:attack=1:"
               f"release=60:level=false:latency=1,volume={-gain:.2f}dB", "-c:a", "pcm_f32le", str(src2))
        m = measure(src2)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-y", "-i", str(src2), "-af",
           f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
           f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true",
           "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", str(dst))
    if not dst.is_file() or dst.stat().st_size < 1000:
        raise ApiError(f"anchor mastering failed for {dst.name}")
    r = ffmpeg("-i", str(dst), "-af", "ebur128=peak=true", "-f", "null", "-")
    tail = r.stderr[r.stderr.rfind("Summary:"):]
    return {"seconds": round(duration(dst), 2), "lufs": float(re.search(r"I:\s+(-?[\d.]+) LUFS", tail).group(1)),
            "true_peak_dbfs": float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail).group(1))}


# --------------------------------------------------------------------------------------------- casting flow
def cast_embeddings(an: Analyzer, voices: dict, exclude: set[str]) -> dict:
    """Plain-part embeddings of every cast voice's anchor (cached by anchor hash)."""
    cache = read_json(EMBEDDINGS_JSON, {})
    out, dirty = {}, False
    for team, v in voices.items():
        if team in exclude:
            continue
        a = v.get("anchor") or {}
        path = ROOT / a.get("path", "")
        if not path.is_file():
            continue
        sha = hashlib.sha1(path.read_bytes()).hexdigest()
        c = cache.get(team)
        if not c or c.get("anchor_sha1") != sha or c.get("segment") != "plain":
            vec = an.embed_file(path, a.get("plain_until_s"))
            cache[team] = {"model": "resemblyzer", "segment": "plain", "anchor_sha1": sha,
                           "vec": [round(float(x), 6) for x in vec]}
            dirty = True
        out[team] = an.np.array(cache[team]["vec"])
    if dirty:
        write_json_atomic(EMBEDDINGS_JSON, cache)
    return out


def limit_for(team: str, other: str, args) -> float:
    return args.host_max_sim if "host" in (team, other) else args.max_sim


def judge(c: dict, team: str, others: dict, an: Analyzer, band: tuple[float, float], args) -> None:
    emb = c.pop("_emb", None)
    sims = {t: round(an.cos(emb, e), 3) for t, e in others.items()} if emb is not None else c.get("sims", {})
    c["sims"] = sims
    over = {t: s for t, s in sims.items() if s > limit_for(team, t, args) - args.sim_margin}
    c["nearest"] = max(sims, key=sims.get) if sims else None
    c["nearest_sim"] = sims.get(c["nearest"]) if sims else None
    c["too_close_to"] = over
    f0 = (c.get("plain") or {}).get("f0_median_hz")
    c["register_off_st"] = round(st_dist(f0, band), 2)
    c["register_center_st"] = round(st_from_center(f0, band), 2)
    c["register_ok"] = c["register_off_st"] <= getattr(args, 'register_tol', REGISTER_TOL_ST)
    min_lift = args.host_min_lift if team == "host" else MIN_LIFT
    c["lift_ok"] = c.get("source") != "design" or (c.get("lift") or 0) >= min_lift
    c["distinct_ok"] = not over
    c["intelligible"] = (c.get("wer") is None or min(c["wer"], c.get("wer_body", c["wer"])) <= MAX_WER
                         or (c.get("cer") is not None and c["cer"] <= MAX_CER))


def pick(cands: list[dict], prefer_lift: bool = False) -> dict | None:
    ok = [c for c in cands if c["register_ok"] and c["distinct_ok"] and c["intelligible"] and c.get("lift_ok", True)
          and (c["source"] != "library" or c.get("fit_score") is None or c["fit_score"] >= 5)]
    if not ok:
        return None
    if prefer_lift:  # the host: biggest demonstrated lift among in-band, distinct candidates
        return max(ok, key=lambda c: (c.get("lift") or 0, -(c["nearest_sim"] or 0.0)))
    # register match first (1 st buckets), then character fit (library), distinctness, expressiveness
    return min(ok, key=lambda c: (round(c["register_center_st"]), -(c.get("fit_score") or 0), c["nearest_sim"] or 0.0,
                                  -(c.get("lift") or 0)))


def evaluate_design(el: ElevenLabs, an: Analyzer, job: dict, attempt: int, others: dict, band, args,
                    description: str | None = None) -> list[dict]:
    description = description or job["design_description"]
    d = el.design(job["team"], description, job["text"], seed_for(job["team"], attempt))
    wdir = WORK / job["team"]
    wdir.mkdir(parents=True, exist_ok=True)
    cands = []
    for i, pv in enumerate(d.get("previews", [])):
        f = wdir / f"a{attempt}_p{i}.mp3"
        f.write_bytes(base64.b64decode(pv["audio_base_64"]))
        c = {"source": "design", "attempt": attempt, "index": i, "file": rel(f), "generated_voice_id": pv["generated_voice_id"],
             "prompt": description,
             **an.analyze(f, job["plain"], job["text"], name_tokens(job))}
        judge(c, job["team"], others, an, band, args)
        cands.append(c)
    # keep every paid preview's generated_voice_id on disk: a failed team can be re-judged with --reuse-previews
    log = read_json(wdir / "previews.json", {"team": job["team"], "candidates": []})
    log["candidates"] = [x for x in log["candidates"] if x.get("file") not in {c["file"] for c in cands}] + [
        {k: v for k, v in c.items() if k != "_emb"} for c in cands]
    write_json_atomic(wdir / "previews.json", log)
    return cands


def library_candidates(el: ElevenLabs, an: Analyzer, job: dict, spec: dict, register: str, others: dict, band, args) -> list[dict]:
    """Search the shared Voice Library with the casting spec; measure up to ~10 previews (free)."""
    team = job["team"]
    gender = {"male": "male", "female": "female"}.get(spec["gender_presentation"])
    age_m = re.search(r"(\d)0", spec.get("age", ""))
    decade = int(age_m.group(1)) * 10 if age_m else 40
    age = "young" if decade < 30 else "old" if decade >= 60 else "middle_aged"
    low = register in ("bass", "bass-baritone", "contralto")
    words = [w for w in spec.get("texture", []) if re.fullmatch(r"[a-z-]+", w.lower())][:2]
    searches = list(job.get("library_search") or []) + [" ".join(words) or register, register.split()[-1]]
    seen, pool = set(), []
    extras = [{"descriptives": ["deep"]}, {}] if low else [{}]
    queries = [(q, x, True) for q in searches for x in extras] + [(q, {}, False) for q in searches]  # last: no age
    canadian = canadian_target(spec)
    if canadian:  # Canadian first: the accent filter on every query (plus the plain most-used list), then unfiltered
        queries = ([(q, {**x, "accent": "canadian"}, a) for q, x, a in queries]
                   + [("", {"accent": "canadian"}, True), ("", {"accent": "canadian"}, False)] + queries)
    for q, extra, with_age in queries:
        if len(pool) >= 10:
            break
        params = {"page_size": 30, "language": "en", "include_custom_rates": "false",
                  "include_live_moderated": "false", "sort": "usage_character_count_1y", **extra}
        if q:
            params["search"] = q
        if gender:
            params["gender"] = gender
        if with_age:
            params["age"] = age
        if True:
            for v in el.shared_voices(team, **params):
                if v["voice_id"] in seen or not v.get("preview_url") or (v.get("rate") or 1.0) > 1.0:
                    continue
                if v.get("category") == "famous":
                    continue
                seen.add(v["voice_id"])
                pool.append(v)
    flav = (spec.get("regional_flavour") or "").lower()
    allowed = {"american", "canadian", "standard", "neutral", ""}
    allowed |= {a for a in ("british", "irish", "scottish", "australian", "english") if a in flav}
    if not flav:
        allowed.add("british")
    if canadian:  # American, British and every other label fail a Canadian card
        allowed = {"canadian"}
    pool = [v for v in pool if (v.get("accent") or "").lower() in allowed]  # no accent the card didn't ask for
    pool = [v for v in pool if not REAL_PERSON.search(f"{v.get('name', '')} {v.get('description') or ''}")]
    wdir = WORK / team / "library"
    wdir.mkdir(parents=True, exist_ok=True)
    cands = []
    for v in pool[:10]:
        f = wdir / f"{v['voice_id']}.mp3"
        if not f.exists():
            try:
                r = httpx.get(v["preview_url"], timeout=60, follow_redirects=True)
                r.raise_for_status()
                f.write_bytes(r.content)
            except httpx.HTTPError:
                continue
        m = an.plain_f0(f)
        emb = an.embed_file(f)
        c = {"source": "library", "file": rel(f), "library_voice_id": v["voice_id"],
             "public_owner_id": v["public_owner_id"], "library_name": v.get("name"),
             "library_description": (v.get("description") or "")[:300], "category": v.get("category"),
             "accent": v.get("accent"), "age": v.get("age"), "gender": v.get("gender"), "descriptive": v.get("descriptive"),
             "use_case": v.get("use_case"), "plain": m, "wer": None, "lift": 0.0, "_emb": emb}
        judge(c, team, others, an, band, args)
        cands.append(c)
    if cands and not args.no_llm:
        try:
            scores = fit_judge(job, cands, args.plan_model)
            for c in cands:
                c["fit_score"] = scores.get(c["library_voice_id"])
        except Exception as e:  # noqa: BLE001
            warn(f"{team}: library fit judge failed ({e}); register + distinctness only")
    return cands


def fit_judge(job: dict, cands: list[dict], model: str) -> dict[str, float]:
    """Cheap non-competing model scores each library voice's own name/description against the GM's card (0-10)."""
    key = env_key("OPENROUTER_API_KEY")
    items = [{"id": c["library_voice_id"], "name": c["library_name"], "description": c["library_description"],
              "accent": c["accent"], "age": c["age"], "use_case": c["use_case"]} for c in cands]
    spec = job.get("spec") or {}
    accent_rule = (f"ACCENT WEIGHS HEAVILY: the character is from {job.get('hometown') or 'Canada'} and must sound like a "
                   f"native speaker with a {accent_target_phrase(spec)}, natural, not a caricature. A voice labelled or "
                   "described as American, British, Australian, Irish or any other non-Canadian accent scores 0-2; a "
                   "Canadian voice can score up to 10 on timbre, age and personality fit. "
                   if canadian_target(spec) else "")
    prompt = ("Score how well each library voice could play this character, 0-10, judging timbre, age and personality "
              "fit from the voice's own name and description (ignore volume/energy: delivery is directed later). "
              + accent_rule +
              "Score 0-3 for voices tied to a specific real-world culture or accent the character did not ask for. "
              "Score 0 for any voice that imitates, is cloned from or is inspired by a real, named person, celebrity, "
              "or a specific game, film or franchise character. "
              'Return JSON only: {"scores": [{"id": "...", "score": 0}]}.\n\nCharacter: '
              + json.dumps({"name": job["gm_name"], "hometown": job.get("hometown"), "accent": spec.get("accent"),
                            "voice_description": job["description"], "personality": job.get("personality")})
              + "\nVoices: " + json.dumps(items))
    body = {"model": model, "temperature": 0.1, "seed": 7, "max_tokens": 3000, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    r = httpx.post(OPENROUTER, json=body, timeout=120, headers={"Authorization": f"Bearer {key}", "X-Title": "GMB voice casting"})
    r.raise_for_status()
    content = r.json()["choices"][0]["message"].get("content") or ""
    try:
        m = re.search(r"\{.*\}", content, re.S)
        return {x["id"]: float(x["score"]) for x in json.loads(m.group(0) if m else content).get("scores", [])}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):  # salvage well-formed pairs from a sloppy answer
        return {i: float(sc) for i, sc in re.findall(r'"id"\s*:\s*"([^"]+)"\s*,\s*"score"\s*:\s*([0-9.]+)', content)}


def name_tokens(job: dict) -> set[str]:
    return set(norm_words(job["gm_name"])) - {"the", "a", "of"}


def render_anchor(el: ElevenLabs, an: Analyzer, job: dict, voice_id: str, levels: tuple[float, ...]) -> tuple[dict, list[dict]]:
    """Render the preview text with eleven_v3 (stability 0.3 by default); garble -> 0.5 fallback."""
    wdir = WORK / job["team"]
    wdir.mkdir(parents=True, exist_ok=True)
    renders = []
    plan = [(levels[0], ANCHOR_SEED), (levels[0], ANCHOR_SEED + 1), *[(x, ANCHOR_SEED) for x in levels[1:]],
            (FALLBACK_STABILITY, ANCHOR_SEED + 1)]
    tried = set()
    for s, seed in plan:
        if (s, seed) in tried:
            continue
        tried.add((s, seed))
        if renders and any(not r["artifacts"] for r in renders):
            break
        settings = {"stability": s, **BASE_SETTINGS}
        f = wdir / f"render_{voice_id[:8]}_s{s:.2f}_{seed}.mp3"  # per voice: trials must not overwrite each other
        f.write_bytes(el.tts(job["team"], voice_id, job["text"], settings, seed, note=f"anchor stability {s} seed {seed}"))
        m = an.analyze(f, job["plain"], job["text"], name_tokens(job))
        m.pop("_emb", None)
        problems = []
        w = min(m["wer"], m.get("wer_body", m["wer"])) if m["wer"] is not None else None
        if w is not None and w > MAX_WER_RENDER and not (m.get("cer") is not None and m["cer"] <= MAX_CER):
            problems.append(f"WER {w:.2f} / CER {m.get('cer')}")
        if not 7.0 <= m["chars_per_s"] <= 26.0:
            problems.append(f"pace {m['chars_per_s']} chars/s")
        renders.append({"stability": s, "seed": seed, "file": rel(f), "settings": settings, "artifacts": problems, **m})
    clean = [r for r in renders if not r["artifacts"]]
    best = clean[0] if clean else renders[-1]
    best["why"] = (f"stability {best['stability']}" + (" (default)" if best["stability"] in levels else " (fallback)")
                   + (f", anchor seed {best['seed']}" if best.get("seed") != ANCHOR_SEED else "")
                   + ("" if clean else "; WARNING: artifacts at every level, kept the last render")
                   + "".join(f"; {r['stability']} rejected: {', '.join(r['artifacts'])}" for r in renders if r["artifacts"]))
    return best, renders


def process(job: dict, el: ElevenLabs, an: Analyzer, book: dict, args, pending: set[str]) -> dict:
    team = job["team"]
    voices = book[BOOK_KEY]
    spec, register = job["spec"], job["planned_register"]
    band = BANDS[register]
    others = cast_embeddings(an, voices, exclude={team, *pending, *(args.ignore_sim or [])})
    need = len(job["text"]) * 2
    if el.spent + need > el.budget:
        raise BudgetExceeded(f"{team}: needs ~{need} chars, {el.budget - el.spent} left of --budget {el.budget}")
    cands, chosen, attempts = [], None, 0
    adopted = None if args.force else el.find_voice(job["voice_name"])
    if adopted:
        warn(f"{team}: adopting existing account voice '{job['voice_name']}' ({adopted}) not yet in voices.json")
        voice_id, source = adopted, "adopted"
    elif args.trials_per_voice > 0 or team == "host":
        n = max(args.trials_per_voice, 3 if team == "host" else 0)
        if args.reuse_previews:
            cands = reuse_previews(an, job, others, band, args)
            print(f"   reusing {len(cands)} previews from the last run", flush=True)
            screen_accent(el, an, job, cands)
        gate = args.host_min_lift if team == "host" else MIN_LIFT
        accent_on = getattr(an, "accent_judge", None) is not None and canadian_target(spec)
        if not any(c["register_ok"] and c["intelligible"] for c in cands if c["source"] == "design") \
                and team not in (args.library or []):
            for attempt in range(1, args.design_attempts + 1):
                attempts = attempt
                escalate = (attempt >= 2 and accent_on and job.get("design_description_stronger")
                            and not any(accent_rank(c) == 0 for c in cands))
                if escalate:
                    print(f"   attempt {attempt}: accent one step stronger", flush=True)
                cands += evaluate_design(el, an, job, attempt, others, band, args,
                                         job["design_description_stronger"] if escalate else None)
                screen_accent(el, an, job, cands)
                usable = [c for c in cands if c["register_ok"] and c["intelligible"]]
                if len(usable) >= n and (not accent_on or any(accent_rank(c) == 0 for c in usable)):
                    break
                if attempt < args.design_attempts and accent_on and not any(accent_rank(c) == 0 for c in usable):
                    warn(f"{team}: no in-band preview passed the accent check; redesigning")
        trial = candidate_trials(el, an, job, cands, others, band, args, n)
        lift_of = lambda t: (t[2].get("lift") or 0) if t else 0
        good = trial and not trial[0].get("forced") and lift_of(trial) >= gate
        if not good and not args.no_library:
            warn(f"{team}: trials of designed voices missed the targets; trying the Voice Library")
            lib = library_candidates(el, an, job, spec, register, others, band, args)
            cands += lib
            trial2 = candidate_trials(el, an, job, lib, others, band, args, max(2, n))
            better = trial2 and (not trial or (bool(trial[0].get("forced")) != bool(trial2[0].get("forced"))
                                               and not trial2[0].get("forced"))
                                 or (bool(trial[0].get("forced")) == bool(trial2[0].get("forced"))
                                     and lift_of(trial2) > lift_of(trial)))
            if better:
                if trial:
                    print(f"   removing earlier trial voice {trial[1]} -> HTTP {el.delete_voice(team, trial[1])}")
                trial = trial2
            elif trial2:
                print(f"   removing library trial voice {trial2[1]} -> HTTP {el.delete_voice(team, trial2[1])}")
        if trial:
            chosen, voice_id, best, renders = trial
            if team == "host" and "host" in voices and (chosen.get("forced") or lift_of(trial) < gate):
                acc = chosen.get("trial_accent") or {}
                print(f"   removing host trial voice {voice_id} -> HTTP {el.delete_voice(team, voice_id)}")
                for c in cands:
                    c.pop("_emb", None)
                write_json_atomic(WORK / team / "decision.json", {"team": team, "spec": spec, "planned_register": register,
                                                                  "candidates": cands, "chosen": "kept the existing host",
                                                                  "renders": renders})
                raise KeepExisting(f"no Canadian host candidate reached lift >= {gate} within the similarity limits "
                                   f"(best: {chosen['source']} lift {best.get('lift')}, accent score {acc.get('score')}, "
                                   f"nearest {chosen.get('trial_sims_max')})")
            return finish(job, el, an, book, args, others, cands, chosen, voice_id, chosen["source"], best, renders, attempts)
        if team == "host" and "host" in voices:
            raise KeepExisting("no host candidate could be trial-rendered")
        raise ApiError(f"{team}: no candidate could be trial-rendered")
    else:
        for attempt in (() if team in (args.library or []) else range(1, args.design_attempts + 1)):
            attempts = attempt
            if attempt >= 2 and el.spent + len(job["text"]) * 2 > el.budget:
                warn(f"{team}: no budget for a redesign")
                break
            cands += evaluate_design(el, an, job, attempt, others, band, args)
            chosen = pick(cands, prefer_lift=(team == "host"))
            if chosen:
                break
            if attempt < args.design_attempts:
                warn(f"{team}: no preview hit the {register} band, the distinctness limits and the lift gate; redesigning")
        if not chosen and not args.no_library:
            warn(f"{team}: design missed the targets twice; searching the Voice Library")
            lib = library_candidates(el, an, job, spec, register, others, band, args)
            cands += lib
            if team == "host" or team in (args.library_trial or []):
                trial = library_trials(el, an, job, lib, others, band, args)
                if trial:
                    chosen, voice_id, best, renders = trial
                    return finish(job, el, an, book, args, others, cands, chosen, voice_id, "library", best, renders, attempts)
            chosen = pick(cands)
        if not chosen:  # nothing passed everything: best distinct, then best register
            pool = [c for c in cands if c["intelligible"]] or cands
            chosen = min(pool, key=lambda c: (not c["distinct_ok"], c["register_off_st"], c["nearest_sim"] or 0.0))
            chosen["forced"] = True
            warn(f"{team}: nothing met every target; took {chosen['source']} candidate "
                 f"(register off {chosen['register_off_st']} st, nearest {chosen['nearest']} {chosen['nearest_sim']})")
        source = chosen["source"]
        if source == "design":
            rest = [c["generated_voice_id"] for c in cands if c.get("generated_voice_id") and c is not chosen]
            voice_id = el.create(team, job["voice_name"], job["design_description"], chosen["generated_voice_id"], rest)
        else:
            voice_id = el.add_shared(team, chosen["public_owner_id"], chosen["library_voice_id"], job["voice_name"])
    best, renders = render_anchor(el, an, job, voice_id, tuple(args.stability_levels))
    return finish(job, el, an, book, args, others, cands, chosen, voice_id, source if not adopted else "adopted",
                  best, renders, attempts)


def candidate_trials(el: ElevenLabs, an: Analyzer, job: dict, cands: list[dict], others: dict, band, args, n: int):
    """Create/add the top candidates one at a time, render the real anchor with eleven_v3 and keep the one that goes
    biggest while staying in register and under the similarity limits (checked on the anchor itself, no margin).
    A voice that won't lift at stability 0.3 gets one more render at 0.0 (Creative follows tags harder).
    Losing trial voices are deleted again."""
    team = job["team"]
    gate = args.host_min_lift if team == "host" else MIN_LIFT
    # Library previews are other recordings, so they read ~0.1 LESS similar than the same voice's eleven_v3 anchor
    # (host trials: 0.657 on the preview -> 0.782 on the anchor). Screen them harder before paying for a trial.
    loose = lambda c: all(v <= limit_for(team, t, args) + (0.02 if c["source"] == "design" else -0.08)
                          for t, v in (c.get("sims") or {}).items())
    pool = [c for c in cands if c["register_ok"] and c["intelligible"] and loose(c)
            and (c["source"] != "library" or (c.get("fit_score") if c.get("fit_score") is not None else 5)
                 >= getattr(args, "library_min_fit", 5))]
    if any(accent_rank(c) == 0 for c in pool):  # previews that clearly failed the listening check aren't worth a trial
        pool = [c for c in pool if accent_rank(c) != 2]
    pool.sort(key=lambda c: (c["source"] != "design", accent_rank(c), -(c.get("lift") or 0), -(c.get("fit_score") or 0),
                             c["nearest_sim"] or 0.0))
    results = []
    for c in pool[:n]:
        if el.spent + 2 * len(job["text"]) > el.budget:
            warn(f"{team}: budget stops the trials")
            break
        try:
            if c["source"] == "design":
                vid = el.create(team, job["voice_name"], c.get("prompt") or job["design_description"], c["generated_voice_id"], [])
            else:
                vid = el.add_shared(team, c["public_owner_id"], c["library_voice_id"], job["voice_name"])
            best, renders = render_anchor(el, an, job, vid, tuple(args.stability_levels))
            if (best.get("lift") or 0) < gate and 0.0 not in args.stability_levels and not best["artifacts"]:
                b0, r0 = render_anchor(el, an, job, vid, (0.0,))
                renders += r0
                if not b0["artifacts"] and (b0.get("lift") or 0) > (best.get("lift") or 0):
                    best = b0
        except (ApiError, BudgetExceeded) as e:
            warn(f"{team}: trial of {c.get('library_name') or 'design preview'} failed ({e})")
            continue
        emb = an.embed_file(ROOT / best["file"], best.get("split_s"))
        sims = {t: round(an.cos(emb, e), 3) for t, e in others.items()}
        dist_ok = all(v <= limit_for(team, t, args) for t, v in sims.items())
        reg_off = st_dist(best["plain"]["f0_median_hz"], band)
        try:
            acc = accent_check(el, an, job, ROOT / best["file"], best.get("split_s"))
            if acc is not None and not acc["passed"] and (acc["score"] is None or 4.0 <= acc["score"] < an.accent_judge.gate):
                acc = pool_accent(acc, accent_check(el, an, job, ROOT / best["file"], best.get("split_s")), an.accent_judge.gate)
        except (ApiError, BudgetExceeded) as e:
            warn(f"{team}: accent check could not run ({e})")
            acc = {"score": None, "wins": "0/0", "passed": False, "error": str(e)[:200]}
        acc_ok = acc is None or acc["passed"]
        c["trial_lift"], c["trial_sims_max"], c["trial_accent"] = best.get("lift"), max(sims.values()) if sims else None, acc
        print(f"   trial {c['source']} {c.get('library_name') or 'a%sp%s' % (c.get('attempt'), c.get('index'))}: "
              f"plain F0 {best['plain']['f0_median_hz']} Hz (off {reg_off:.2f} st), lift {best.get('lift')} at stability "
              f"{best['stability']}, nearest {max(sims, key=sims.get) if sims else None} {c['trial_sims_max']}"
              + (f", accent {acc['score']} ({acc['wins']} {'PASS' if acc['passed'] else 'fail'})" if acc else ""),
              flush=True)
        results.append({"c": c, "vid": vid, "best": best, "renders": renders, "dist_ok": dist_ok, "acc_ok": acc_ok,
                        "reg_ok": reg_off <= getattr(args, 'register_tol', REGISTER_TOL_ST) and not best["artifacts"]})
        if dist_ok and acc_ok and results[-1]["reg_ok"] and (best.get("lift") or 0) >= (
                args.host_target_lift if team == "host" else gate + 4):
            break
    if not results:
        return None
    good = [r for r in results if r["dist_ok"] and r["reg_ok"] and r["acc_ok"]]
    fails = lambda r: ((not r["acc_ok"]) + (not r["dist_ok"]) + (not r["reg_ok"])
                       + ((r["best"].get("lift") or 0) < gate))
    win = (max(good, key=lambda r: r["best"].get("lift") or 0) if good else
           min(results, key=lambda r: (fails(r), not r["acc_ok"], not r["dist_ok"], not r["reg_ok"],
                                       -(r["best"].get("lift") or 0))))
    if not good:
        win["c"]["forced"] = True
        warn(f"{team}: no trial met every target; kept the best available")
    for r in results:
        if r is not win:
            print(f"   removing losing trial voice {r['vid']} -> HTTP {el.delete_voice(team, r['vid'])}")
    return win["c"], win["vid"], win["best"], win["renders"]


def reuse_previews(an: Analyzer, job: dict, others: dict, band, args) -> list[dict]:
    """Re-judge the previous run's previews (work/<team>/decision.json) against the current cast: no design cost."""
    d = read_json(WORK / job["team"] / "decision.json", {})
    extra = read_json(WORK / job["team"] / "previews.json", {}).get("candidates", [])
    seen = {c.get("file") for c in d.get("candidates", [])}
    d = {**d, "candidates": d.get("candidates", []) + [c for c in extra if c.get("file") not in seen]}
    out = []
    for c in d.get("candidates", []):
        f = ROOT / c.get("file", "")
        if not f.is_file():
            continue
        if c.get("source") == "design":
            m = an.analyze(f, job["plain"], job["text"], name_tokens(job))
        elif c.get("source") == "library":
            m = {"plain": an.plain_f0(f), "wer": None, "lift": 0.0, "_emb": an.embed_file(f)}
        else:
            continue
        c = {k: v for k, v in c.items() if k not in ("sims", "nearest", "nearest_sim", "too_close_to", "trial_lift",
                                                     "trial_accent", "trial_sims_max", "forced", "accent_preview")}
        c.update(m)
        judge(c, job["team"], others, an, band, args)
        out.append(c)
    return out


def library_trials(el: ElevenLabs, an: Analyzer, job: dict, lib: list[dict], others: dict, band, args):
    """Library previews are ordinary reads, so lift can't be judged from them: add the top in-band, distinct,
    well-fitting library voices one at a time, render the real preview line (plain + [shouting]) and keep the one
    that goes biggest. Trial voices that lose are deleted again."""
    team = job["team"]
    ok = [c for c in lib if c["register_ok"] and c["distinct_ok"] and (c.get("fit_score") is None or c["fit_score"] >= 5)]
    ok.sort(key=lambda c: (-(c.get("fit_score") or 0), round(c["register_center_st"]), c["nearest_sim"] or 0.0))
    tried = []
    for c in ok[: args.trials]:
        if el.spent + len(job["text"]) * 2 > el.budget:
            break
        try:
            vid = el.add_shared(team, c["public_owner_id"], c["library_voice_id"], job["voice_name"])
            best, renders = render_anchor(el, an, job, vid, tuple(args.stability_levels))
        except (ApiError, BudgetExceeded) as e:
            warn(f"{team}: library trial '{c['library_name']}' failed ({e})")
            continue
        c["trial_lift"] = best.get("lift")
        print(f"   trial '{c['library_name']}': plain F0 {best['plain']['f0_median_hz']} Hz, lift {best.get('lift')}", flush=True)
        tried.append((c, vid, best, renders))
        if (best.get("lift") or 0) >= args.host_target_lift and not best["artifacts"]:
            break
    if not tried:
        return None
    win = max(tried, key=lambda t: ((t[2].get("lift") or 0) if not t[2]["artifacts"] else -99))
    for c, vid, _, _ in tried:
        if vid != win[1]:
            print(f"   removing losing trial voice {vid} -> HTTP {el.delete_voice(team, vid)}")
    return win


def finish(job: dict, el: ElevenLabs, an: Analyzer, book: dict, args, others: dict, cands: list[dict], chosen: dict | None,
           voice_id: str, source: str, best: dict, renders: list[dict], attempts: int) -> dict:
    team = job["team"]
    voices = book[BOOK_KEY]
    spec, register = job["spec"], job["planned_register"]
    band = BANDS[register]
    adopted = source == "adopted"
    anchor_path = ANCHORS / f"{team}.mp3"
    anchor = master_anchor(ROOT / best["file"], anchor_path)
    # the master trims leading silence: re-measure the plain/excited split on the anchor itself
    a = an.analyze(anchor_path, job["plain"], job["text"], name_tokens(job))
    a_emb = a.pop("_emb")
    a_sims = {t: round(an.cos(a_emb, e), 3) for t, e in others.items()}
    over = {t: s for t, s in a_sims.items() if s > limit_for(team, t, args)}
    f0 = a["plain"]["f0_median_hz"]
    reason = "adopted an existing account voice with the same name (crash recovery)"
    if chosen:
        c = chosen
        reason = (f"{'FORCED: ' if c.get('forced') else ''}{c['source']} candidate "
                  + (f"attempt {c['attempt']} preview {c['index']}" if c["source"] == "design"
                     else f"'{c['library_name']}' ({c['category']}, {c['accent']}, {c['age']})")
                  + f" of {len(cands)}: plain-read F0 {c['plain']['f0_median_hz']} Hz vs {register} band "
                  f"{band[0]}-{band[1]} (off {c['register_off_st']} st); nearest cast voice "
                  f"{c['nearest']} {c['nearest_sim']}; preview lift {c.get('lift')}, real v3 lift {c.get('trial_lift')}"
                  + (f"; WER {c['wer']:.2f}" if c.get("wer") is not None else ""))
    entry = {
        "team": team, "gm_name": job["gm_name"], "voice_id": voice_id, "name": job["voice_name"],
        "description": job["description"], "source": source if not adopted else "adopted",
        "eleven_model": TTS_MODEL, "settings": best["settings"], "settings_reason": best["why"],
        "preview_generated_voice_id": chosen.get("generated_voice_id") if chosen else None,
        "chosen_reason": reason,
        "casting": {"spec": spec, "planned_register": register, "plan_note": job.get("plan_note"), "band_hz": list(band),
                    "design_prompt": (chosen or {}).get("prompt") or job["design_description"], "attempts": attempts},
        "measured": {"plain_f0_hz": f0, "plain_register_off_st": round(st_dist(f0, band), 2),
                     "plain_alpha_db": a["plain"]["alpha_db"], "excited_f0_hz": a["excited"]["f0_median_hz"],
                     "excited_lift": a["lift"], "nearest": max(a_sims, key=a_sims.get) if a_sims else None,
                     "nearest_sim": max(a_sims.values()) if a_sims else None, "over_limit": over},
        "anchor": {"path": rel(anchor_path), "text": spoken(job["text"]), "tagged_text": job["text"],
                   "plain_text": job["plain"], "plain_until_s": a["split_s"], "model": TTS_MODEL, "seed": best.get("seed", ANCHOR_SEED),
                   "stability": best["stability"], "format": "mp3 44.1 kHz mono 128 kbps", **anchor},
        "fish_fallback": {"model": "fish-audio/s2.1-pro", "input_references": "anchor.path as data:audio/mpeg;base64 + anchor.text"},
        "source_card": job["source"],
        "created_at": now(),
    }
    if chosen and chosen["source"] == "library":
        entry["library"] = {k: chosen.get(k) for k in ("library_voice_id", "public_owner_id", "library_name",
                                                       "library_description", "category", "accent", "age", "use_case",
                                                       "fit_score", "trial_lift")}
    acc = (chosen or {}).get("trial_accent")
    if acc is None and canadian_target(spec):  # not trial-rendered here (library-trial / preview-only paths)
        try:
            acc = accent_check(el, an, job, anchor_path, a["split_s"])
        except (ApiError, BudgetExceeded) as e:
            warn(f"{team}: accent check could not run ({e})")
    entry["accent"] = {"target": spec.get("accent") or None, "region": spec.get("accent_region") or None,
                       "strength": spec.get("accent_strength"), "hometown": job.get("hometown") or None,
                       **({k: acc.get(k) for k in ("target", "score", "wins", "passed", "gate", "judge", "method", "orders")}
                          if acc else {"score": None, "passed": None}),
                       "reference_voice_id": getattr(an, "reference_id", None) if acc else None}
    old = voices.get(team)
    if team == "host":
        entry["host_policy"] = ("FIXED: the Commissioner, a Canadian (Alberta) hockey play-by-play voice with the biggest "
                                "shout lift; recast only with --recast-host (see RUNBOOK Sunday voice step).")
    if old and old.get("voice_id") != voice_id:
        entry["replaced_voice_ids"] = [old["voice_id"], *old.get("replaced_voice_ids", [])][:5]
    for c in cands:
        c.pop("_emb", None)
    write_json_atomic(WORK / team / "decision.json", {"team": team, "spec": spec, "planned_register": register,
                                                      "candidates": cands, "chosen": reason, "renders": renders})
    return entry


def reanchor(job: dict, el: ElevenLabs, an: Analyzer, book: dict, args) -> dict:
    """Re-render the anchor of an already-cast voice (new seeds / fallback), keep the voice itself."""
    voices = book[BOOK_KEY]
    e = voices[job["team"]]
    job = {**job, "plain": e["anchor"].get("plain_text", job["plain"]), "text": e["anchor"].get("tagged_text", job["text"])}
    others = cast_embeddings(an, voices, exclude={job["team"]})
    best, renders = render_anchor(el, an, job, e["voice_id"], tuple(args.stability_levels))
    anchor_path = ANCHORS / f"{job['team']}.mp3"
    anchor = master_anchor(ROOT / best["file"], anchor_path)
    a = an.analyze(anchor_path, job["plain"], job["text"], name_tokens(job))
    a_emb = a.pop("_emb")
    a_sims = {t: round(an.cos(a_emb, x), 3) for t, x in others.items()}
    e["settings"], e["settings_reason"] = best["settings"], best["why"]
    e["anchor"].update({"plain_until_s": a["split_s"], "seed": best.get("seed", ANCHOR_SEED), "stability": best["stability"],
                        **anchor})
    band = BANDS[(e.get("casting") or {}).get("planned_register", "baritone")]
    f0 = a["plain"]["f0_median_hz"]
    e["measured"].update({"plain_f0_hz": f0, "plain_register_off_st": round(st_dist(f0, band), 2),
                          "plain_alpha_db": a["plain"]["alpha_db"], "excited_f0_hz": a["excited"]["f0_median_hz"],
                          "excited_lift": a["lift"], "nearest": max(a_sims, key=a_sims.get) if a_sims else None,
                          "nearest_sim": max(a_sims.values()) if a_sims else None,
                          "over_limit": {t: x for t, x in a_sims.items() if x > limit_for(job["team"], t, args)}})
    e["reanchored_at"] = now()
    return e


def delete_replaced(el: ElevenLabs, team: str, old_id: str, new_id: str) -> None:
    if not old_id or old_id == new_id:
        return
    if old_id in el.protected:
        print(f"   kept replaced voice {old_id}: protected reference/fallback voice")
        return
    v = el.get_voice(old_id)
    if not v:
        return
    if not str(v.get("name", "")).startswith("GMB-"):
        warn(f"{team}: replaced voice {old_id} is named {v.get('name')!r}, not GMB-*; NOT deleting")
        return
    print(f"   deleted replaced voice {old_id} ({v.get('name')}) -> HTTP {el.delete_voice(team, old_id)}")


def cast_report(an: Analyzer, voices: dict, args) -> dict:
    import itertools

    emb = cast_embeddings(an, voices, exclude=set(getattr(args, "ignore_sim", None) or []))
    f0 = {t: (v.get("measured") or {}).get("plain_f0_hz") for t, v in voices.items()}
    pairs = {(a, b): round(an.cos(emb[a], emb[b]), 3) for a, b in itertools.combinations(sorted(emb), 2)}
    gm_pairs = {k: s for k, s in pairs.items() if "host" not in k}
    host_pairs = {k: s for k, s in pairs.items() if "host" in k}
    vals = [x for x in f0.values() if x]
    rep = {"voices": len(voices), "plain_f0_hz": dict(sorted(f0.items(), key=lambda x: x[1] or 0)),
           "f0_spread_hz": [min(vals), max(vals)] if vals else None,
           "max_gm_pair": max(gm_pairs.items(), key=lambda x: x[1]) if gm_pairs else None,
           "gm_pairs_over_limit": sorted([(a, b, s) for (a, b), s in gm_pairs.items() if s > args.max_sim], key=lambda x: -x[2]),
           "max_host_pair": max(host_pairs.items(), key=lambda x: x[1]) if host_pairs else None,
           "host_pairs_over_limit": sorted([(a, b, s) for (a, b), s in host_pairs.items() if s > args.host_max_sim], key=lambda x: -x[2]),
           "median_pair": sorted(pairs.values())[len(pairs) // 2] if pairs else None,
           "sources": {t: v.get("source") for t, v in voices.items()}}
    write_json_atomic(CASTING / "cast_report.json", {"generated_at": now(), **{k: (list(v) if isinstance(v, tuple) else v)
                                                                                for k, v in rep.items()},
                                                     "pairs": {f"{a}|{b}": s for (a, b), s in pairs.items()}})
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--personas", required=True, type=Path, help="personas export, e.g. runs/<run>/exports/personas.json")
    ap.add_argument("--teams", nargs="*", help="only these team ids (default: every team with a persona); 'host' = host")
    ap.add_argument("--dry-run", action="store_true", help="casting specs, cast plan, prompts and cost; no ElevenLabs calls")
    ap.add_argument("--force", action="store_true", help="recast teams already in voices.json")
    ap.add_argument("--delete-replaced", action="store_true", help="delete superseded GMB-* voices after replacement")
    ap.add_argument("--no-host", action="store_true", help="don't cast the host")
    ap.add_argument("--recast-host", action="store_true",
                    help="allow --force to replace the FIXED host voice (never casually; see RUNBOOK)")
    ap.add_argument("--no-library", action="store_true", help="never fall back to the shared Voice Library")
    ap.add_argument("--no-llm", action="store_true", help="deterministic casting specs (no OpenRouter call)")
    ap.add_argument("--casting-model", default=CASTING_MODEL, help=f"non-competing OpenRouter model (default {CASTING_MODEL})")
    ap.add_argument("--plan-model", default=PLAN_MODEL, help=f"non-competing model for the whole-cast plan (default {PLAN_MODEL})")
    ap.add_argument("--cast-llm", action="store_true", help="also run the whole-cast texture/signature pass (off: it flattened textures)")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help=f"max ElevenLabs characters (default {DEFAULT_BUDGET})")
    ap.add_argument("--max-sim", type=float, default=DEFAULT_MAX_SIM, help="max speaker cosine between two GMs")
    ap.add_argument("--host-max-sim", type=float, default=DEFAULT_HOST_MAX_SIM, help="max speaker cosine host vs GM")
    ap.add_argument("--stability-levels", type=float, nargs="+", default=list(STABILITY_LEVELS),
                    help="eleven_v3 stability for the anchor (default 0.3; 0.5 is the garble fallback)")
    ap.add_argument("--stt-model", default="base", help="faster-whisper model (local cache only)")
    ap.add_argument("--repair", type=int, default=2, help="max extra recasts for pairs still over the limits (default 2)")
    ap.add_argument("--set-register", action="append", metavar="TEAM=REGISTER",
                    help="override the planned register for one team (repeatable)")
    ap.add_argument("--library", action="append", metavar="TEAM", help="cast this team straight from the Voice Library")
    ap.add_argument("--reanchor", nargs="+", metavar="TEAM", help="only re-render these voices' anchors (no design)")
    ap.add_argument("--host-min-lift", type=float, default=HOST_MIN_LIFT,
                    help=f"host shout-lift gate (default {HOST_MIN_LIFT}; GMs use {MIN_LIFT})")
    ap.add_argument("--library-trial", action="append", metavar="TEAM",
                    help="for this GM too, trial-render library voices and keep the biggest lift (always on for the host)")
    ap.add_argument("--trials", type=int, default=3, help="max library voices to trial-render (default 3)")
    ap.add_argument("--host-target-lift", type=float, default=15.0,
                    help="stop library trials early once a voice lifts this much (default 15)")
    ap.add_argument("--design-attempts", type=int, default=2, help="Voice Design attempts before the library (default 2)")
    ap.add_argument("--trials-per-voice", type=int, default=2,
                    help="create + render the top N candidates and keep the one whose REAL eleven_v3 render lifts most "
                         "(default 2; the host always gets 3; 0 = preview-only selection)")
    ap.add_argument("--reuse-previews", action="store_true", help="re-judge the last run's previews instead of designing")
    ap.add_argument("--accent-model", default=ACCENT_JUDGE_MODEL,
                    help=f"audio-input OpenRouter model for the listening accent check (default {ACCENT_JUDGE_MODEL})")
    ap.add_argument("--accent-gate", type=float, default=ACCENT_GATE,
                    help=f"pairwise accent score a voice must reach, 0-10 (default {ACCENT_GATE})")
    ap.add_argument("--no-accent-check", action="store_true", help="skip the listening accent check")
    ap.add_argument("--accent-first", nargs="+", metavar="TEAM", default=[],
                    help="open these teams' design prompts with the accent even in a low register (register-first "
                         "wording kept low voices in band but failed the accent check for gemini 0/6)")
    ap.add_argument("--register-tol", type=float, default=REGISTER_TOL_ST,
                    help=f"semitones the plain-read F0 may sit outside the planned band (default {REGISTER_TOL_ST})")
    ap.add_argument("--library-min-fit", type=float, default=5.0,
                    help="minimum fit-judge score for a library voice to be trial-rendered (default 5)")
    ap.add_argument("--ignore-sim", nargs="+", metavar="TEAM", default=[],
                    help="leave these (deferred) voices out of the distinctness checks and the cast report")
    ap.add_argument("--sim-margin", type=float, default=0.02,
                    help="candidates must sit this far under the similarity limits (previews drift ~0.02-0.05 vs the anchor)")
    args = ap.parse_args()

    personas = args.personas if args.personas.is_absolute() else (Path.cwd() / args.personas)
    if not personas.is_file():
        raise SystemExit(f"personas file not found: {personas}")
    tf = list(args.teams or []) or None
    book = load_book()
    voices = book[BOOK_KEY]

    gm_filter = [t for t in tf if t != "host"] if tf else None
    all_jobs, competitors = load_jobs(personas, None)
    if args.reanchor:
        key = load_key()
        el = ElevenLabs(key, args.budget)
        an = Analyzer(args.stt_model)
        pool = {j["team"]: j for j in all_jobs}
        pool["host"] = host_job()
        for team in args.reanchor:
            if team not in voices or team not in pool:
                warn(f"--reanchor {team}: not cast / not in the personas file")
                continue
            e = reanchor(pool[team], el, an, book, args)
            write_json_atomic(VOICES_JSON, book)
            m = e["measured"]
            print(f"{team}: {e['settings_reason']}; plain F0 {m['plain_f0_hz']} Hz, lift {m['excited_lift']}, "
                  f"nearest {m['nearest']} {m['nearest_sim']}")
        rep = cast_report(an, voices, args)
        print(json.dumps({k: rep[k] for k in ("f0_spread_hz", "max_gm_pair", "max_host_pair")}, default=str),
              f"chars spent {el.spent}")
        return
    jobs = []
    # The host is FIXED once cast (see RUNBOOK): --force alone never recasts it; it takes an explicit --recast-host.
    if not args.no_host and ("host" not in voices or (args.force and args.recast_host)):
        jobs.append(host_job())
    elif args.force and tf and "host" in tf and not args.recast_host:
        warn("host is fixed (see RUNBOOK); add --recast-host if you really mean to replace it")
    for j in all_jobs:
        if gm_filter is not None and j["team"] not in gm_filter:
            continue
        if tf and not gm_filter:
            continue
        if j["team"] in voices and not args.force:
            print(f"skip {j['team']}: already cast ({voices[j['team']]['voice_id']}); use --force to recast")
            if voices[j["team"]].get("description") != j["description"]:
                warn(f"{j['team']}: its card changed since it was cast; re-run with --force --teams {j['team']}")
            continue
        jobs.append(j)

    # casting specs + cast plan (whole cast, so kept voices count toward crowding)
    for j in jobs:
        j["spec"] = casting_spec(j, args, competitors)
    kept = {t: (v.get("casting") or {}).get("planned_register") for t, v in voices.items()
            if t not in {j["team"] for j in jobs} and (v.get("casting") or {}).get("planned_register")}
    host = next((j for j in jobs if j["team"] == "host"), None)
    if host:
        host["planned_register"], host["plan_note"] = host["spec"]["register"], "host: fixed"
        kept["host"] = host["planned_register"]
    gms = [j for j in jobs if j["team"] != "host"]
    if gms and not args.no_llm and len(gms) > 1 and args.cast_llm:
        try:  # the whole-cast model only enriches texture/signature variety; registers are planned below
            if args.plan_model in competitors:
                raise RuntimeError(f"{args.plan_model} is a league competitor")
            apply_cast_plan(gms, cast_plan_llm(gms, kept.get("host"), env_key("OPENROUTER_API_KEY"), args.plan_model))
        except Exception as e:  # noqa: BLE001
            warn(f"cast plan model failed ({e}); keeping per-character textures")
    for j in gms:
        j["spec"]["register"] = j["spec"]["register"]  # the card's own reading stays the reference for nudges
    plan_cast(gms, kept)
    for spec_arg in args.set_register or []:  # manual casting override, e.g. --set-register "gemini=high baritone"
        team, _, reg = spec_arg.partition("=")
        j = next((x for x in jobs if x["team"] == team.strip()), None)
        if j is None or reg.strip() not in REGISTERS:
            raise SystemExit(f"--set-register {spec_arg!r}: unknown team or register ({', '.join(REGISTERS)})")
        j["planned_register"], j["plan_note"] = reg.strip(), f"set by --set-register (card: {j['spec']['register']})"
    diversify_textures(gms)
    for j in jobs:
        af = j["team"] in (args.accent_first or [])
        j["design_description"] = timbre_prompt(j["spec"], j["planned_register"], RANGE_CLAUSE, af)
        if canadian_target(j["spec"]) and STRONGER[j["spec"]["accent_strength"]] != j["spec"]["accent_strength"]:
            j["design_description_stronger"] = timbre_prompt(
                {**j["spec"], "accent_strength": STRONGER[j["spec"]["accent_strength"]]}, j["planned_register"], RANGE_CLAUSE, af)
        if j["team"] == "host":
            j["library_search"] = HOST["library_search"]
    plan = {j["team"]: {"register": j["planned_register"], "note": j["plan_note"], "spec": j["spec"],
                        "accent_prompt": accent_phrase(j["spec"]), "hometown": j.get("hometown"),
                        "prompt": j["design_description"], "prompt_attempt2_if_accent_fails": j.get("design_description_stronger"),
                        "preview": j["text"]} for j in jobs}
    write_json_atomic(CASTING / "plan.json", {"generated_at": now(), "personas": rel(personas), "plan": plan})

    est = sum(2 * len(j["text"]) for j in jobs)
    print(f"{len(jobs)} voice(s) to cast; ~{est} ElevenLabs characters (+{sum(len(j['text']) for j in jobs)} if every "
          f"voice needs a redesign; library fallback adds no design cost); budget {args.budget}")
    for j in jobs:
        s = j["spec"]
        print(f"\n[{j['team']}] {j['voice_name']}  -> {j['planned_register']} ({j['plan_note']}; spec by {s.get('_source')})\n"
              + (f"  DIVERGED: {s['diverged']}\n" if s.get("diverged") else "") +
              f"  accent: {s.get('accent') or '-'} ({s.get('accent_strength')}; region {s.get('accent_region') or '-'}; "
              f"hometown {j.get('hometown') or '-'})\n"
              f"  prompt: {j['design_description']}\n  preview: {j['text']}")
    print("\nCASTING PLAN")
    print(f"{'team':9s} {'GM':34s} {'hometown':22s} {'accent target':52s} {'strength':10s} {'card register':14s} planned")
    for j in jobs:
        s = j["spec"]
        print(f"{j['team']:9s} {j['gm_name'][:34]:34s} {(j.get('hometown') or '-')[:22]:22s} {(s.get('accent') or '-')[:52]:52s} "
              f"{s.get('accent_strength') or '-':10s} {s['register']:14s} {j['planned_register']}")
        print(f"{'':9s} -> prompt accent: {accent_phrase(s)}")
    if args.dry_run or not jobs:
        return

    key = load_key()
    el = ElevenLabs(key, args.budget)
    sub0 = el.subscription()
    remaining = (sub0["character_limit"] or 0) - (sub0["character_count"] or 0)
    print(f"\nElevenLabs {sub0['tier']}: {remaining} characters left, voice slots {sub0['voice_slots_used']}/{sub0['voice_limit']}")
    if remaining < est:
        raise SystemExit(f"account has {remaining} characters left; this run needs ~{est}")
    slots_free = (sub0["voice_limit"] or 0) - (sub0["voice_slots_used"] or 0)
    print("loading analysis models ...", flush=True)
    an = Analyzer(args.stt_model)
    refs = book.setdefault("reference_voices", {})
    refs.setdefault("american", {
        "voice_id": AMERICAN_REFERENCE_ID, "name": "Adam - Radio Announcer (Voice Library, General American)",
        "role": ("General American reference for the listening accent check (reads each candidate's own words) and the "
                 "host fallback; never deleted by design.py")})
    el.protected |= {v["voice_id"] for v in refs.values() if v.get("voice_id")}
    an.reference_id = refs["american"]["voice_id"]
    an.accent_judge = None if args.no_accent_check else AccentJudge(args.accent_model, args.accent_gate)
    book["accent_policy"] = ("Accent is cast into the voice (design prompt / Canadian library voices) and checked by "
                             "listening; no accent_tag: a '[Canadian accent]' prefix on eleven_v3 had no measurable effect.")
    kept_host = None
    # host first, then the least flexible cards
    jobs.sort(key=lambda j: (j["team"] != "host", len(j["spec"]["acceptable_registers"])))
    done, failed, stopped = [], [], []
    for n, job in enumerate(jobs):
        if slots_free <= 0 and not args.delete_replaced:
            stopped = [j["team"] for j in jobs[n:]]
            warn(f"no free voice slots; stopping before {', '.join(stopped)}")
            break
        print(f"\n== {job['team']} ({job['voice_name']}) -> {job['planned_register']}", flush=True)
        pending = {j["team"] for j in jobs[n + 1:] if j["team"] in voices} if args.force else set()
        try:
            entry = process(job, el, an, book, args, pending)
        except KeepExisting as e:
            kept_host = str(e)
            warn(f"{job['team']}: {e}; keeping {voices[job['team']]['voice_id']} (fallback)")
            continue
        except BudgetExceeded as e:
            stopped = [j["team"] for j in jobs[n:]]
            warn(f"budget guard: {e}; stopping. Not done: {', '.join(stopped)}")
            break
        except (ApiError, httpx.HTTPError) as e:
            warn(scrub(str(e), key))
            failed.append(job["team"])
            continue
        old_id = (voices.get(job["team"]) or {}).get("voice_id")
        voices[job["team"]] = entry
        book["updated_at"] = now()
        write_json_atomic(VOICES_JSON, book)
        slots_free -= 1
        done.append(job["team"])
        m = entry["measured"]
        print(f"   -> {entry['voice_id']} [{entry['source']}] plain F0 {m['plain_f0_hz']} Hz (off {m['plain_register_off_st']} st), "
              f"nearest {m['nearest']} {m['nearest_sim']}, lift {m['excited_lift']}, accent {entry['accent'].get('score')} "
              f"({'PASS' if entry['accent'].get('passed') else 'not passed'}), {entry['settings_reason']}\n"
              f"      {entry['chosen_reason']}", flush=True)
        if old_id and old_id != entry["voice_id"]:
            if args.delete_replaced:
                delete_replaced(el, job["team"], old_id, entry["voice_id"])
                slots_free += 1
            else:
                warn(f"{job['team']}: replaced voice {old_id} is still on the account (use --delete-replaced)")
    repaired = []
    for _ in range(args.repair):
        rep = cast_report(an, voices, args)
        bad = rep["gm_pairs_over_limit"] + rep["host_pairs_over_limit"]
        if not bad:
            break
        a, b, sim = bad[0]
        target = b if a == "host" else a if b == "host" else max((a, b), key=lambda t: voices[t].get("created_at", ""))
        if target in repaired or target not in {j["team"] for j in jobs}:
            break
        job = next(j for j in jobs if j["team"] == target)
        print(f"\n== repair: {a}-{b} at {sim} > limit; recasting {target}", flush=True)
        try:
            old_id = voices[target]["voice_id"]
            forced = argparse.Namespace(**{**vars(args), "force": True})
            entry = process(job, el, an, book, forced, set())
        except (BudgetExceeded, ApiError, KeepExisting, httpx.HTTPError) as e:
            warn(f"repair of {target} failed: {scrub(str(e), key)}")
            break
        voices[target] = entry
        write_json_atomic(VOICES_JSON, book)
        repaired.append(target)
        print(f"   -> {entry['voice_id']} [{entry['source']}] nearest {entry['measured']['nearest']} {entry['measured']['nearest_sim']}")
        if old_id != entry["voice_id"]:
            delete_replaced(el, target, old_id, entry["voice_id"]) if args.delete_replaced else None
    rep = cast_report(an, voices, args)
    sub1 = el.subscription()
    summary = {"ts": now(), "personas": rel(personas), "done": done, "failed": failed, "not_done": stopped,
               "chars_spent_run": el.spent, "account_remaining": (sub1["character_limit"] or 0) - (sub1["character_count"] or 0),
               "voice_slots": f"{sub1['voice_slots_used']}/{sub1['voice_limit']}",
               "f0_spread_hz": rep["f0_spread_hz"], "max_gm_pair": rep["max_gm_pair"], "max_host_pair": rep["max_host_pair"],
               "gm_pairs_over_limit": len(rep["gm_pairs_over_limit"]), "host_pairs_over_limit": len(rep["host_pairs_over_limit"]),
               "sources": rep["sources"], "repaired": repaired, "host_kept": kept_host,
               "accent": {t: (v.get("accent") or {}).get("score") for t, v in voices.items()},
               "accent_judge_calls": an.accent_judge.calls if an.accent_judge else 0}
    LOGS.mkdir(parents=True, exist_ok=True)
    with RUNS_LOG.open("a") as f:
        f.write(json.dumps(summary, default=str) + "\n")
    print("\n" + json.dumps(summary, indent=2, default=str))
    if failed or stopped:
        sys.exit(1)


if __name__ == "__main__":
    main()
