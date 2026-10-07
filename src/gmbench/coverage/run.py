"""Build (and optionally publish) a front-office week's coverage.

runs/<run>/coverage/<week>/
  judge.json      the panel's verdict on every line (cached; never re-asked)
  article.json    what was built: title, handle, featured line ids, image alts
  published.json  where it went: the article id/URL and each image's CDN URL (committed, so the tracker can link it)
  img/*.png       the images (regenerated on demand, not committed)
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from gmbench.config import ROOT, LeagueConfig
from gmbench.coverage import article as art
from gmbench.coverage import curate, images
from gmbench.coverage.facts import WeekFacts, week_facts, weeks_with_front_office

TRACKER_HANDLE = "ai-gm-draft-tracker"
TRACKER_SEO_TITLE = "AI GM League Tracker: 13 AI Models Play Fantasy Hockey"
TRACKER_SEO_DESC = ("Live standings, rosters, trades and trash talk from the AI GM League: 13 frontier AI models and a "
                    "control bot run NHL fantasy teams all season, scored daily from official NHL stats.")


def coverage_dir(run_dir: Path, week: date) -> Path:
    return run_dir / "coverage" / week.isoformat()


def build_week(events: list[dict[str, Any]], cfg: LeagueConfig, run_dir: Path, week: date, *, log=print,
               judge: bool = True) -> tuple[WeekFacts, art.Article, Path]:
    out = coverage_dir(run_dir, week)
    f = week_facts(events, cfg, week_start=week, root=ROOT, run_dir=run_dir)
    if not f.locked:
        raise SystemExit(f"week {week} has no locked front office in the ledger yet")
    verdicts = curate.judge(f, out / "judge.json", log=log) if judge else (
        json.loads((out / "judge.json").read_text()) if (out / "judge.json").exists() else {})
    best = curate.featured(f, verdicts)
    ok_ids = {k for k, v in verdicts.items() if v.get("ok")}
    earlier = [w for w in weeks_with_front_office(events) if w < week]
    first_trade_ever = bool(f.executed) and not any(
        e["type"] == "TRADE_EXECUTED" and e["payload"]["week"] < week.isoformat() for e in events)
    prev = earlier[-1] if earlier else None
    prev_handle = None
    if prev and (coverage_dir(run_dir, prev) / "published.json").exists():
        prev_handle = json.loads((coverage_dir(run_dir, prev) / "published.json").read_text()).get("handle")
    a = art.build(f, best, ok_ids, prev_handle=prev_handle, first_trade_ever=first_trade_ever)

    img = out / "img"
    a.images["cover"]["path"] = str(images.cover(f, art.headline(f, first_trade_ever), img / f"week-{f.number}-cover.png"))
    a.images["standings"]["path"] = str(images.standings_chart(f, img / f"week-{f.number}-standings.png"))
    for i, (ln, _) in enumerate(best[:3]):
        a.images[f"quote{i + 1}"]["path"] = str(images.quote_card(f, ln, img / f"week-{f.number}-quote-{i + 1}.png"))
    meta = {"week": week.isoformat(), "number": f.number, "handle": a.handle, "title": a.title, "seo_title": a.seo_title,
            "seo_description": a.seo_description, "featured": [ln.id for ln, _ in best],
            "images": {k: {"alt": v["alt"], "file": Path(v["path"]).name} for k, v in a.images.items() if "path" in v}}
    (out / "article.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    log(f"coverage week {f.number} ({week}): {a.title} | {len(f.lines)} lines, {len(ok_ids)} pass, {len(best)} featured")
    return f, a, out


def publish_week(a: art.Article, out: Path, *, log=print) -> dict[str, Any]:
    from gmbench.coverage.shopify import Shopify

    shop = Shopify()
    pub_path = out / "published.json"
    pub = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    urls: dict[str, str] = pub.get("images", {})  # "<name>:<sha>" -> CDN URL, so an unchanged image never re-uploads
    shas = {name: _sha(Path(im["path"])) for name, im in a.images.items() if "path" in im}
    for name, sha in shas.items():
        if f"{name}:{sha}" not in urls:
            urls[f"{name}:{sha}"] = shop.upload_image(Path(a.images[name]["path"]), a.images[name]["alt"])
            log(f"  uploaded {Path(a.images[name]['path']).name}")
    by_name = {name: urls[f"{name}:{sha}"] for name, sha in shas.items()}
    body = re.sub(r"\{\{img:(\w+)\}\}", lambda m: by_name.get(m.group(1), ""), a.body)
    body = re.sub(r'<p><img src="" [^>]*></p>', "", body)
    blog = shop.blog_id("news")
    made = shop.upsert_article(blog_id=blog, handle=a.handle, title=a.title, body=body, summary=a.summary, tags=a.tags,
                               image_url=by_name.get("cover"), image_alt=a.images["cover"]["alt"],
                               seo_title=a.seo_title, seo_description=a.seo_description)
    pub = {"handle": made["handle"], "article_id": made["id"], "url": f"{art.SITE}/blogs/news/{made['handle']}",
           "title": a.title, "published_at": pub.get("published_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "images": urls,
           "cover_url": by_name.get("cover")}
    pub_path.write_text(json.dumps(pub, indent=1))
    log(f"  published {pub['url']}")
    return pub


def _sha(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def publish_tracker_page(run_dir: Path, *, log=print) -> None:
    """Push the repo's tracker page to Shopify, with a server-rendered standings snapshot inside it for crawlers and
    answer engines (the page's script replaces it with live data on load)."""
    from gmbench.coverage.shopify import Shopify

    body = tracker_page_body(run_dir)
    Shopify().update_page(TRACKER_HANDLE, body, seo_title=TRACKER_SEO_TITLE, seo_description=TRACKER_SEO_DESC)
    log(f"  tracker page refreshed ({len(body)} chars)")


def tracker_page_body(run_dir: Path) -> str:
    import html

    page = (ROOT / "site" / "shopify" / "draft-tracker.html").read_text()
    try:
        d = json.loads((run_dir / "exports" / "tracker.json").read_text())
    except (OSError, ValueError):
        return page
    teams = sorted(d.get("teams", []), key=lambda t: (-int(t.get("points") or 0), t.get("display") or ""))
    pts = [int(t.get("points") or 0) for t in teams]

    def name(t: dict[str, Any]) -> str:
        return "Autodraft (control bot)" if t.get("bot") else f"{t.get('display') or t['id']} ({t.get('lab') or ''})"
    rows = "".join(
        f"<tr><td>{1 + sum(p > int(t.get('points') or 0) for p in pts)}</td><td>{html.escape(name(t))}</td>"
        f"<td>{html.escape(t.get('franchise') or '—')}</td><td class=\"gdt-num\">{int(t.get('points') or 0)}</td></tr>"
        for t in teams)
    as_of = d.get("last_game_date") or ""
    static = (f'<table class="gdt-table"><caption class="gdt-note">Standings through {html.escape(as_of)} '
              f'(loading live data…)</caption><thead><tr><th>Rank</th><th>Model</th><th>Team</th>'
              f'<th class="gdt-num">Points</th></tr></thead><tbody>{rows}</tbody></table>')
    return page.replace('<div data-slot="standings"><p class="gdt-empty">Loading the latest standings…</p></div>',
                        f'<div data-slot="standings">{static}</div>')


def front_office_feed(events: list[dict[str, Any]], cfg: LeagueConfig, run_dir: Path) -> list[dict[str, Any]]:
    """Every front-office week for tracker.json, newest first: trades with their talk, waiver wins, the featured lines
    (judged) and the rest of the chat, plus the week's article when it's out."""
    feed = []
    for wk in reversed(weeks_with_front_office(events)):
        f = week_facts(events, cfg, week_start=wk, root=ROOT, run_dir=run_dir)
        out = coverage_dir(run_dir, wk)
        verdicts = json.loads((out / "judge.json").read_text()) if (out / "judge.json").exists() else {}
        best = {ln.id for ln, _ in curate.featured(f, verdicts)}
        pub = json.loads((out / "published.json").read_text()) if (out / "published.json").exists() else {}
        line = lambda ln: {"id": ln.id, "team": ln.team, "kind": ln.kind, "text": ln.text,  # noqa: E731
                           "featured": ln.id in best, "ok": bool(verdicts.get(ln.id, {}).get("ok"))}
        feed.append({
            "week": wk.isoformat(), "number": f.number, "trades_open": f.trades_open,
            "article": {"url": pub["url"], "title": pub.get("title"), "image": pub.get("cover_url")} if pub.get("url") else None,
            "trades": [{"id": t.trade_id, "from": t.proposer, "to": t.recipient, "give": t.give, "get": t.get,
                        "status": t.status, "counter_of": t.counter_of, "reason": t.void_reason,
                        "talk": [line(ln) for ln in f.lines if ln.kind in ("pitch", "reply") and
                                 (ln.id == f"pitch-{t.trade_id}" or ln.id.startswith(f"reply-{t.trade_id}-"))
                                 and verdicts.get(ln.id, {}).get("ok")]}
                       for t in f.trades],
            "waivers": [{"team": w["team"], "add": w["add"], "drop": w["drop"]} for w in f.waivers],
            "lines": sorted((line(ln) for ln in f.lines if ln.kind in ("chat", "presser")),
                            key=lambda x: (not x["featured"], x["kind"] != "chat", x["id"])),
            "highlights": [line(ln) for ln, _ in curate.featured(f, verdicts)],
        })
    return feed


def feed_player_ids(feed: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for wk in feed:
        for t in wk["trades"]:
            ids |= set(t["give"]) | set(t["get"])
        for w in wk["waivers"]:
            ids |= {w["add"], w["drop"]}
    return ids

