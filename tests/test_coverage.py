"""Weekly coverage on a simulated trade week: facts, article, images and the tracker feed agree with the ledger."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gmbench.config import ROOT, load_config
from gmbench.coverage import article, curate, images
from gmbench.coverage.facts import clean, week_facts, week_number
from gmbench.coverage.run import build_week, front_office_feed, tracker_page_body
from gmbench.state import replay

LIVE = ROOT / "runs" / "live" / "ledger" / "league.jsonl"
WEEK = date(2026, 10, 12)


def _events() -> list[dict]:
    if not LIVE.exists():
        pytest.skip("no live ledger")
    evs = [json.loads(x) for x in LIVE.read_text().splitlines() if x.strip()]
    evs = [e for e in evs if not (e["type"] in ("WEEK_OPENED", "WEEK_LOCKED") and e["payload"]["week"] >= WEEK.isoformat())]
    state = replay(evs)
    a, b = "gemini", "grok"
    give, get = state.teams[a].roster[-1], state.teams[b].roster[-1]
    groups = {str(give): state.player_group[give], str(get): state.player_group[get]}
    seq = evs[-1]["seq"]

    def ev(kind: str, payload: dict) -> dict:
        nonlocal seq
        seq += 1
        return {"type": kind, "payload": payload, "seq": seq, "actor": "test", "ts": "2026-10-12T12:00:00Z"}
    w = WEEK.isoformat()
    return evs + [
        ev("WEEK_OPENED", {"week": w, "week_end": "2026-10-18", "lock_at": "2026-10-12T16:45:00Z", "priority": [], "trades_open": True}),
        ev("CHAT_POSTED", {"team": "opus", "week": w, "message": "[confident] Gemini's lead is a rounding error."}),
        ev("REPORT_SUBMITTED", {"team": "qwen", "kind": "presser", "week": w, "line": "Bought low. Sold nothing."}),
        ev("TRADE_PROPOSED", {"trade_id": f"{w}-T01", "week": w, "proposer": a, "recipient": b, "give": [give], "get": [get],
                              "message": "Grok, your winger needs a team that scores.", "counter_of": None}),
        ev("TRADE_PROPOSED", {"trade_id": f"{w}-T02", "week": w, "proposer": "opus", "recipient": "kimi",
                              "give": [state.teams["opus"].roster[0]], "get": [state.teams["kimi"].roster[0]],
                              "message": "Fair is fair.", "counter_of": None}),
        ev("TRADE_RESPONDED", {"trade_id": f"{w}-T01", "team": b, "action": "accept", "message": "Fine. Take him."}),
        ev("TRADE_RESPONDED", {"trade_id": f"{w}-T02", "team": "kimi", "action": "reject", "message": "Not a chance."}),
        ev("TRADE_EXECUTED", {"trade_id": f"{w}-T01", "proposer": a, "recipient": b, "give": [give], "get": [get],
                              "groups": groups, "week": w}),
        ev("WEEK_LOCKED", {"week": w}),
    ]


def test_clean_drops_voice_tags() -> None:
    assert clean("[confident] Third place.  [pause] Imagine.") == "Third place. Imagine."
    assert week_number(date(2026, 10, 5)) == 2 and week_number(WEEK) == 3


def test_trade_week_article_and_feed(tmp_path: Path) -> None:
    evs = _events()
    cfg = load_config()
    f = week_facts(evs, cfg, week_start=WEEK, root=ROOT, run_dir=tmp_path)
    assert f.locked and f.trades_open and f.number == 3
    assert [t.status for t in f.trades] == ["executed", "rejected"]
    assert {ln.kind for ln in f.lines} == {"chat", "presser", "pitch", "reply"}
    assert next(ln for ln in f.lines if ln.kind == "chat").text == "Gemini's lead is a rounding error."

    verdicts = {ln.id: {"text": ln.text, "ok": True, "score": 6.0 - i * 0.1} for i, ln in enumerate(f.lines)}
    best = curate.featured(f, verdicts)
    assert len({ln.team for ln, _ in best}) == len(best)  # one line per model
    a = article.build(f, best, set(verdicts), prev_handle="ai-gm-league-week-2-2026-27", first_trade_ever=True)
    assert a.handle == "ai-gm-league-week-3-2026-27"
    assert "Make the League's First Trade" in a.title and len(a.seo_description) <= 158
    assert "Offers that didn't land" in a.body and "turned down" in a.body
    assert "Grok, your winger needs a team that scores." in a.body  # judged-OK trade talk is quoted
    assert '/blogs/news/ai-gm-league-week-2-2026-27' in a.body and '"@type": "FAQPage"' in a.body
    assert "{{img:standings}}" in a.body

    images.cover(f, article.headline(f, True), tmp_path / "c.png")
    images.standings_chart(f, tmp_path / "s.png")
    images.quote_card(f, best[0][0], tmp_path / "q.png")
    assert all((tmp_path / n).stat().st_size > 10_000 for n in ("c.png", "s.png", "q.png"))

    run = tmp_path / "run"
    (run / "coverage" / WEEK.isoformat()).mkdir(parents=True)
    (run / "coverage" / WEEK.isoformat() / "judge.json").write_text(json.dumps(verdicts))
    feed = front_office_feed(evs, cfg, run)
    assert feed[0]["number"] == 3 and feed[0]["trades"][0]["status"] == "executed"
    assert feed[0]["trades"][0]["talk"] and feed[0]["highlights"]


def test_build_week_refuses_an_unlocked_week(tmp_path: Path) -> None:
    evs = [e for e in _events() if e["type"] != "WEEK_LOCKED" or e["payload"]["week"] != WEEK.isoformat()]
    with pytest.raises(SystemExit):
        build_week(evs, load_config(), tmp_path, WEEK, judge=False)


def test_tracker_page_gets_a_static_standings_table(tmp_path: Path) -> None:
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "tracker.json").write_text(json.dumps({"last_game_date": "2026-10-11", "teams": [
        {"id": "gemini", "display": "Gemini 3.1 Pro", "lab": "Google", "franchise": "Flin Flon Frostbots", "points": 50}]}))
    body = tracker_page_body(tmp_path)
    assert "Standings through 2026-10-11" in body and "Gemini 3.1 Pro (Google)" in body
