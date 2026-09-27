"""Small audio toolkit: ffmpeg decode, WAV I/O, EBU R128 measurement, silence trim, soft limiting."""

from __future__ import annotations

import re
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np

FFMPEG = "ffmpeg"


def run(cmd: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-1500:]
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    return proc


def decode(path: Path, sr: int = 48000, channels: int = 1) -> np.ndarray:
    """Any ffmpeg-readable file -> float32 array (n,) or (n, channels)."""
    out = run([FFMPEG, "-v", "error", "-i", str(path), "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]).stdout
    x = np.frombuffer(out, dtype=np.float32).copy()
    return x.reshape(-1, channels) if channels > 1 else x


def write_wav(path: Path, x: np.ndarray, sr: int, bits: int = 16) -> None:
    """Write mono/stereo float [-1,1] as PCM16/PCM24 or float32 WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(x, dtype=np.float32)
    ch = 1 if x.ndim == 1 else x.shape[1]
    tmp = path.with_suffix(path.suffix + ".tmp")
    if bits == 32:
        data = x.astype("<f4").tobytes()
        with tmp.open("wb") as fh:
            fh.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
            fh.write(b"fmt " + struct.pack("<IHHIIHH", 16, 3, ch, sr, sr * ch * 4, ch * 4, 32))
            fh.write(b"data" + struct.pack("<I", len(data)) + data)
    else:
        y = np.clip(x, -1.0, 1.0)
        if bits == 16:
            data = (y * 32767.0).round().astype("<i2").tobytes()
            width = 2
        elif bits == 24:
            i32 = (y * 8388607.0).round().astype("<i4")
            b = i32.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3]
            data, width = b.tobytes(), 3
        else:
            raise ValueError(bits)
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(width)
            w.setframerate(sr)
            w.writeframes(data)
    tmp.replace(path)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read PCM16/24/32-int or float32 WAV -> float32 (n,) or (n, ch)."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"not a WAV: {path}")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f"bad WAV: {path}")
    tag, ch, sr, _, _, bits = fmt
    if tag == 0xFFFE:  # extensible: trust bits
        tag = 3 if bits == 32 and len(data) % 4 == 0 and _looks_float(data) else 1
    if tag == 3:
        x = np.frombuffer(data, dtype="<f4").astype(np.float32)
    elif bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        i = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        i = np.where(i & 0x800000, i - 0x1000000, i)
        x = i.astype(np.float32) / 8388608.0
    elif bits == 32:
        x = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV bits={bits}")
    return (x.reshape(-1, ch) if ch > 1 else x), sr


def _looks_float(data: bytes) -> bool:
    f = np.frombuffer(data[: 4 * 4096], dtype="<f4")
    return bool(np.all(np.isfinite(f)) and np.max(np.abs(f), initial=0) <= 4.0)


_I_RE = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
_LRA_RE = re.compile(r"LRA:\s+(-?[\d.]+)\s+LU")
_TP_RE = re.compile(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


def ebur128(src: Path | np.ndarray, sr: int = 48000, true_peak: bool = False) -> dict:
    """EBU R128 via ffmpeg's ebur128 filter. Returns {I, LRA, TP} (None when unmeasurable)."""
    filt = "ebur128=framelog=quiet" + (":peak=true" if true_peak else "")
    if isinstance(src, np.ndarray):
        ch = 1 if src.ndim == 1 else src.shape[1]
        proc = run([FFMPEG, "-hide_banner", "-nostats", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
                    "-af", filt, "-f", "null", "-"], input_bytes=src.astype("<f4").tobytes())
    else:
        proc = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(src), "-af", filt, "-f", "null", "-"])
    err = proc.stderr.decode(errors="replace")
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else err

    def num(rx: re.Pattern) -> float | None:
        m = rx.search(summary)
        if not m or m.group(1) == "-inf":
            return None
        return float(m.group(1))

    return {"I": num(_I_RE), "LRA": num(_LRA_RE), "TP": num(_TP_RE) if true_peak else None}


def trim_bounds(x: np.ndarray, sr: int, thr_db: float = -50.0, head_ms: int = 25, tail_ms: int = 60) -> tuple[int, int]:
    """Sample range [a, b) that keeps speech plus small pads (10 ms RMS windows above thr_db)."""
    win = max(1, sr // 100)
    n = (len(x) // win) * win
    if n == 0:
        return 0, len(x)
    rms = np.sqrt(np.mean(x[:n].reshape(-1, win) ** 2, axis=1) + 1e-12)
    loud = np.nonzero(20 * np.log10(rms) > thr_db)[0]
    if loud.size == 0:
        return 0, len(x)
    a = max(0, loud[0] * win - int(sr * head_ms / 1000))
    b = min(len(x), (loud[-1] + 1) * win + int(sr * tail_ms / 1000))
    return int(a), int(b)


def trim_silence(x: np.ndarray, sr: int, thr_db: float = -50.0, head_ms: int = 25, tail_ms: int = 60) -> np.ndarray:
    if x.size == 0:
        return x
    a, b = trim_bounds(x, sr, thr_db, head_ms, tail_ms)
    y = x[a:b].copy()
    fade = min(len(y) // 4, int(sr * 0.004))
    if fade > 1:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    return y


def soft_limit(x: np.ndarray, ceiling_db: float = -1.5) -> np.ndarray:
    """Gentle tanh knee above 80% of the ceiling; transparent below it."""
    c = 10 ** (ceiling_db / 20)
    knee = 0.8 * c
    y = x.copy()
    over = np.abs(y) > knee
    if np.any(over):
        s = np.sign(y[over])
        a = np.abs(y[over]) - knee
        y[over] = s * (knee + (c - knee) * np.tanh(a / (c - knee)))
    return y


def rms_dbfs(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12))


def duration_s(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
               str(path)]).stdout.decode().strip()
    return float(out or 0.0)
