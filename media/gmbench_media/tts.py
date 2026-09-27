"""TTS adapters behind one interface, with content-hash caching and per-line loudness normalisation.

    adapter = get_adapter("say", cfg)            # or "elevenlabs", "silent"
    voice = adapter.voice_for("qwen")            # media/voices/voices.json (+ media/voices_dev.json for `say`)
    wav = adapter.synthesize(line, voice)        # -> normalised 48 kHz mono WAV at -19 LUFS (cached)

Caching: the raw engine output is keyed by (engine, model, voice, settings, text) so changing
normalisation never re-bills a paid engine; the normalised WAV is keyed by (raw key, norm params).
Secrets come from the project .env and are never printed or written anywhere.
"""

from __future__ import annotations

import abc
import base64
import concurrent.futures as cf
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import audio
from .paths import DEV_VOICES_JSON, ENV_FILE, VOICES_JSON, cache_dir, rel

PIPE_VERSION = 1
TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]")

SAY_HOST = "Daniel"
SAY_MALE = ["Reed (English (US))", "Eddy (English (US))", "Rocko (English (US))", "Fred", "Ralph", "Albert",
            "Rishi", "Reed (English (UK))", "Eddy (English (UK))", "Rocko (English (UK))", "Grandpa (English (US))"]
SAY_FEMALE = ["Samantha", "Karen", "Moira", "Tessa", "Flo (English (US))", "Sandy (English (US))",
              "Shelley (English (US))", "Kathy", "Flo (English (UK))", "Shelley (English (UK))"]
VOICE_ALIAS = {"autodraft": "host"}  # the control bot is voiced by the host
FEMALE_CUES = ("mezzo", "alto", "soprano", "feminine", "woman", " she ", " her ", "female")
MALE_CUES = ("baritone", "bass", "tenor", "masculine", " man ", " he ", " his ", "male")


def load_env() -> dict[str, str]:
    """Read the project .env without exporting or printing anything."""
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(ENV_FILE).items() if v} if ENV_FILE.exists() else {}


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Voice:
    engine: str
    speaker: str
    voice_id: str | None
    settings: dict = field(default_factory=dict)
    accent: str = ""  # voices.json accent_tag, e.g. "[Canadian accent]": prepended to the TTS input only


# --------------------------------------------------------------------------- voice book

def infer_gender(desc: str) -> str | None:
    d = f" {desc.lower()} "
    f = sum(c in d for c in FEMALE_CUES)
    m = sum(c in d for c in MALE_CUES)
    if f > m:
        return "f"
    if m > f:
        return "m"
    return None


def default_dev_book(league) -> dict:
    say = {"host": SAY_HOST}
    males, females = list(SAY_MALE), list(SAY_FEMALE)
    flip = True
    for tid, t in league.teams.items():
        if t.is_bot:
            continue  # the host speaks for the control bot
        g = infer_gender(t.p("voice_description") or "") if t.has_persona else None
        if g is None:
            g = "f" if flip else "m"
            flip = not flip
        pool = females if g == "f" else males
        say[tid] = pool.pop(0) if pool else SAY_HOST
    return {
        "_doc": "Zero-cost dev voices for the `say` engine (macOS). Production voices live in media/voices/voices.json "
                "(ElevenLabs eleven_v3, written by media/voices/design.py) and are merged in at load time.",
        "say": say,
    }


def load_voice_book(league=None) -> dict:
    """Dev book (say voices, generated once) merged with the production book (read-only)."""
    if DEV_VOICES_JSON.exists():
        dev = json.loads(DEV_VOICES_JSON.read_text())
    elif league is not None:
        dev = default_dev_book(league)
        DEV_VOICES_JSON.write_text(json.dumps(dev, indent=2, ensure_ascii=False) + "\n")
    else:
        dev = {"say": {"host": SAY_HOST}}
    book = dict(dev)
    if VOICES_JSON.exists():
        prod = json.loads(VOICES_JSON.read_text())
        for k, v in prod.items():
            if k not in ("_doc", "say"):
                book[k] = v
    return book


# --------------------------------------------------------------------------- adapters

class TTSAdapter(abc.ABC):
    engine = "base"
    paid = False
    raw_ext = "wav"

    def __init__(self, cfg: dict, book: dict):
        self.cfg = cfg
        self.tcfg = cfg["tts"]
        self.book = book
        self.sr = int(cfg["sample_rate"])
        self.raw_dir = cache_dir("tts", self.engine, "raw")
        self.norm_dir = cache_dir("tts", self.engine, "norm")

    # -- interface
    @abc.abstractmethod
    def voice_for(self, speaker: str) -> Voice: ...

    @abc.abstractmethod
    def render_raw(self, text: str, voice: Voice, out: Path) -> dict | None:
        """Write engine-native audio to `out`; return optional alignment {chars, starts, ends}."""

    def model_id(self) -> str:
        return self.engine

    def has_voice(self, speaker: str) -> bool:
        return True

    def engine_text(self, text: str) -> str:
        """Default: engines without tag support speak the words only."""
        return re.sub(r"\s+", " ", TAG_RE.sub("", text)).strip()

    def speech_input(self, text: str, voice: Voice) -> str:
        """Exactly what the engine receives (engine text, plus the voice's accent tag where supported)."""
        return self.engine_text(text)

    # -- keys
    def raw_key(self, text: str, voice: Voice) -> str:
        return sha({"v": PIPE_VERSION, "engine": self.engine, "model": self.model_id(), "voice": voice.voice_id,
                    "settings": voice.settings, "text": self.speech_input(text, voice)})

    def norm_params(self) -> dict:
        t = self.tcfg
        return {"lufs": t["line_lufs"], "thr": t["trim_silence_db"], "head": t["head_pad_ms"],
                "tail": t["tail_pad_ms"], "sr": self.sr}

    def paths(self, text: str, voice: Voice) -> tuple[Path, Path, Path]:
        rk = self.raw_key(text, voice)
        nk = sha({"raw": rk, "norm": self.norm_params()})
        raw = self.raw_dir / rk[:2] / f"{rk}.{self.raw_ext}"
        return raw, self.norm_dir / f"{nk}.wav", self.norm_dir / f"{nk}.json"

    def is_cached_raw(self, line: dict, voice: Voice) -> bool:
        return self.paths(line["text"], voice)[0].exists()

    def synthesize(self, line: dict, voice: Voice) -> Path:
        """line -> normalised WAV path (content-hash cached)."""
        raw, norm, meta = self.paths(line["text"], voice)
        if norm.exists() and meta.exists():
            return norm
        align = None
        align_path = raw.with_suffix(".align.json")
        if not raw.exists():
            raw.parent.mkdir(parents=True, exist_ok=True)
            tmp = raw.with_name(raw.name + ".part")
            align = self.render_raw(line["text"], voice, tmp)
            tmp.replace(raw)
            if align:
                align_path.write_text(json.dumps(align))
        elif align_path.exists():
            align = json.loads(align_path.read_text())
        info = normalize(raw, norm, self.norm_params(), align)
        info.update({"engine": self.engine, "model": self.model_id(), "voice": voice.voice_id,
                     "speaker": voice.speaker, "raw": rel(raw)})
        tmp_meta = meta.with_name(meta.name + ".tmp")
        tmp_meta.write_text(json.dumps(info))
        tmp_meta.replace(meta)
        return norm

    def meta(self, line: dict, voice: Voice) -> dict:
        return json.loads(self.paths(line["text"], voice)[2].read_text())

    def derive_prefix(self, line: dict, parent: dict, voice: Voice) -> str | None:
        """A line whose speech is an exact, sentence-final prefix of a cached parent take (the cold-open hook quoting
        the call) is CUT from that take instead of re-synthesized: the same delivery, the same words, zero engine
        characters. Writes the line's raw audio + alignment into the cache; returns the cut's last words, or None."""
        raw_l = self.paths(line["text"], voice)[0]
        raw_p = self.paths(parent["text"], voice)[0]
        ap = raw_p.with_suffix(".align.json")
        if raw_l.exists() or not raw_p.exists() or not ap.exists():
            return None
        sl, sp_ = self.speech_input(line["text"], voice), self.speech_input(parent["text"], voice)
        if (len(sp_) <= len(sl) or not sp_.startswith(sl) or not sp_[len(sl)].isspace()
                or not re.search(r"[.!?…][\"”')\]]*$", sl)):
            return None  # only at a sentence boundary
        al = json.loads(ap.read_text())
        n = len(sl)
        if "".join(al["chars"][:n]) != sl:
            return None
        end = float(al["ends"][n - 1])
        nxt = next((float(al["starts"][k]) for k in range(n, len(al["chars"])) if not al["chars"][k].isspace()), end + 0.5)
        cut = max(end + 0.06, min(nxt - 0.03, end + 0.3))
        raw_l.parent.mkdir(parents=True, exist_ok=True)
        tmp = raw_l.with_name(raw_l.name + ".part")
        fmt = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"], "wav": ["-f", "wav"],
               "aiff": ["-f", "aiff"]}.get(self.raw_ext, ["-f", self.raw_ext])
        subprocess.run([audio.FFMPEG, "-v", "error", "-y", "-i", str(raw_p), "-t", f"{cut:.3f}",
                        "-af", f"afade=t=out:st={max(0.0, cut - 0.04):.3f}:d=0.04", *fmt, str(tmp)], check=True)
        tmp.replace(raw_l)
        raw_l.with_suffix(".align.json").write_text(json.dumps(
            {"chars": al["chars"][:n], "starts": al["starts"][:n], "ends": al["ends"][:n], "derived_from": raw_p.name}))
        return sl[-40:]


class MacSay(TTSAdapter):
    """Zero-cost dev voices via the macOS `say` command."""

    engine = "say"
    raw_ext = "aiff"
    _installed: set[str] | None = None

    def model_id(self) -> str:
        return f"say@{self.tcfg['say']['rate_wpm']}/{self.tcfg['say'].get('hype_rate_wpm')}"

    def rate_for(self, text: str) -> int:
        from .tags import HYPE, tags_in
        hype = any(t in HYPE for t in tags_in(text)) or text.count("!") >= 2
        return int(self.tcfg["say"].get("hype_rate_wpm") or self.tcfg["say"]["rate_wpm"]) if hype else \
            int(self.tcfg["say"]["rate_wpm"])

    def raw_key(self, text: str, voice: Voice) -> str:
        return sha({"v": PIPE_VERSION, "engine": self.engine, "voice": voice.voice_id, "rate": self.rate_for(text),
                    "text": self.engine_text(text)})

    @classmethod
    def installed(cls) -> set[str]:
        if cls._installed is None:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
            cls._installed = {re.split(r"\s{2,}", ln.strip())[0] for ln in out.splitlines() if ln.strip()}
        return cls._installed

    def voice_for(self, speaker: str) -> Voice:
        name = (self.book.get("say") or {}).get(VOICE_ALIAS.get(speaker, speaker)) or SAY_HOST
        if name not in self.installed():
            name = SAY_HOST if speaker == "host" else "Samantha"
        return Voice("say", speaker, name, {"rate": self.tcfg["say"]["rate_wpm"]})

    def render_raw(self, text: str, voice: Voice, out: Path) -> dict | None:
        if not shutil.which("say"):
            raise SystemExit("macOS `say` not available")
        with tempfile.TemporaryDirectory() as td:
            txt = Path(td) / "line.txt"
            txt.write_text(self.engine_text(text))
            aiff = Path(td) / "out.aiff"
            audio.run(["say", "-v", voice.voice_id, "-r", str(self.rate_for(text)), "-o", str(aiff), "-f", str(txt)])
            shutil.move(str(aiff), out)
        return None


class Silent(TTSAdapter):
    """Zero-cost, zero-time placeholder: digital silence of the estimated line length."""

    engine = "silent"

    def voice_for(self, speaker: str) -> Voice:
        return Voice("silent", speaker, "silence", {"cps": self.cfg["script"]["est_chars_per_sec"]})

    def render_raw(self, text: str, voice: Voice, out: Path) -> dict | None:
        n = int(self.sr * max(0.6, len(self.engine_text(text)) / float(voice.settings["cps"])))
        audio.write_wav(out, np.zeros(n, dtype=np.float32), self.sr)
        return None


class ElevenLabsV3(TTSAdapter):
    """ElevenLabs text-to-speech (model eleven_v3). Voice ids come from media/voices.json['elevenlabs']."""

    engine = "elevenlabs"
    paid = True
    raw_ext = "mp3"
    API = "https://api.elevenlabs.io/v1"
    _no_stamps = False

    def __init__(self, cfg: dict, book: dict):
        super().__init__(cfg, book)
        self.ecfg = self.tcfg["elevenlabs"]
        fmt = self.ecfg["output_format"]
        self.pcm_rate = int(fmt.split("_")[1]) if fmt.startswith("pcm_") else None
        if self.pcm_rate:
            self.raw_ext = "wav"  # raw s16le PCM is wrapped into a WAV container on arrival
        self.safe = {t.lower() for t in self.tcfg["safe_tags"]}

    def model_id(self) -> str:
        return self.ecfg["model_id"]

    expected_gm: dict[str, str] = {}

    def _entry(self, speaker: str) -> dict:
        e = (self.book.get("elevenlabs") or {}).get(VOICE_ALIAS.get(speaker, speaker)) or {}
        return e if isinstance(e, dict) else {"voice_id": e}

    def has_voice(self, speaker: str) -> bool:
        e = self._entry(speaker)
        if not e.get("voice_id"):
            return False
        want, got = self.expected_gm.get(speaker), e.get("gm_name")
        return not (want and got and want.strip().lower() != str(got).strip().lower())  # voice made for another persona

    def voice_for(self, speaker: str) -> Voice:
        entry = self._entry(speaker)
        vid = entry.get("voice_id")
        if not vid:
            raise SystemExit(f"no ElevenLabs voice_id for speaker {speaker!r} in {rel(VOICES_JSON)}")
        settings = {k: self.ecfg[k] for k in ("stability", "similarity_boost", "style", "use_speaker_boost")}
        settings.update(entry.get("settings") or {})
        accent = str(entry.get("accent_tag") or "").strip()
        if accent and not accent.startswith("["):
            accent = f"[{accent}]"
        return Voice("elevenlabs", speaker, vid, settings, accent)

    def speech_input(self, text: str, voice: Voice) -> str:
        body = self.engine_text(text)
        return f"{voice.accent} {body}".strip() if voice.accent else body

    def engine_text(self, text: str) -> str:
        """Keep delivery tags from the safe list only (v3 audio tags); drop anything else."""
        def keep(m: re.Match) -> str:
            return m.group(0) if m.group(1).strip().lower() in self.safe else ""
        return re.sub(r"\s+", " ", TAG_RE.sub(keep, text)).strip()

    def render_raw(self, text: str, voice: Voice, out: Path) -> dict | None:
        import httpx

        key = load_env().get("ELEVENLABS_API_KEY")
        if not key:
            raise SystemExit("ELEVENLABS_API_KEY missing from .env")
        stamps = bool(self.ecfg.get("timestamps")) and not ElevenLabsV3._no_stamps
        url = f"{self.API}/text-to-speech/{voice.voice_id}" + ("/with-timestamps" if stamps else "")
        body = {
            "text": self.speech_input(text, voice),
            "model_id": self.model_id(),
            "voice_settings": voice.settings,
            "seed": int(sha(text)[:8], 16),
        }
        if self.ecfg.get("language_code"):
            body["language_code"] = self.ecfg["language_code"]
        headers = {"xi-api-key": key, "content-type": "application/json"}
        last = None
        for attempt in range(5):
            try:
                r = httpx.post(url, params={"output_format": self.ecfg["output_format"]}, json=body,
                               headers=headers, timeout=float(self.ecfg.get("timeout_s", 120)))
            except httpx.HTTPError as e:  # network hiccup: retry
                last = f"{type(e).__name__}"
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                self.log_usage(voice, body["text"], r.headers.get("request-id") or r.headers.get("x-request-id"))
                data = r.json() if stamps else None
                blob = base64.b64decode(data["audio_base64"]) if stamps else r.content
                if self.pcm_rate:
                    pcm = np.frombuffer(blob, dtype="<i2").astype(np.float32) / 32768.0
                    audio.write_wav(out, pcm, self.pcm_rate, bits=16)
                else:
                    out.write_bytes(blob)
                if stamps:
                    al = data.get("alignment") or {}
                    chars = list(al.get("characters") or [])
                    starts = list(al.get("character_start_times_seconds") or [])
                    ends = list(al.get("character_end_times_seconds") or [])
                    pre = f"{voice.accent} " if voice.accent else ""
                    if pre and "".join(chars[:len(pre)]) == pre:  # drop the accent tag from the caption timing
                        chars, starts, ends = chars[len(pre):], starts[len(pre):], ends[len(pre):]
                    return {"chars": chars, "starts": starts, "ends": ends}
                return None
            last = f"HTTP {r.status_code}: {r.text[:300].replace(key, '***')}"
            if stamps and r.status_code in (400, 404, 405, 422):
                # alignment endpoint unavailable for this model/plan: plain audio, proportional caption timing
                ElevenLabsV3._no_stamps = True
                stamps = False
                url = f"{self.API}/text-to-speech/{voice.voice_id}"
                continue
            if r.status_code in (400, 422) and set(body["voice_settings"]) - {"stability", "similarity_boost"}:
                # eleven_v3 can reject some settings: retry once with the core pair (same as media/voices/design.py)
                body["voice_settings"] = {k: v for k, v in body["voice_settings"].items()
                                          if k in ("stability", "similarity_boost")}
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt + 1)
                continue
            break
        raise RuntimeError(f"ElevenLabs synthesis failed for {voice.speaker}: {last}")


    def log_usage(self, voice: Voice, text: str, request_id: str | None) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "speaker": voice.speaker,
               "voice_id": voice.voice_id, "model": self.model_id(), "chars": len(text), "request_id": request_id}
        with (cache_dir("tts", self.engine) / "usage.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")


class OpenRouterTTS(TTSAdapter):  # pragma: no cover - candidates in tonight's bake-off
    """Placeholder for Gemini 3.8 Flash TTS / Fish S2.1 Pro via OpenRouter (not wired until the engine is chosen)."""

    engine = "openrouter"
    paid = True

    def voice_for(self, speaker: str) -> Voice:
        entry = (self.book.get(self.engine) or {}).get(speaker) or {}
        return Voice(self.engine, speaker, entry.get("voice_id"), entry.get("settings") or {})

    def render_raw(self, text: str, voice: Voice, out: Path) -> dict | None:
        raise NotImplementedError("OpenRouter TTS adapter: implement once the bake-off picks Gemini TTS or Fish S2.1")


ADAPTERS = {"say": MacSay, "silent": Silent, "elevenlabs": ElevenLabsV3, "openrouter": OpenRouterTTS}


def get_adapter(engine: str, cfg: dict, book: dict) -> TTSAdapter:
    if engine not in ADAPTERS:
        raise SystemExit(f"unknown TTS engine {engine!r}; choose from {', '.join(ADAPTERS)}")
    return ADAPTERS[engine](cfg, book)


# --------------------------------------------------------------------------- normalisation

def normalize(raw: Path, out: Path, p: dict, align: dict | None = None) -> dict:
    sr = int(p["sr"])
    x = audio.decode(raw, sr=sr)
    a, b = audio.trim_bounds(x, sr, p["thr"], p["head"], p["tail"])
    y = audio.trim_silence(x, sr, p["thr"], p["head"], p["tail"])
    loud = audio.ebur128(y, sr) if len(y) > int(0.45 * sr) else {"I": None}
    lufs_in = loud["I"]
    if lufs_in is None or lufs_in < -60:
        lufs_in = audio.rms_dbfs(y) + 3.0 if np.any(y) else None  # short/quiet line: RMS-based estimate
    gain_db = (p["lufs"] - lufs_in) if lufs_in is not None else 0.0
    gain_db = float(np.clip(gain_db, -30.0, 30.0))
    z = audio.soft_limit(y * np.float32(10 ** (gain_db / 20)), -1.5)
    audio.write_wav(out, z, sr, bits=16)
    info = {"duration_s": round(len(z) / sr, 4), "lufs_in": None if lufs_in is None else round(float(lufs_in), 2), "gain_db": round(gain_db, 2),
            "peak_dbfs": round(float(20 * np.log10(np.max(np.abs(z)) + 1e-9)), 2), "trim_start_s": round(a / sr, 4)}
    if align and align.get("chars") and align.get("starts"):
        off = a / sr
        dur = len(z) / sr  # a take's alignment can run past its audio (the last character's long tail): clamp
        info["alignment"] = {"chars": align["chars"],
                             "starts": [round(min(max(t - off, 0.0), dur), 4) for t in align["starts"]],
                             "ends": [round(min(max(t - off, 0.0), dur), 4) for t in align["ends"]]}
    return info


# --------------------------------------------------------------------------- tight lines with comic timing

def take_durations(cfg: dict, league, speaker: str, text: str, parts: list[str]) -> list[float] | None:
    """Spoken length of each sentence in the cached ElevenLabs take of `text` (None when there is no take).
    Reads the cache only: never synthesizes."""
    from . import timing as TM
    from .pronounce import load_aliases, prepare
    a = get_adapter("elevenlabs", cfg, load_voice_book(league))
    if not a.has_voice(speaker):
        return None
    v = a.voice_for(speaker)
    aliases = load_aliases(speaker=speaker)
    src, _ = prepare(text, aliases)
    raw = a.paths(src, v)[0]
    ap = raw.with_suffix(".align.json")
    if not ap.exists():
        return None
    al = json.loads(ap.read_text())
    return TM.part_durations(a.speech_input(src, v), al, [a.speech_input(prepare(x, aliases)[0], v) for x in parts])


def derive_beats_line(ln: dict, a: "TTSAdapter", v: "Voice", dev: "TTSAdapter", cfg: dict, cache_only: bool,
                      plan: bool, log=print, body: dict | None = None) -> tuple[dict | None, int]:
    """A line with a beat map (ln["beats"]): its kept sentences, cut from a take, with the comic beats.
    -> (manifest entry | None when planning, billable characters). Modes: 'take' (a cached take of the model's whole
    line: 0 characters), 'tight' (paid engine, no cached take: synthesize only the kept sentences), 'dev' (free /
    --cache-only: each kept sentence alone on the dev voice; exact boundaries, layout timing)."""
    from . import timing as TM
    from .pronounce import load_aliases, prepare, remap_alignment
    b = ln["beats"]
    parts, labels, keep = b["parts"], b["labels"], list(b["keep"])
    aliases = load_aliases(speaker=ln["speaker"])
    src_speech, _ = prepare(b["source"], aliases)
    prepped = [prepare(x, aliases) for x in parts]
    free = a.engine in ("say", "silent")
    mode = "dev"
    take_raw = None
    bkeep: list[int] | None = None  # a cold open cut from its body line's tight take: the body's kept sentences
    if a.paid:
        raw = a.paths(src_speech, v)[0]
        if raw.exists() and raw.with_suffix(".align.json").exists():
            mode, take_raw = "take", raw
        elif body is not None and (body.get("beats") or {}).get("keep") and set(keep) <= set(body["beats"]["keep"]):
            bk = list(body["beats"]["keep"])
            braw = a.paths(" ".join(prepped[i][0] for i in bk), v)[0]
            if braw.exists() and braw.with_suffix(".align.json").exists():
                mode, take_raw, bkeep = "body", braw, bk
            elif plan and not cache_only:  # the body is synthesized first in this batch: the hook costs nothing
                return None, 0
            elif not cache_only:
                mode = "tight"
        elif not cache_only:
            mode = "tight"
    if plan:
        if mode == "tight" and sorted(keep) == list(range(len(parts))):
            return None, len(a.speech_input(src_speech, v))
        tight_text = " ".join(prepped[i][0] for i in keep)
        return None, (len(a.speech_input(tight_text, v)) if mode == "tight" else 0)
    sr = int(cfg["sample_rate"])
    ad = a if (mode in ("take", "tight", "body") or free) else dev
    vv = v if ad is a else ad.voice_for(ln["speaker"])
    tempo = float((cfg.get("mix") or {}).get("tempo") or 1.0)  # beats are sized for the screen, after atempo
    key = TM.derived_key({"v": 4, "tempo": round(tempo, 3), "mode": mode, "engine": ad.engine, "voice": vv.voice_id,
                          "src": src_speech,
                          "parts": [x[0] for x in prepped], "keep": keep,
                          "labels": [[x["role"], x.get("kicker"), x.get("spice")] for x in labels], "id": ln["id"]})
    ddir = ad.norm_dir / "derived"
    norm, meta = ddir / f"{key}.wav", ddir / f"{key}.json"
    if norm.exists() and meta.exists():
        return json.loads(meta.read_text()), 0
    billed = 0
    segments = None
    if mode == "body":  # the cold open's punch, cut from the body line's own tight take
        pcm = TM.read_pcm(take_raw, sr)
        al = json.loads(take_raw.with_suffix(".align.json").read_text())
        segments = TM.cut_from_take(pcm, sr, a.speech_input(" ".join(prepped[i][0] for i in bkeep), v), al,
                                    [a.speech_input(prepped[i][0], v) for i in bkeep], [bkeep.index(i) for i in keep],
                                    [labels[i] for i in bkeep], [parts[i] for i in bkeep], trim_lead=True)
        if segments:
            for sg, idx in zip(segments, keep):
                rm = remap_alignment({"chars": sg["chars"], "starts": sg["starts"], "ends": sg["ends"]}, prepped[idx][1])
                sg["chars"], sg["starts"], sg["ends"] = rm["chars"], rm["starts"], rm["ends"]
        else:
            log(f"tts: {ln['id']}: the body take's alignment does not match; falling back")
            mode = "tight" if not cache_only else "dev"
    if mode == "take":
        pcm = TM.read_pcm(take_raw, sr)
        al = json.loads(take_raw.with_suffix(".align.json").read_text())
        segments = TM.cut_from_take(pcm, sr, a.speech_input(src_speech, v), al,
                                    [a.speech_input(x[0], v) for x in prepped], keep, labels, parts,
                                    trim_lead=ln["kind"] == "hook")
        if segments:
            for sg, idx in zip(segments, keep):
                rm = remap_alignment({"chars": sg["chars"], "starts": sg["starts"], "ends": sg["ends"]}, prepped[idx][1])
                sg["chars"], sg["starts"], sg["ends"] = rm["chars"], rm["starts"], rm["ends"]
        else:
            log(f"tts: {ln['id']}: the take's alignment does not match its sentences; falling back")
            mode = "tight" if not cache_only else "dev"
    if mode == "body" and segments is None:
        mode = "tight" if not cache_only else "dev"
    if segments is None and mode == "tight" and sorted(keep) == list(range(len(parts))):
        # every sentence kept (the complete show): voice the line exactly as written -- the same take the shorts
        # later cut from (0 characters for them)
        a.synthesize({**ln, "text": src_speech}, v)
        billed = len(a.speech_input(src_speech, v))
        raw = a.paths(src_speech, v)[0]
        if raw.with_suffix(".align.json").exists():
            mode, take_raw = "take", raw
            pcm = TM.read_pcm(raw, sr)
            al = json.loads(raw.with_suffix(".align.json").read_text())
            segments = TM.cut_from_take(pcm, sr, a.speech_input(src_speech, v), al,
                                        [a.speech_input(x[0], v) for x in prepped], keep, labels, parts)
            if segments:
                for sg, idx in zip(segments, keep):
                    rm = remap_alignment({"chars": sg["chars"], "starts": sg["starts"], "ends": sg["ends"]},
                                         prepped[idx][1])
                    sg["chars"], sg["starts"], sg["ends"] = rm["chars"], rm["starts"], rm["ends"]
    if segments is None and mode == "tight":
        tight_text = " ".join(prepped[i][0] for i in keep)
        a.synthesize({**ln, "text": tight_text}, v)
        billed = len(a.speech_input(tight_text, v))
        traw = a.paths(tight_text, v)[0]
        pcm = TM.read_pcm(traw, sr)
        ap = traw.with_suffix(".align.json")
        if ap.exists():
            al = json.loads(ap.read_text())
            segments = TM.cut_from_take(pcm, sr, a.speech_input(tight_text, v), al,
                                        [a.speech_input(prepped[i][0], v) for i in keep], list(range(len(keep))),
                                        [labels[i] for i in keep], [parts[i] for i in keep],
                                        trim_lead=ln["kind"] == "hook")
            if segments:
                for sg, idx in zip(segments, keep):
                    rm = remap_alignment({"chars": sg["chars"], "starts": sg["starts"], "ends": sg["ends"]},
                                         prepped[idx][1])
                    sg["chars"], sg["starts"], sg["ends"] = rm["chars"], rm["starts"], rm["ends"]
    if segments is None:  # dev: each kept sentence alone (free): exact boundaries; tags cannot be voiced
        mode = "dev"
        ad = a if free else dev
        vv = ad.voice_for(ln["speaker"]) if ad is not a else v
        segments = []
        for k, idx in enumerate(keep):
            npath = ad.synthesize({**ln, "id": f"{ln['id']}:{idx}", "text": parts[idx]}, vv)
            x, _ = audio.read_wav(npath)
            speech = ad.speech_input(parts[idx], vv)
            wrote = bool(re.search(r"\[pause\]", parts[idx])) or (
                idx > 0 and bool(re.search(r"\[pause\]\s*$", parts[idx - 1])))
            al = TM.linear_alignment(speech, len(x) / sr, 0.02)
            segments.append({"pcm": x, "chars": al["chars"], "starts": al["starts"], "ends": al["ends"],
                             "role": labels[idx]["role"], "label": labels[idx],
                             "gap_before": (0.4 if (wrote and k > 0) else None), "pause_before": wrote})
    y, al, marks, end_beat = TM.assemble(segments, sr, ln["id"], tempo=tempo)
    ddir.mkdir(parents=True, exist_ok=True)
    raw_out = ddir / f"{key}.raw.wav"
    audio.write_wav(raw_out, y, sr, bits=32)
    info = normalize(raw_out, norm, ad.norm_params(), al)
    raw_out.unlink(missing_ok=True)
    off = float(info.get("trim_start_s") or 0.0)
    for mk in marks:
        for k2 in ("punch_start", "kicker_start", "kicker_end"):
            mk[k2] = round(max(0.0, mk[k2] - off), 4)
    entry = {"path": rel(norm), "duration_s": info["duration_s"], "speaker": ln["speaker"], "engine": ad.engine,
             "voice": vv.voice_id, "lufs_in": info.get("lufs_in"), "gain_db": info.get("gain_db"),
             "alignment": info.get("alignment"),
             "beats": {"mode": mode, "marks": marks, "end_beat": end_beat, "keep": keep,
                       "take": rel(take_raw) if take_raw else None}}
    meta.write_text(json.dumps(entry))
    log(f"tts: {ln['id']}: tight edit ({mode}) {len(keep)}/{len(parts)} sentences, {len(marks)} punch beats, "
        f"{billed} chars")
    return entry, billed


# --------------------------------------------------------------------------- batch

def run_tts(rundown: dict, cfg: dict, engine: str, league=None, only: set[str] | None = None,
            max_chars: int | None = None, jobs: int | None = None, fallback: str | None = "say",
            log=print, plan: bool = False, cache_only: bool = False) -> dict:
    """Synthesize every line. Speakers without a voice on `engine` use the `fallback` engine (logged)."""
    book = load_voice_book(league)
    primary = get_adapter(engine, cfg, book)
    if league is not None:
        primary.expected_gm = {tid: t.gm_name for tid, t in league.teams.items() if t.has_persona}
    host_reads = fallback == "host"  # emergency: the host's voice reads the GM's own words (unchanged)
    fb = get_adapter(fallback, cfg, book) if fallback and fallback not in (engine, "host") else None
    lines = [ln for ln in rundown["timeline"] if not only or ln["id"] in only]
    adapters: dict[str, TTSAdapter] = {}
    voices: dict[str, Voice] = {}
    for sp in sorted({ln["speaker"] for ln in lines}):
        if primary.has_voice(sp):
            adapters[sp], voices[sp] = primary, primary.voice_for(sp)
        elif host_reads and primary.has_voice("host"):
            v = primary.voice_for("host")
            adapters[sp], voices[sp] = primary, Voice(v.engine, sp, v.voice_id, v.settings)
        elif fb is not None:
            adapters[sp], voices[sp] = fb, fb.voice_for(sp)
        else:
            raise SystemExit(f"speaker {sp!r} has no {engine} voice and no fallback engine is set")
    fell_back = sorted(sp for sp in adapters if not primary.has_voice(sp))
    if fell_back:
        log(f"tts: no {engine} voice for {', '.join(fell_back)} -> {fallback}")
    # TTS input only (script + captions keep the ledger words): caps-colon guard + confirmed name respellings
    from .pronounce import load_aliases, prepare, remap_alignment
    edits: dict[str, list] = {}
    rewritten: set[str] = set()
    spoken_lines = []
    for ln in lines:
        speak, ed = prepare(ln["text"], load_aliases(speaker=ln["speaker"]))
        edits[ln["id"]] = ed
        if speak != ln["text"]:
            rewritten.add(ln["id"])
        if speak != ln["text"]:
            log(f"tts: {ln['id']}: speech-only rewrite ({'respelling' if ed else 'caps-colon'})")
        spoken_lines.append({**ln, "text": speak})
    lines = spoken_lines
    beat_ids = [ln["id"] for ln in lines if ln.get("beats")]
    beat_results: dict[str, dict] = {}
    beat_billed = 0
    if beat_ids:
        dev_ad = fb or get_adapter("say", cfg, book)
        orig = {x["id"]: x for x in rundown["timeline"]}
        beat_ids = sorted(beat_ids, key=lambda lid: orig[lid].get("kind") == "hook")  # hooks cut from their body
        body_of = {lid: next((x for x in rundown["timeline"] if x["id"] != lid and x.get("beats")
                              and x.get("kind") != "hook" and x["speaker"] == orig[lid]["speaker"]
                              and list(x.get("refs") or []) == list(orig[lid].get("refs") or [])), None)
                   for lid in beat_ids if orig[lid].get("kind") == "hook"}
        if not plan:  # the budget BEFORE anything is synthesized: a tight line is voiced inside its derivation
            need = sum(derive_beats_line(orig[lid], adapters[orig[lid]["speaker"]], voices[orig[lid]["speaker"]],
                                         dev_ad, cfg, cache_only, True, log, body=body_of.get(lid))[1]
                       for lid in beat_ids)
            if need:
                if max_chars is None:
                    raise SystemExit(f"{engine} is a paid engine: the tight edit would bill {need} characters. "
                                     f"Re-run with --max-chars N (N >= {need}) to allow it.")
                if need > max_chars:
                    raise SystemExit(f"refusing: {need} billable chars (tight edit) exceed --max-chars {max_chars}")
        for lid in beat_ids:
            ln0 = orig[lid]
            entry, billed = derive_beats_line(ln0, adapters[ln0["speaker"]], voices[ln0["speaker"]], dev_ad, cfg,
                                              cache_only, plan, log, body=body_of.get(lid))
            beat_billed += billed
            if entry:
                beat_results[lid] = entry
        lines = [ln for ln in lines if ln["id"] not in set(beat_ids)]
    # a cold-open hook quotes a call in this run: cut it from that take when the take is cached (no new characters)
    derived: dict[str, str] = {}
    for ln in lines:
        if ln.get("kind") != "hook":
            continue
        a, v = adapters[ln["speaker"]], voices[ln["speaker"]]
        if a.is_cached_raw(ln, v):
            continue
        for par in lines:
            if par is ln or par["speaker"] != ln["speaker"] or not a.is_cached_raw(par, v):
                continue
            if plan:  # a plan never writes: just check that the cut would be possible
                sl, sp_ = a.speech_input(ln["text"], v), a.speech_input(par["text"], v)
                ok = (len(sp_) > len(sl) and sp_.startswith(sl) and sp_[len(sl)].isspace()
                      and re.search(r"[.!?…][\"”')\]]*$", sl))
                if ok:
                    derived[ln["id"]] = par["id"]
                    break
                continue
            tail = a.derive_prefix(ln, par, v)
            if tail:
                derived[ln["id"]] = par["id"]
                log(f"tts: {ln['id']}: cut from the cached {par['id']} take at a sentence boundary "
                    f"(ends '...{tail}'; 0 chars)")
                break
    todo = [ln for ln in lines if not adapters[ln["speaker"]].is_cached_raw(ln, voices[ln["speaker"]])
            and ln["id"] not in derived]
    paid_todo = [ln for ln in todo if adapters[ln["speaker"]].paid]
    dev_lines: set[str] = set()
    if cache_only and paid_todo:  # rehearsal: never bill; the uncached lines get the dev voice instead
        fb_dev = fb or get_adapter("say", cfg, book)
        for ln in paid_todo:
            dev_lines.add(ln["id"])
        by_sp_dev = sorted({ln["speaker"] for ln in paid_todo})
        log(f"tts: --cache-only: {len(paid_todo)} uncached {engine} lines -> {fb_dev.engine} dev voices "
            f"({', '.join(by_sp_dev)}); {len(lines) - len(todo)} cached {engine} takes reused")
        for ln in paid_todo:
            adapters[ln["id"]] = fb_dev
            voices[ln["id"]] = fb_dev.voice_for(ln["speaker"])
        todo = [ln for ln in todo if ln["id"] not in dev_lines] + [
            ln for ln in paid_todo if not fb_dev.is_cached_raw(ln, voices[ln["id"]])]
        paid_todo = []
    todo_chars = sum(len(adapters[ln["speaker"]].speech_input(ln["text"], voices[ln["speaker"]])) for ln in paid_todo)
    todo_chars += beat_billed
    if plan:
        by_sp: dict[str, int] = {}
        for ln in paid_todo:
            by_sp[ln["speaker"]] = by_sp.get(ln["speaker"], 0) + len(adapters[ln["speaker"]].speech_input(ln["text"], voices[ln["speaker"]]))
        log(f"tts[{engine}] plan: {len(lines)} lines, {len(todo)} uncached, {todo_chars} billable chars "
            f"({', '.join(f'{k} {v}' for k, v in sorted(by_sp.items()))})")
        return {"plan": True, "billable_chars": todo_chars, "uncached": len(todo), "by_speaker": by_sp}
    if paid_todo:
        if max_chars is None:
            raise SystemExit(f"{engine} is a paid engine: {len(paid_todo)} uncached lines / {todo_chars} chars would be "
                             f"billed. Re-run with --max-chars N (N >= {todo_chars}) to allow it.")
        if todo_chars > max_chars:
            raise SystemExit(f"refusing: {todo_chars} uncached billable chars exceed --max-chars {max_chars}")
    log(f"tts[{engine}]: {len(lines)} lines, {len(todo)} to synthesize ({todo_chars} billable chars), "
        f"{len(lines) - len(todo)} cached")
    workers = jobs or (3 if paid_todo else 6)
    results: dict[str, dict] = {}
    t0 = time.time()

    # one synthesis per unique (voice, text): identical lines share a cache entry, so never race on it
    def ad(ln: dict):  # the adapter + voice for this line (a --cache-only dev line overrides its speaker's)
        key = ln["id"] if ln["id"] in dev_lines else ln["speaker"]
        return adapters[key], voices[key]

    unique: dict[str, dict] = {}
    for ln in lines:
        a, v = ad(ln)
        unique.setdefault(str(a.paths(ln["text"], v)[1]), ln)

    def work(ln: dict) -> None:
        a, v = ad(ln)
        a.synthesize(ln, v)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, unique.values()))
    for ln in lines:
        a, v = ad(ln)
        path = a.paths(ln["text"], v)[1]
        m = a.meta(ln, v)
        results[ln["id"]] = {"path": rel(path), "duration_s": m["duration_s"], "speaker": ln["speaker"],
                             "engine": a.engine, "voice": v.voice_id, "lufs_in": m.get("lufs_in"),
                             "gain_db": m.get("gain_db"), "alignment": remap_alignment(m.get("alignment"), edits[ln["id"]])}
        if ln["id"] in rewritten or v.accent:
            results[ln["id"]]["tts_input"] = a.speech_input(ln["text"], v)  # speech-only rewrite / accent tag
        if ln["id"] in derived:
            results[ln["id"]]["derived_from"] = derived[ln["id"]]  # the same take, trimmed at a sentence end
    results.update(beat_results)
    log(f"tts[{engine}]: done in {time.time() - t0:.1f}s")
    total = sum(r["duration_s"] for r in results.values())
    engines = {sp: (f"{a.engine}:host-voice" if host_reads and sp in fell_back else a.engine)
               for sp, a in adapters.items() if sp not in dev_lines}
    return {"schema": "gmbench.tts/1", "engine": engine, "fallback": fallback, "model": primary.model_id(),
            "speaker_engines": engines, "line_lufs": cfg["tts"]["line_lufs"], "speech_s": round(total, 2),
            "lines": results, "billable_chars": todo_chars}
