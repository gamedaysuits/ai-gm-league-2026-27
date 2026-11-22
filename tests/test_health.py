"""The hockey-page health check: each check, its timing allowances, the alert email, and a replay of the Nov 2026 outage."""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from gmbench import health
from gmbench.health import AI_GM, PAGES, POOL, Problem, check, due_monday, due_night, render, send_email

# Sunday Nov 22 2026, 6:17 PM Alberta: the evening run. Saturday's games are due; Sunday's aren't.
NOW = datetime(2026, 11, 23, 1, 17, tzinfo=UTC)
NIGHTS = {date(2026, 11, d): n for d, n in [(15, 5), (16, 8), (17, 10), (18, 4), (19, 12), (20, 0), (21, 14), (22, 9)]}
FRESH = "2026-11-22T17:10:24Z"


def _pages() -> dict[str, str]:
    return {name: f'<div id="gdt" data-src="{src}"></div>' for name, (_, src) in PAGES.items()}


def _feeds(**over: Any) -> dict[str, Any]:
    tracker = {"generated_at": FRESH, "last_game_date": "2026-11-21", "season": {"start": "2026-09-29", "end": "2027-04-10"},
               "front_office": [{"week": "2026-11-16", "number": 8, "article": None},
                                {"week": "2026-10-05", "number": 2, "article": {"url": "https://example/week-2"}}]}
    pool = {"generated_at": FRESH, "last_game_date": "2026-11-21", "unresolved": []}
    feeds = {"standings": {"generated_at": FRESH}, "tracker": tracker, "pool": pool}
    for key, patch in over.items():
        feeds[key] = None if patch is None else {**feeds[key], **patch}
    return feeds


def _run(**kw: Any) -> list[Problem]:
    args = {"pages": _pages(), "feeds": _feeds(), "nights": NIGHTS, "now": NOW} | kw
    return check(**args)


def test_healthy_pages_pass() -> None:
    assert _run() == []


def test_replay_of_the_november_2026_pool_outage() -> None:
    """The pool export froze at Nov 18 while every workflow run stayed green. The evening of Nov 20 must alert."""
    now = datetime(2026, 11, 21, 1, 17, tzinfo=UTC)
    feeds = _feeds(tracker={"generated_at": "2026-11-20T18:40:16Z", "last_game_date": "2026-11-19"},
                   pool={"generated_at": "2026-11-19T19:03:53Z", "last_game_date": "2026-11-18"})
    problems = check(pages=_pages(), feeds=feeds, nights=NIGHTS, now=now)
    assert {p.page for p in problems} == {POOL}
    assert any("haven't been rebuilt in 30 hours" in p.text for p in problems)
    assert any("scored through Wed Nov 18" in p.text and "Thu Nov 19" in p.text for p in problems)


def test_game_night_is_due_at_22_utc_the_next_day() -> None:
    assert due_night(NIGHTS, datetime(2026, 11, 22, 21, 59, tzinfo=UTC)) == date(2026, 11, 19)  # Fri had no games
    assert due_night(NIGHTS, datetime(2026, 11, 22, 22, 0, tzinfo=UTC)) == date(2026, 11, 21)
    assert due_night({date(2026, 11, 21): 0}, NOW) is None


def test_league_missing_a_night() -> None:
    problems = _run(feeds=_feeds(tracker={"last_game_date": "2026-11-19"}))
    assert [p.page for p in problems] == [AI_GM]
    assert "scored through Thu Nov 19" in problems[0].text and "Sat Nov 21" in problems[0].text


def test_a_night_with_no_games_is_not_missing() -> None:
    sat_afternoon = datetime(2026, 11, 21, 23, 0, tzinfo=UTC)  # Friday the 20th had no games
    feeds = _feeds(tracker={"last_game_date": "2026-11-19", "generated_at": "2026-11-21T17:40:09Z"},
                   pool={"last_game_date": "2026-11-19", "generated_at": "2026-11-21T17:40:09Z"})
    assert _run(feeds=feeds, now=sat_afternoon) == []


def test_pool_behind_the_league() -> None:
    problems = _run(feeds=_feeds(pool={"last_game_date": "2026-11-19"}))
    assert [(p.page, "Sat Nov 21" in p.text) for p in problems] == [(POOL, True)]


def test_stale_feed_alerts_even_without_games() -> None:
    problems = _run(feeds=_feeds(tracker={"generated_at": "2026-11-21T17:40:09Z"}), nights={})
    assert [p.page for p in problems] == [AI_GM] and "31 hours" in problems[0].text


def test_unreachable_nhl_skips_only_the_game_night_check() -> None:
    assert _run(feeds=_feeds(pool={"last_game_date": "2026-11-15"}), nights=None) == [
        Problem(POOL, "It's missing game nights: scored through Sun Nov 15, but games through Sat Nov 21 are in.")]
    assert _run(nights=None) == []


def test_page_and_feed_failures() -> None:
    pages = _pages() | {AI_GM: None, POOL: "<p>edited page</p>"}
    problems = _run(pages=pages, feeds=_feeds(pool=None))
    texts = [(p.page, p.text.split(" (")[0]) for p in problems]
    assert (AI_GM, "The page doesn't load") in texts
    assert (POOL, "The page no longer points at its live data") in texts
    assert (POOL, "Its data feed doesn't load") in texts


def test_unmatched_pool_players() -> None:
    problems = _run(feeds=_feeds(pool={"unresolved": [{"name": "Ivar Stenberg"}, {"name": "Q. Hughes"}]}))
    assert problems == [Problem(POOL, "These drafted players don't match an NHL player, so they score 0: Ivar Stenberg, Q. Hughes.")]


def test_front_office_and_article() -> None:
    tue_evening = datetime(2026, 11, 25, 1, 17, tzinfo=UTC)  # Monday the 23rd's front office is due by now
    assert due_monday(tue_evening) == date(2026, 11, 23)
    assert due_monday(datetime(2026, 11, 24, 0, 59, tzinfo=UTC)) == date(2026, 11, 16)
    feeds = _feeds(tracker={"generated_at": "2026-11-24T19:00:00Z", "last_game_date": "2026-11-23"},
                   pool={"generated_at": "2026-11-24T19:00:00Z", "last_game_date": "2026-11-23"})
    nights = NIGHTS | {date(2026, 11, 23): 7}
    missed = check(pages=_pages(), feeds=feeds, nights=nights, now=tue_evening)
    assert [p.page for p in missed] == [AI_GM] and "didn't run for the week of Mon Nov 23" in missed[0].text

    feeds["tracker"]["front_office"] = [{"week": "2026-11-23", "number": 9, "article": None}, *feeds["tracker"]["front_office"]]
    unposted = check(pages=_pages(), feeds=feeds, nights=nights, now=tue_evening)
    assert [p.text.split(" was")[0] for p in unposted] == ["The week 9 article"]
    for key in ("tracker", "pool"):
        feeds[key]["generated_at"] = "2026-11-25T19:00:00Z"
    assert check(pages=_pages(), feeds=feeds, nights=nights, now=datetime(2026, 11, 26, 1, 0, tzinfo=UTC)) == []  # alerts twice, then stops

    feeds["tracker"]["front_office"][0]["article"] = {"url": "https://example/week-9"}
    assert check(pages=_pages(), feeds=feeds, nights=nights, now=tue_evening) == []


def test_no_front_office_alerts_out_of_season() -> None:
    feeds = _feeds(tracker={"season": {"start": "2026-09-29", "end": "2026-11-01"}})
    assert _run(feeds=feeds) == []


def test_render() -> None:
    subject, body = render([Problem(POOL, "A."), Problem(POOL, "B."), Problem(AI_GM, "C.")], run_url="https://run/1")
    assert subject == "GDS hockey alert: 3 problems on the tracker pages"
    assert body.index(POOL) < body.index("  - A.") < body.index("  - B.") < body.index(AI_GM) < body.index("  - C.")
    assert "https://www.gamedaysuits.ca/pages/auction-pool-tracker" in body and "Run log: https://run/1" in body
    assert render([], test=True)[0] == "GDS hockey alert: test (all clear)"
    assert render([Problem(POOL, "A.")], test=True)[0] == "GDS hockey alert: test, 1 problem on the tracker pages"


class FakeSMTP:
    sent: list[Any] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.where = (host, port)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.creds = (user, password)

    def send_message(self, msg: Any) -> None:
        FakeSMTP.sent.append((self.where, self.creds, msg))


def test_send_email() -> None:
    FakeSMTP.sent = []
    assert send_email("s", "b", {}, smtp=FakeSMTP) is False
    assert send_email("s", "b", {"GMAIL_ADDRESS": "me@example.com"}, smtp=FakeSMTP) is False
    env = {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}
    assert send_email("Subject", "Body", env, smtp=FakeSMTP) is True
    where, creds, msg = FakeSMTP.sent[0]
    assert where == ("smtp.gmail.com", 465) and creds == ("me@example.com", "abcdefghijklmnop")
    assert msg["To"] == "me@example.com" and msg["Subject"] == "Subject" and msg.get_content().strip() == "Body"


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    def fake_send(subject: str, body: str, env: Any) -> bool:
        if not env.get("GMAIL_APP_PASSWORD"):
            return False
        sent.append((subject, body))
        return True

    monkeypatch.setattr(health, "send_email", fake_send)
    return sent


ENV = {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "x",
       "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9"}


def test_main_exit_status_and_email(monkeypatch: pytest.MonkeyPatch, outbox: list) -> None:
    monkeypatch.setattr(health, "gather", lambda now: {"pages": _pages(), "feeds": _feeds(), "nights": NIGHTS})
    assert health.main(now=NOW, env=ENV) == 0 and outbox == []
    assert health.main(test_email=True, now=NOW, env=ENV) == 0 and outbox[-1][0].endswith("(all clear)")
    assert health.main(test_email=True, now=NOW, env={}) == 1  # a test with no email configured must not look like success

    monkeypatch.setattr(health, "gather", lambda now: {"pages": _pages(), "feeds": _feeds(pool=None), "nights": NIGHTS})
    assert health.main(now=NOW, env=ENV) == 1
    assert "1 problem" in outbox[-1][0] and "https://github.com/o/r/actions/runs/9" in outbox[-1][1]
    assert health.main(now=NOW, env={}) == 1  # no email configured: still fails, so GitHub's own email goes out


def test_a_crashing_check_still_emails(monkeypatch: pytest.MonkeyPatch, outbox: list) -> None:
    def boom(now: datetime) -> dict:
        raise ValueError("schema changed")

    monkeypatch.setattr(health, "gather", boom)
    assert health.main(now=NOW, env=ENV) == 1
    assert "crashed: ValueError('schema changed')" in outbox[-1][1]
