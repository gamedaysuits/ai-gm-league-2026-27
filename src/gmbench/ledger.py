"""Append-only, hash-chained event ledger (one JSON object per line).

Every league decision is an event. Each event's `hash` is the sha256 of its
canonical JSON (all fields except `hash`), and each event carries the previous
event's hash, so any edit, deletion, or reordering is detectable at the exact
sequence number. League state is never stored; it is rebuilt by replaying events.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


class ChainError(Exception):
    def __init__(self, seq: int, reason: str) -> None:
        super().__init__(f"ledger chain broken at seq {seq}: {reason}")
        self.seq = seq


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(event: dict[str, Any]) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def append(
        self,
        type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        session: str | None = None,
        refs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                head = self._head()
                event: dict[str, Any] = {
                    "seq": (head["seq"] + 1) if head else 1,
                    "ts": utc_now(),
                    "type": type,
                    "actor": actor,
                    "session": session,
                    "payload": payload,
                    "refs": refs or {},
                    "prev_hash": head["hash"] if head else GENESIS,
                }
                event["hash"] = event_hash(event)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(canonical(event) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                return event
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def events(self, types: set[str] | None = None) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                event = json.loads(line)
                if types is None or event["type"] in types:
                    yield event

    def verify(self) -> tuple[int, str]:
        """Check every hash and link. Returns (event count, head hash)."""
        prev, count = GENESIS, 0
        for event in self.events():
            count += 1
            if event["seq"] != count:
                raise ChainError(event["seq"], f"expected seq {count}")
            if event["prev_hash"] != prev:
                raise ChainError(event["seq"], "prev_hash does not match previous event")
            if event_hash(event) != event["hash"]:
                raise ChainError(event["seq"], "hash does not match contents")
            prev = event["hash"]
        return count, prev

    def repair_torn_tail(self) -> bool:
        """Drop a partially written final line (crash mid-write). Returns True if repaired."""
        if not self.path.exists():
            return False
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return False
        keep = data[: data.rfind(b"\n") + 1]
        self.path.write_bytes(keep)
        return True

    def _head(self) -> dict[str, Any] | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with open(self.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 65536)
            while True:
                fh.seek(size - block)
                chunk = fh.read(block)
                lines = chunk.rstrip(b"\n").split(b"\n")
                if len(lines) > 1 or block == size:
                    last = lines[-1]
                    break
                block = min(size, block * 2)
        try:
            return json.loads(last)
        except json.JSONDecodeError as exc:
            raise ChainError(-1, "torn final line; run repair_torn_tail()") from exc
