"""Daily health check for the two public hockey pages on gamedaysuits.ca, with an email alert.

It tests what a visitor actually sees, end to end, not whether a workflow step exited 0
(the auction pool was broken for three days in November 2026 while every run was green):

* both Shopify pages load and still point at their live data feeds;
* the feeds load from GitHub, exactly as the pages fetch them;
* the feeds were rebuilt recently and include every NHL game night that is due;
* every drafted pool player still matches an NHL player;
* the Monday front office ran, and its blog article was posted.

Any problem fails the run, which makes GitHub send its "Run failed" email, and, when
GMAIL_ADDRESS and GMAIL_APP_PASSWORD are set, sends a plain-English alert email.

Allowances, because GitHub starts scheduled runs 4-10 hours late:

* game night D is due at 22:00 UTC on D+1 (the scorer is scheduled 10:23 UTC and has
  always finished by about 21:15 UTC);
* feeds must have been rebuilt in the last 26 hours (the scorer rewrites them daily,
  even after nights with no games);
* Monday M's front office is due at 01:00 UTC on M+1;
* a week's blog article is checked only for two days after it is due, so a missing
  article alerts twice, not every day for a week.
"""
from __future__ import annotations

import os
import smtplib
import time as _time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import httpx

RAW = "https://raw.githubusercontent.com/gamedaysuits/ai-gm-league-2026-27/main/"
AI_GM = "AI GM League Tracker"
POOL = "Auction Pool Tracker"
CHECK = "Health check"
PAGES: dict[str, tuple[str, str]] = {  # page name -> (public URL, the data-src the page must carry)
    AI_GM: ("https://www.gamedaysuits.ca/pages/ai-gm-draft-tracker", RAW + "runs/live/exports/"),
    POOL: ("https://www.gamedaysuits.ca/pages/auction-pool-tracker", RAW + "exports/pools/auction-pool-2026-27.json"),
}
FEEDS: dict[str, tuple[str, str]] = {  # feed key -> (path under RAW, page it drives)
    "standings": ("runs/live/exports/standings.json", AI_GM),
    "tracker": ("runs/live/exports/tracker.json", AI_GM),
    "pool": ("exports/pools/auction-pool-2026-27.json", POOL),
}
NHL_SCORE = "https://api-web.nhle.com/v1/score/{day}"
REGULAR = 2
MAX_AGE = timedelta(hours=26)
NIGHT_DUE = time(22)            # UTC, the day after the game night
WEEK_DUE = timedelta(hours=25)  # after Monday 00:00 UTC
ARTICLE_WINDOW = timedelta(days=2)
LOCAL = ZoneInfo("America/Edmonton")
HEADERS = {"User-Agent": "Mozilla/5.0 (gmbench health check; +https://www.gamedaysuits.ca)"}


@dataclass(frozen=True)
class Problem:
    page: str
    text: str


# --------------------------------------------------------------------------- pure checks


def _day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d:%b} {d.day}"


def _when(ts: datetime) -> str:
    t = ts.astimezone(LOCAL)
    return f"{t:%a} {t:%b} {t.day}, {t:%-I:%M %p} Alberta time"


def due_night(nights: Mapping[date, int], now: datetime) -> date | None:
    """The latest NHL night with regular-season games whose scoring is due by `now`."""
    due = [d for d, n in nights.items()
           if n > 0 and now >= datetime.combine(d + timedelta(days=1), NIGHT_DUE, tzinfo=UTC)]
    return max(due, default=None)


def due_monday(now: datetime) -> date:
    """The latest Monday whose front office is due by `now`."""
    d = (now - WEEK_DUE).date()
    return d - timedelta(days=d.weekday())


def check(
    *,
    pages: Mapping[str, str | None],
    feeds: Mapping[str, dict[str, Any] | None],
    nights: Mapping[date, int] | None,
    now: datetime,
) -> list[Problem]:
    """Every problem a visitor would see. Pure: callers fetch; `nights` None means the NHL was unreachable."""
    out: list[Problem] = []
    for name, (url, src) in PAGES.items():
        html = pages.get(name)
        if html is None:
            out.append(Problem(name, f"The page doesn't load ({url})."))
        elif src not in html:
            out.append(Problem(name, f"The page no longer points at its live data ({src}), so it can't update. "
                                     "It may have been edited in Shopify."))
    for key, (path, page) in FEEDS.items():
        if feeds.get(key) is None:
            out.append(Problem(page, f"Its data feed doesn't load ({RAW}{path})."))

    tracker, pool = feeds.get("tracker"), feeds.get("pool")
    for data, page in ((tracker, AI_GM), (pool, POOL)):
        if data:
            built = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
            if now - built > MAX_AGE:
                hours = int((now - built).total_seconds() // 3600)
                out.append(Problem(page, f"Its numbers haven't been rebuilt in {hours} hours (last: {_when(built)}). "
                                         "The daily update failed or didn't run."))

    expected = due_night(nights, now) if nights is not None else None
    league_last = tracker["last_game_date"] if tracker else None
    if tracker and expected and league_last < expected.isoformat():
        out.append(Problem(AI_GM, f"It's missing game nights: scored through {_day(league_last)}, "
                                  f"but the NHL has played through {_day(expected.isoformat())}."))
    if pool:
        target = max(filter(None, [expected.isoformat() if expected else None, league_last]), default=None)
        if target and pool["last_game_date"] < target:
            out.append(Problem(POOL, f"It's missing game nights: scored through {_day(pool['last_game_date'])}, "
                                     f"but games through {_day(target)} are in."))
        if pool.get("unresolved"):
            names = ", ".join(u.get("name", "?") for u in pool["unresolved"])
            out.append(Problem(POOL, f"These drafted players don't match an NHL player, so they score 0: {names}."))

    weeks = (tracker or {}).get("front_office") or []
    if weeks:
        monday = due_monday(now)
        season_end = (tracker.get("season") or {}).get("end", "9999-12-31")
        first = min(w["week"] for w in weeks)
        latest = max(weeks, key=lambda w: w["week"])
        if first <= monday.isoformat() <= season_end:
            if latest["week"] < monday.isoformat():
                out.append(Problem(AI_GM, f"The weekly front office (lineups, waivers, trades, chatter) didn't run for "
                                          f"the week of {_day(monday.isoformat())}. The latest is {_day(latest['week'])}."))
            elif not latest.get("article"):
                due = datetime.combine(monday, time(), tzinfo=UTC) + WEEK_DUE
                if now < due + ARTICLE_WINDOW:
                    out.append(Problem(AI_GM, f"The week {latest.get('number', '?')} article was written but never "
                                              "posted to the blog. The weekly job needs the Shopify keys in the "
                                              "repository's GitHub secrets."))
    return out


# --------------------------------------------------------------------------- fetching


def _get(client: httpx.Client, url: str, tries: int = 3) -> httpx.Response | None:
    for attempt in range(tries):
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp
            if resp.status_code < 500 and resp.status_code != 429:
                return None
        except httpx.HTTPError:
            pass
        if attempt < tries - 1:
            _time.sleep(5 * (attempt + 1))
    return None


def gather(now: datetime, client: httpx.Client | None = None) -> dict[str, Any]:
    """Fetch the pages, the feeds (cache-busted, exactly as the pages fetch them) and the last 8 NHL nights."""
    http = client or httpx.Client(timeout=30.0, follow_redirects=True, headers=HEADERS)
    bust = int(now.timestamp())
    pages = {name: (r.text if (r := _get(http, url)) else None) for name, (url, _) in PAGES.items()}
    feeds: dict[str, dict[str, Any] | None] = {}
    for key, (path, _) in FEEDS.items():
        r = _get(http, f"{RAW}{path}?v={bust}")
        try:
            feeds[key] = r.json() if r else None
        except ValueError:
            feeds[key] = None
    nights: dict[date, int] | None = {}
    for back in range(1, 9):
        day = now.date() - timedelta(days=back)
        r = _get(http, NHL_SCORE.format(day=day.isoformat()))
        if r is None:
            nights = None  # NHL unreachable: skip the game-night check rather than raise a false alarm
            break
        nights[day] = sum(1 for g in r.json().get("games", []) if g.get("gameType") == REGULAR)
    return {"pages": pages, "feeds": feeds, "nights": nights}


# --------------------------------------------------------------------------- reporting


def render(problems: list[Problem], *, run_url: str | None = None, test: bool = False) -> tuple[str, str]:
    """(subject, plain-text body) for the alert email."""
    if test and not problems:
        subject = "GDS hockey alert: test (all clear)"
        lines = ["This is a test of the hockey-page alert email. Both pages passed every check right now.", ""]
    else:
        n = len(problems)
        subject = ("GDS hockey alert: test, " if test else "GDS hockey alert: ") + \
            f"{n} problem{'s' if n != 1 else ''} on the tracker pages"
        lines = [f"The health check found {n} problem{'s' if n != 1 else ''} on the hockey pages.", ""]
        for page in dict.fromkeys(p.page for p in problems):
            url = PAGES[page][0] if page in PAGES else ""
            lines.append(f"{page}" + (f": {url}" if url else ""))
            lines += [f"  - {p.text}" for p in problems if p.page == page]
            lines.append("")
    if run_url:
        lines.append(f"Run log: {run_url}")
    lines.append("This check runs every evening. Until a problem is fixed, you'll get this email once a day.")
    return subject, "\n".join(lines) + "\n"


def send_email(subject: str, body: str, env: Mapping[str, str] = os.environ,
               smtp: Callable[..., Any] = smtplib.SMTP_SSL) -> bool:
    """Send via Gmail SMTP. False (nothing sent) when GMAIL_ADDRESS / GMAIL_APP_PASSWORD aren't set."""
    sender, password = env.get("GMAIL_ADDRESS", "").strip(), env.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not sender or not password:
        return False
    msg = EmailMessage()
    msg["From"] = f"GDS hockey pages <{sender}>"
    msg["To"] = env.get("ALERT_TO", "").strip() or sender
    msg["Subject"] = subject
    msg.set_content(body)
    with smtp("smtp.gmail.com", 465, timeout=30) as s:
        s.login(sender, password)
        s.send_message(msg)
    return True


def main(*, test_email: bool = False, now: datetime | None = None, env: Mapping[str, str] = os.environ) -> int:
    """Run every check, report, alert. Exit status 1 when anything is wrong (or a test email can't be sent)."""
    now = now or datetime.now(UTC)
    try:
        problems = check(now=now, **gather(now))
    except Exception as exc:  # the check itself breaking is also worth an email
        problems = [Problem(CHECK, f"The health check itself crashed: {exc!r}")]
    server, repo, run_id = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_ID")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None

    for p in problems:
        print(f"::error title={p.page}::{p.text}")
    print(f"{len(problems)} problem(s)" if problems else "All checks passed.")
    if summary := env.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write("## Hockey pages health check\n\n")
            f.write("\n".join(f"- **{p.page}**: {p.text}" for p in problems) + "\n" if problems else "All checks passed.\n")

    if problems or test_email:
        subject, body = render(problems, run_url=run_url, test=test_email)
        try:
            sent = send_email(subject, body, env)
        except Exception as exc:
            print(f"::error title=Alert email::Couldn't send the alert email: {exc!r}")
            return 1
        print("alert email sent" if sent else
              "::warning title=Alert email::GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set; no email sent "
              "(GitHub's failed-run email still goes out)")
        if test_email and not sent:
            return 1
    return 1 if problems else 0
