"""Publishable copies of GM session transcripts.

Everything a GM saw and said is kept, except third-party text (ESPN injury notes
and news headlines), which is replaced with a placeholder. Injury *status* stays.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

NOTE = re.compile(r"(status: injury: [^\n—]+) — [^\n]*")
NEWS_LINE = re.compile(r"^(news \d{4}-\d{2}-\d{2}: ).*$", re.M)
HEADLINE_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2} \[[^\]]*\] ).*$", re.M)


def redact_text(text: str) -> str:
    text = NOTE.sub(r"\1 — [injury note withheld: third-party text]", text)
    text = NEWS_LINE.sub(r"\1[headline withheld: third-party text]", text)
    return HEADLINE_ROW.sub(r"\1[headline withheld: third-party text]", text)


def redact_transcript(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    msgs = []
    for m in doc.get("messages", []):
        m = dict(m)
        if isinstance(m.get("content"), str) and m.get("role") in ("tool", "user"):
            m["content"] = redact_text(m["content"])
        msgs.append(m)
    out["messages"] = msgs
    out["redaction"] = "ESPN injury notes and news headlines withheld; injury status kept."
    return out


def export_public_transcripts(run_root: Path) -> Path:
    src, dst = run_root / "transcripts", run_root / "public" / "transcripts"
    index = []
    for path in sorted(src.rglob("*.json.gz")):
        with gzip.open(path, "rb") as fh:
            raw = fh.read()
        doc = json.loads(raw)
        public = redact_transcript(doc)
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(public, sort_keys=True, ensure_ascii=False).encode()
        with gzip.open(target, "wb", compresslevel=9) as fh:
            fh.write(body)
        index.append({"session_id": doc.get("session_id"), "team": doc.get("team"), "model": doc.get("model"),
                      "outcome": doc.get("outcome"), "path": str(rel), "private_sha256": hashlib.sha256(raw).hexdigest(),
                      "public_sha256": hashlib.sha256(body).hexdigest()})
    (dst / "index.json").write_text(json.dumps(index, indent=1))
    return dst
