"""Wrap text written by other GMs or third parties so it can't pose as league instructions."""
from __future__ import annotations

OPEN, CLOSE = "<<untrusted", "<</untrusted>>"


def wrap(text: str, source: str) -> str:
    safe = (text or "").replace("<<", "‹‹").replace(">>", "››")
    return f"{OPEN} source={source}>>\n{safe}\n{CLOSE}"


INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "system:", "commissioner:", "as the commissioner",
    "you must accept", "new instructions", "developer message", "[system]", "<<untrusted",
)


def looks_like_injection(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in INJECTION_MARKERS)
