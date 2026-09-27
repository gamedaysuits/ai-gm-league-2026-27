from gmbench.export.transcripts import redact_text


def test_redacts_third_party_text_but_keeps_status() -> None:
    text = ("status: injury: Out — Draisaitl (lower body) will miss 2-3 weeks per the coach\n"
            "news 2026-09-25: Oilers finalize opening-night roster\n"
            "2026-09-25 [EDM] Oilers finalize opening-night roster\n"
            "12345 | Leon Draisaitl | EDM C 30y | 25-26: ... | injury: Out | available")
    out = redact_text(text)
    assert "lower body" not in out and "finalize" not in out
    assert "status: injury: Out" in out and "injury: Out | available" in out
