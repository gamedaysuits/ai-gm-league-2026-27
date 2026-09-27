"""TTS-input-only speech fixes and the Whisper check (name pronunciation + dropped phrases).

The script and the captions always keep the ledger's words. Only the text sent to the TTS engine may differ:
  1. caps-colon: eleven_v3 reads an ALL-CAPS phrase that ends in a colon as a script speaker label and silently
     drops it ("CHIRPS LIKE A RINK SHOVEL:"). Such a colon becomes an em dash on the way to the engine.
  2. aliases: a respelling for a player name the voice gets wrong ("Pastrnak" -> "PAHS-ter-nock"). Only aliases
     the Whisper check confirmed are applied (media/pronunciations.json, "confirmed": true).
Engine alignment is mapped back onto the original characters, so captions, word pops and name slams keep the
real spelling and timing.

    uv run --with faster-whisper python -m gmbench_media.pronounce check --run energy-1 --profile r1high
    uv run python -m gmbench_media.pronounce seed --run energy-1        # one cheap LLM call -> candidates
    uv run --with faster-whisper python -m gmbench_media.pronounce fix --run energy-1 --profile r1high \\
        --engine elevenlabs --max-chars 800                             # re-voice failing lines, keep what passes
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

from . import tags as T
from .paths import MEDIA, ROOT, cache_dir, out_dir, rel

PRON_JSON = MEDIA / "pronunciations.json"
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'’][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*|\d+")


# --------------------------------------------------------------------------- TTS input

def _is_caps_word(w: str) -> bool:
    letters = [c for c in w if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def fix_caps_colons(text: str) -> str:
    """'CHIRPS LIKE A RINK SHOVEL: you...' -> 'CHIRPS LIKE A RINK SHOVEL — you...' (an ALL-CAPS run of 2+ words,
    or one all-caps word of 3+ letters, right before a colon). Tags are ignored when looking back."""
    out, last = [], 0
    for m in re.finditer(r":", text):
        i = m.start()
        if i > 0 and text[i - 1].isdigit() and i + 1 < len(text) and text[i + 1].isdigit():
            continue  # 3:15
        before = T.TAG_RE.sub(" ", text[:i]).rstrip()
        words = before.split()
        run: list[str] = []
        for w in reversed(words):
            core = w.strip("\"'“”‘’()[],.!?…")
            if core and _is_caps_word(core):
                run.append(core)
            else:
                break
        if len(run) >= 2 or (len(run) == 1 and sum(c.isalpha() for c in run[0]) >= 3):
            out.append(text[last:i] + " —")
            last = i + 1
    out.append(text[last:])
    return "".join(out)


def load_aliases(confirmed_only: bool = True, speaker: str | None = None) -> dict[str, str]:
    """Confirmed respellings. An entry with a "scope" list applies only to those speakers' voices (a name one voice
    gets wrong can be fine in another, and re-voicing a good take costs money); no scope = every speaker."""
    if not PRON_JSON.exists():
        return {}
    data = json.loads(PRON_JSON.read_text())
    out = {}
    for name, e in (data.get("aliases") or {}).items():
        if not e.get("say") or not (e.get("confirmed") or not confirmed_only):
            continue
        scope = e.get("scope")
        if scope and speaker is not None and speaker not in scope:
            continue
        out[name] = str(e["say"])
    return out


def apply_aliases(text: str, aliases: dict[str, str]) -> tuple[str, list[tuple[str, str]]]:
    """Whole-word, case-insensitive name -> respelling; an ALL-CAPS occurrence gets an all-caps respelling.
    Returns (text, [(spoken_respelling, original_occurrence), ...] in order of appearance)."""
    if not aliases:
        return text, []
    names = sorted(aliases, key=len, reverse=True)
    rx = re.compile(r"(?<![\w'’])(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.IGNORECASE)  # "Makar's" ok
    edits: list[tuple[str, str]] = []

    def sub(m: re.Match) -> str:
        orig = m.group(1)
        key = next(n for n in names if n.lower() == orig.lower())
        say = aliases[key]
        if _is_caps_word(orig.replace(" ", "")):
            say = say.upper()
        edits.append((say, orig))
        return say

    return rx.sub(sub, text), edits


def prepare(text: str, aliases: dict[str, str] | None = None) -> tuple[str, list[tuple[str, str]]]:
    """Script text (words + allowlisted tags) -> TTS input text, plus the alias edits for alignment remapping."""
    t = fix_caps_colons(text)
    return apply_aliases(t, aliases if aliases is not None else load_aliases())


def remap_alignment(al: dict | None, edits: list[tuple[str, str]]) -> dict | None:
    """Map the engine's character alignment of the respelled text back onto the original names (each original
    character gets an evenly spaced slice of the respelling's time span)."""
    if not al or not al.get("chars") or not edits:
        return al
    chars, starts, ends = list(al["chars"]), list(al["starts"]), list(al["ends"])
    pos = 0
    for say, orig in edits:
        s = "".join(chars).lower()
        i = s.find(say.lower(), pos)
        if i < 0:
            return al  # engine normalised the text differently: leave it (captions fall back to proportional)
        j = i + len(say)
        t0, t1 = float(starts[i]), float(ends[j - 1])
        n = len(orig)
        step = (t1 - t0) / max(1, n)
        chars[i:j] = list(orig)
        starts[i:j] = [round(t0 + k * step, 4) for k in range(n)]
        ends[i:j] = [round(t0 + (k + 1) * step, 4) for k in range(n)]
        pos = i + n
    return {"chars": chars, "starts": starts, "ends": ends}


# --------------------------------------------------------------------------- matching helpers

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
       "seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _num_words(n: int) -> list[str]:
    if n < 20:
        return [ONES[n]]
    if n < 100:
        return [TENS[n // 10]] + ([ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [ONES[n // 100], "hundred"] + (_num_words(n % 100) if n % 100 else [])
    return [str(n)]


def norm_words(text: str) -> list[str]:
    """Lowercased words for comparison: tags dropped, digits spelled out, hyphens split, 's kept."""
    out: list[str] = []
    for w in WORD_RE.findall(T.strip(text or "")):
        w = w.lower().replace("’", "'")
        if w.isdigit():
            out += _num_words(int(w))
            continue
        for part in w.split("-"):
            part = part.strip("'")
            if part:
                out.append(part)
    return out


def phonetic(word: str) -> str:
    """Tiny consonant-skeleton key (a rough metaphone): enough to tell 'Pastrnak' ~ 'Pasternak' from 'costar'."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("c(?=[eiy])", "s"), ("c", "k"), ("q", "k"),
                 ("z", "s"), ("dg", "j"), ("gh", "g"), ("sch", "sk"), ("tch", "ch"), ("wh", "w"), ("mc", "mk"),
                 ("mac", "mk"), ("v", "f"), ("j", "y")):
        w = re.sub(a, b, w)
    head, rest = w[0], w[1:]
    rest = re.sub(r"[aeiouhwy]", "", rest)
    return re.sub(r"(.)\1+", r"\1", head + rest)


def name_score(name: str, transcript_words: list[str]) -> tuple[float, str]:
    """How close the transcript comes to the player's SURNAME (the part voices get wrong): best spelling
    similarity over 1-2 word windows ('Mc Arr' -> 'mcarr'). Strict on purpose: 'costar' (Pastrnak), 'Salabrini'
    (Celebrini) and 'McArr' (Makar) all score < 0.8, while 'Pasternak' / 'MacKinnon' spellings pass."""
    target = norm_words(name)
    if not target or not transcript_words:
        return 0.0, ""
    sur = target[-1]
    best, best_txt = 0.0, ""
    for k in range(len(transcript_words)):
        for span in (1, 2):
            win = transcript_words[k:k + span]
            if len(win) < span:
                continue
            txt = "".join(win)
            r = difflib.SequenceMatcher(None, sur, txt).ratio()
            if r > best:
                best, best_txt = r, " ".join(win)
    return round(best, 3), best_txt


def alias_score(say: str, transcript_words: list[str]) -> tuple[float, str]:
    """How close the transcript comes to the respelling itself ('MAY-kar' -> 'maykar' vs heard 'maycar')."""
    base = re.sub(r"[^a-z]", "", say.lower())
    best, best_txt = 0.0, ""
    for target in (base, base + "s"):  # possessives: "MAY-kar's" is heard as "make cars"
        for k in range(len(transcript_words)):
            for span in (1, 2, 3):
                win = transcript_words[k:k + span]
                if len(win) < span:
                    continue
                txt = "".join(win)
                r = difflib.SequenceMatcher(None, target, txt).ratio()
                if r > best:
                    best, best_txt = r, " ".join(win)
    return round(best, 3), best_txt


def confirm_alias(name: str, say: str, before: float, heard_words: list[str]) -> tuple[bool, dict]:
    """A respelling is confirmed when the new take is heard as the respelling (>= 0.75) and it beats the take that
    failed. Speech recognisers spell unfamiliar names phonetically ('maycar', 'pastor knock'), so matching the
    respelling is the evidence that the voice now says what we asked for."""
    real, real_txt = name_score(name, heard_words)
    ali, ali_txt = alias_score(say, heard_words)
    ok = (real >= 0.8) or (ali >= 0.75 and max(real, ali) > before + 0.05)
    return ok, {"score_real": real, "heard_as": real_txt, "score_alias": ali, "heard_alias": ali_txt, "before": before}


def coverage(script_words: list[str], transcript_words: list[str]) -> float:
    """Share of script words present in order in the transcript (fuzzy per word)."""
    if not script_words:
        return 1.0
    sm = difflib.SequenceMatcher(None, script_words, transcript_words, autojunk=False)
    hit = sum(b.size for b in sm.get_matching_blocks())
    # partial credit for near-miss spellings (names, numbers heard as digits, etc.)
    used = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("replace", "delete"):
            continue
        for i in range(i1, i2):
            for j in range(max(0, j1 - 1), min(len(transcript_words), j2 + 1)):
                if j in used:
                    continue
                pair = transcript_words[j] + (transcript_words[j + 1] if j + 1 < len(transcript_words) else "")
                if difflib.SequenceMatcher(None, script_words[i], transcript_words[j]).ratio() >= 0.75 or \
                        difflib.SequenceMatcher(None, script_words[i], pair).ratio() >= 0.8:  # "Overvolts" / "over volts"
                    hit += 1
                    used.add(j)
                    break
    return round(min(1.0, hit / len(script_words)), 3)


# --------------------------------------------------------------------------- whisper

_MODEL = None


def whisper_model(size: str = "small.en"):
    global _MODEL
    if _MODEL is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise SystemExit("faster-whisper missing: run via `uv run --with faster-whisper python -m gmbench_media.pronounce ...`")
        _MODEL = WhisperModel(size, device="cpu", compute_type="int8")
    return _MODEL


def transcribe(path: Path, prompt: str | None = None) -> str:
    """Plain transcription (no initial prompt by default, so the model cannot 'hear' the right name for free)."""
    segs, _ = whisper_model().transcribe(str(path), language="en", beam_size=5, vad_filter=False,
                                         condition_on_previous_text=False, initial_prompt=prompt)
    return " ".join(s.text.strip() for s in segs).strip()


def _cache_key(path: Path) -> str:
    st = path.stat()
    return f"{rel(path)}:{st.st_size}:{st.st_mtime_ns}"


def transcribe_cached(path: Path) -> str:
    cdir = cache_dir("pronounce", "whisper")
    import hashlib
    k = hashlib.sha1(_cache_key(path).encode()).hexdigest()[:20]
    f = cdir / f"{k}.json"
    if f.exists():
        return json.loads(f.read_text())["text"]
    txt = transcribe(path)
    f.write_text(json.dumps({"src": rel(path), "text": txt}))
    return txt


# --------------------------------------------------------------------------- dev alignment (engines without timestamps)

def word_stamps(path: Path) -> list[tuple[str, float, float]]:
    """Whisper word timestamps for a clip (cached)."""
    import hashlib
    cdir = cache_dir("pronounce", "words")
    k = hashlib.sha1(_cache_key(path).encode()).hexdigest()[:20]
    f = cdir / f"{k}.json"
    if f.exists():
        return [tuple(x) for x in json.loads(f.read_text())["words"]]
    segs, _ = whisper_model().transcribe(str(path), language="en", beam_size=5, word_timestamps=True,
                                         condition_on_previous_text=False)
    ws = [(w.word.strip(), float(w.start), float(w.end)) for sg in segs for w in (sg.words or [])]
    f.write_text(json.dumps({"src": rel(path), "words": ws}))
    return ws


def synth_alignment(display_text: str, stamps: list[tuple[str, float, float]], duration: float) -> dict | None:
    """Character alignment for the display text from Whisper word stamps (matched in order, gaps interpolated), in
    the same shape ElevenLabs returns, so captions, beat switches and name slams time the same way."""
    words = display_text.split()
    if not words or not stamps:
        return None
    a = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in words]
    b = [re.sub(r"[^a-z0-9]", "", w[0].lower()) for w in stamps]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    t: list[float | None] = [None] * len(words)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "replace"):
            for k in range(min(i2 - i1, j2 - j1)):
                t[i1 + k] = stamps[j1 + k][1]
    known = [(i, x) for i, x in enumerate(t) if x is not None]
    if not known:
        return None
    for i in range(len(t)):  # interpolate the unmatched words between their matched neighbours
        if t[i] is None:
            lo = max((k for k in known if k[0] < i), default=(-1, 0.0), key=lambda k: k[0])
            hi = min((k for k in known if k[0] > i), default=(len(t), duration), key=lambda k: k[0])
            t[i] = lo[1] + (hi[1] - lo[1]) * (i - lo[0]) / max(1, hi[0] - lo[0])
    ends_w = {}
    for k, (w, a, b) in enumerate(stamps):  # the recogniser's own word end when the word matched
        ends_w[k] = b
    chars, starts, ends = [], [], []
    for i, w in enumerate(words):
        nxt = t[i + 1] if i + 1 < len(words) else duration
        t0 = float(t[i])
        t1 = max(t0 + 0.04, min(float(nxt), t0 + 0.12 * max(1, len(w)) + 0.1))
        # letters share the word's time; vowels hold longer (so lip sync has per-letter timing on dev voices)
        wts = [1.6 if c.lower() in "aeiouy" else (0.0 if not c.isalnum() else 1.0) for c in w]
        tot = sum(wts) or 1.0
        acc = t0
        for c, wt in zip(w, wts):
            d = (t1 - t0) * wt / tot
            chars.append(c)
            starts.append(round(acc, 4))
            ends.append(round(acc + d, 4))
            acc += d
        if i + 1 < len(words):
            chars.append(" ")
            starts.append(round(acc, 4))
            ends.append(round(float(nxt), 4))
    return {"chars": chars, "starts": starts, "ends": ends, "source": "whisper"}


def align_manifest(od: Path, rundown: dict, log=print) -> int:
    """Fill missing alignment in <od>/tts_manifest.json (dev voices) from Whisper word stamps."""
    mpath = od / "tts_manifest.json"
    man = json.loads(mpath.read_text())
    texts = {ln["id"]: ln["display_text"] for ln in rundown["timeline"]}
    n = 0
    for lid, info in man["lines"].items():
        if info.get("alignment") or lid not in texts:
            continue
        al = synth_alignment(texts[lid], word_stamps(ROOT / info["path"]), float(info["duration_s"]))
        if al:
            info["alignment"] = al
            n += 1
    mpath.write_text(json.dumps(man, indent=1))
    log(f"align: {n} lines timed from Whisper word stamps -> {rel(mpath)}")
    return n


# --------------------------------------------------------------------------- check

def names_in_line(line: dict, league) -> list[str]:
    """Players named in a line (full name or surname present in the display text)."""
    text = (line.get("display_text") or "").lower()
    out = []
    for p in league.picks:
        nm = p.player_name
        if not nm or nm.lower().startswith(p.nhl_team.lower() + " "):  # synthetic placeholder names
            continue
        sur = nm.split()[-1].lower()
        if re.search(r"(?<![a-z])" + re.escape(sur) + r"(?![a-z])", text) and nm not in out:
            out.append(nm)
    return out


def check_lines(lines: list[dict], league, audio_key: str = "audio", missing_max: float = 0.2,
                name_min: float = 0.8, log=print) -> dict:
    rows = []
    for ln in lines:
        src = ln.get(audio_key)
        if not src:
            continue
        path = ROOT / src
        if not path.exists():
            continue
        heard = transcribe_cached(path)
        tw = norm_words(heard)
        named = {w for nm in names_in_line(ln, league) for w in norm_words(nm)}  # names are judged separately
        sw = [w for w in norm_words(ln.get("display_text") or "") if w not in named]
        cov = coverage(sw, tw)
        row = {"id": ln["id"], "speaker": ln["speaker"], "text": ln.get("display_text"), "heard": heard,
               "coverage": cov, "dropped": cov < 1.0 - missing_max, "names": []}
        confirmed = load_aliases(speaker=ln["speaker"])
        for nm in names_in_line(ln, league):
            sc, got = name_score(nm, tw)
            item = {"name": nm, "score": sc, "heard_as": got, "ok": sc >= name_min}
            key = next((k for k in confirmed if k.lower() in nm.lower()), None)
            if not item["ok"] and key and key.lower() in (ln.get("display_text") or "").lower():
                asc, atxt = alias_score(confirmed[key], tw)  # voiced from a confirmed respelling: judge against it
                if asc >= 0.75:
                    item.update({"ok": True, "via": f"respelling {confirmed[key]!r}", "alias_score": asc, "heard_as": atxt})
            row["names"].append(item)
        rows.append(row)
    bad_names = [(r["id"], n) for r in rows for n in r["names"] if not n["ok"]]
    dropped = [r["id"] for r in rows if r["dropped"]]
    for r in rows:
        for n in r["names"]:
            mark = "ok " if n["ok"] else "BAD"
            via = f"  via {n['via']}" if n.get("via") else ""
            log(f"  {mark} {r['id']:<18} {n['name']:<22} heard {n['heard_as']!r:<26} ({n['score']:.2f}){via}")
        if r["dropped"]:
            log(f"  DROP {r['id']:<18} coverage {r['coverage']:.0%}: heard {r['heard']!r}")
    return {"schema": "gmbench.pronounce/1", "checked": len(rows), "name_checks": sum(len(r["names"]) for r in rows),
            "name_failures": [{"line": i, **n} for i, n in bad_names], "dropped_phrases": dropped, "lines": rows}


# --------------------------------------------------------------------------- seed (one LLM call)

# non-competing labs only (never a model that plays in the league); non-reasoning models (command-a-plus spends its
# whole budget thinking and returns nothing)
SEED_MODELS = ["mistralai/mistral-medium-3.1", "cohere/command-a", "mistralai/mistral-large-2512"]
SEED_MODEL = SEED_MODELS[0]

SEED_PROMPT = """You help a sports text-to-speech voice say NHL player names correctly.
For each name below, decide whether a typical American-English TTS voice is likely to MISPRONOUNCE it
(Czech/Slovak/Finnish/Swedish/Russian/French-Canadian/Italian names, silent letters, unusual stress).
For those only, give a respelling in plain English syllables that a TTS voice will read correctly, with the
stressed syllable in CAPS and hyphens between syllables (e.g. "Pastrnak" -> "PAHS-ter-nock").
Respell only the part that is at risk: use the surname as the key when only the surname is hard.
Return ONLY a JSON object mapping name-or-surname -> respelling. No commentary. Names:
"""


FOLLOWUP_PROMPT = """A sports text-to-speech voice MISPRONOUNCED these NHL player names; a speech recogniser heard what is
shown after each one. For EACH name give a respelling in plain English syllables that a TTS voice will read
correctly (stressed syllable in CAPS, hyphens between syllables, e.g. "Makar" -> "MAY-kar"). Respell only the part that
is wrong (use the surname as the key when only the surname is wrong). Return ONLY a JSON object name -> respelling.
"""


def seed(league, models: list[str] | None = None, log=print, names: list[str] | None = None,
         heard: dict[str, str] | None = None) -> dict[str, str]:
    """One cheap LLM call over all drafted names (or, with `heard`, over the names the Whisper check failed)."""
    import hashlib

    import httpx

    from .tts import load_env
    models = models or SEED_MODELS
    if names is None:
        names = sorted({p.player_name for p in league.picks if p.player_name and not p.player_name.lower()
                       .startswith(p.nhl_team.lower() + " ")})
    if not names:
        return {}
    prompt = SEED_PROMPT + "\n".join(names)
    if heard:
        prompt = FOLLOWUP_PROMPT + "\n".join(f"{n} (heard: {heard.get(n, '?')})" for n in names)
    key = hashlib.sha1(json.dumps({"m": models, "n": names, "p": prompt}).encode()).hexdigest()[:16]
    cf = cache_dir("pronounce", "seed") / f"{key}.json"
    if cf.exists():
        return json.loads(cf.read_text())["aliases"]
    api_key = load_env().get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY missing from .env")
    last = ""
    for model in models:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 3000, "temperature": 0}
        r = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, timeout=180,
                       headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"})
        if r.status_code != 200:
            last = f"{model}: HTTP {r.status_code}"
            continue
        data = r.json()
        txt = (data["choices"][0]["message"].get("content") or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            last = f"{model}: no JSON in reply"
            continue
        try:
            raw = json.loads(m.group(0))
        except json.JSONDecodeError:
            last = f"{model}: bad JSON"
            continue
        aliases = {str(k).strip(): str(v).strip() for k, v in raw.items() if str(v).strip()}
        usage = data.get("usage") or {}
        cf.write_text(json.dumps({"model": model, "names": names, "aliases": aliases, "usage": usage,
                                  "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1))
        log(f"seed: {len(names)} names -> {len(aliases)} candidate respellings ({model}; "
            f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')} tokens)")
        return aliases
    raise SystemExit(f"seed failed on every model ({last})")


def merge_candidates(cands: dict[str, str], source: str) -> dict:
    data = json.loads(PRON_JSON.read_text()) if PRON_JSON.exists() else {}
    data.setdefault("_doc", "Player-name respellings for TTS input only (captions keep the real spelling). "
                            "Only entries with confirmed=true are applied; the Whisper check confirms them. "
                            "Written by gmbench_media.pronounce.")
    al = data.setdefault("aliases", {})
    for name, say in cands.items():
        e = al.setdefault(name, {})
        if not e.get("confirmed"):
            e.update({"say": say, "source": source, "confirmed": False})
    PRON_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return data


def set_status(name: str, confirmed: bool, evidence: dict, speaker: str | None = None) -> None:
    data = json.loads(PRON_JSON.read_text())
    e = data["aliases"][name]
    first = not e.get("checks")
    e["confirmed"] = confirmed or bool(e.get("confirmed") and e.get("scope") is None and not first)
    if speaker and confirmed and (first or e.get("scope") is not None):
        sc = e.setdefault("scope", [])
        if speaker not in sc:
            sc.append(speaker)
    e.setdefault("checks", []).append(evidence)
    PRON_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- CLI

def _odir(run: str, profile: str | None, short: str | None, name: str | None = None) -> Path:
    if short:
        return out_dir(f"{run}-shorts", short)
    base = run if not profile or profile == "full" else f"{run}-{profile}"
    return out_dir(base, name) if name else out_dir(base)


def _lines_for(run: str, profile: str | None, short: str | None, name: str | None = None) -> tuple[Path, list[dict]]:
    od = _odir(run, profile, short, name)
    cues = od / "cues.json"
    if not cues.exists():
        raise SystemExit(f"missing {rel(cues)}: run tts + mix first")
    return od, json.loads(cues.read_text())["lines"]


def main(argv: list[str] | None = None) -> int:
    from .config import load_config
    from .ledger import load_league
    ap = argparse.ArgumentParser(prog="gmbench_media.pronounce")
    ap.add_argument("command", choices=["check", "seed", "fix", "prepare", "align"])
    ap.add_argument("--run", required=True)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--short", default=None, help="check a short: media/out/<run>-shorts/<name>/")
    ap.add_argument("--name", default=None, help="a named cut: media/out/<run>-<profile>/<name>/ (e.g. chain clips)")
    ap.add_argument("--names", default=None, help="fix: only these player names (comma-separated, e.g. Kaprizov)")
    ap.add_argument("--engine", default="elevenlabs")
    ap.add_argument("--max-chars", type=int, default=0)
    ap.add_argument("--text", default=None)
    args = ap.parse_args(argv)
    L = load_league(args.run)
    if args.command == "prepare":
        print(prepare(args.text or "")[0])
        return 0
    if args.command == "seed":
        cands = seed(L)
        merge_candidates(cands, "seed:llm")
        print(json.dumps(cands, indent=1, ensure_ascii=False))
        return 0
    if args.command == "align":  # dev voices have no timestamps: time words with Whisper (before mix)
        od = _odir(args.run, args.profile, args.short, args.name)
        align_manifest(od, json.loads((od / "rundown.json").read_text()))
        return 0
    od, lines = _lines_for(args.run, args.profile, args.short, args.name)
    if args.command == "check":
        rep = check_lines(lines, L)
        (od / "pronunciation_report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
        print(f"check: {rep['checked']} clips, {rep['name_checks']} name calls, {len(rep['name_failures'])} name "
              f"failures, {len(rep['dropped_phrases'])} clips missing >20% of words -> "
              f"{rel(od / 'pronunciation_report.json')}")
        return 0
    # fix: re-voice lines whose name check failed, with candidate respellings; keep the ones Whisper confirms
    from .tts import get_adapter, load_voice_book
    cfg = load_config(args.profile or "full")
    rep = check_lines(lines, L, log=lambda *_: None)
    failing: dict[str, list[dict]] = {}
    only = {n.strip().lower() for n in (args.names or "").split(",") if n.strip()}
    for f in rep["name_failures"]:
        if only and not any(o in f["name"].lower() for o in only):
            continue  # judged acceptable by ear (e.g. a phonetic spelling of the right sound): leave it
        failing.setdefault(f["line"], []).append(f)
    data = json.loads(PRON_JSON.read_text()) if PRON_JSON.exists() else {"aliases": {}}
    cands = {n: e["say"] for n, e in (data.get("aliases") or {}).items() if e.get("say")}

    def key_for(full: str) -> str | None:
        return next((k for k in sorted(cands, key=len, reverse=True) if k.lower() in full.lower()), None)

    uncovered = sorted({f["name"] for fs in failing.values() for f in fs if key_for(f["name"]) is None})
    if uncovered:  # one follow-up call: respell exactly what the voice got wrong
        heard = {f["name"]: f["heard_as"] for fs in failing.values() for f in fs}
        extra = seed(L, names=uncovered, heard=heard)
        merge_candidates(extra, "seed:llm-followup")
        cands.update(extra)
    from .tts import get_adapter, load_voice_book
    cfg = load_config(args.profile or ("short" if args.short else "full"))
    book = load_voice_book(L)
    ad = get_adapter(args.engine, cfg, book)
    ad.expected_gm = {tid: t.gm_name for tid, t in L.teams.items() if t.has_persona}
    todo = []
    for lid, fs in failing.items():
        ln = next(x for x in lines if x["id"] == lid)
        use = {}
        for f in fs:
            k = key_for(f["name"])
            if k:
                use[k] = cands[k]
        if not use or not ad.has_voice(ln["speaker"]):
            continue
        spoken, _ = prepare(ln["tts_text"], {**load_aliases(speaker=ln["speaker"]), **use})
        todo.append((ln, spoken, use, fs))
    need = 0
    for ln, spoken, _, _ in todo:
        if not ad.paths(spoken, ad.voice_for(ln["speaker"]))[0].exists():
            need += len(ad.engine_text(spoken))
    print(f"fix: {len(failing)} failing lines; {len(todo)} with candidate respellings; {need} billable chars")
    if need > args.max_chars:
        raise SystemExit(f"refusing: {need} chars exceed --max-chars {args.max_chars}")
    verdict: dict[str, list[bool]] = {}
    for ln, spoken, use, fs in todo:
        v = ad.voice_for(ln["speaker"])
        wav = ad.synthesize({**ln, "text": spoken}, v)
        heard = transcribe_cached(wav)
        tw = norm_words(heard)
        for name, say in use.items():
            f = next(f for f in fs if name.lower() in f["name"].lower())
            ok, ev = confirm_alias(f["name"], say, float(f["score"]), tw)
            verdict.setdefault(name, []).append(ok)
            set_status(name, all(verdict[name]), {"line": ln["id"], "speaker": ln["speaker"], "heard": heard, **ev,
                                                  "voice": v.voice_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                       speaker=ln["speaker"])
            print(f"  {'CONFIRMED' if ok else 'rejected '} {name!r} -> {say!r} on {ln['id']}: heard {ev['heard_alias']!r} "
                  f"(alias {ev['score_alias']:.2f}, name {ev['score_real']:.2f}, before {ev['before']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
