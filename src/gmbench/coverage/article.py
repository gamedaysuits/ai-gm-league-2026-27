"""The weekly article: deterministic copy from the ledger, the GMs' best (judged) lines, and SEO/AEO metadata.

Built for search and answer engines: a question-shaped title, a one-paragraph answer up top, key takeaways, a real
HTML standings table, an FAQ that mirrors how people ask ("which AI is winning…"), Article + FAQPage JSON-LD,
descriptive alt text, a stable handle per week, and links to the live tracker, last week's report and the ledger.
Images are referenced as ``{{img:<name>}}`` and swapped for their Shopify CDN URLs at publish time.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Any

from gmbench.coverage.facts import Line, TradeTalk, WeekFacts

SITE = "https://www.gamedaysuits.ca"
TRACKER = "/pages/ai-gm-draft-tracker"
LAUNCH_POST = "/blogs/news/ai-fantasy-draft-2026-27"
REPO = "https://github.com/gamedaysuits/ai-gm-league-2026-27"
TAG = "AI GM League"


def handle_for(number: int) -> str:
    return f"ai-gm-league-week-{number}-2026-27"


@dataclass
class Article:
    handle: str
    title: str
    seo_title: str
    seo_description: str
    summary: str
    body: str
    tags: list[str]
    images: dict[str, dict[str, str]] = field(default_factory=dict)  # name -> {path, alt}
    cover: str = "cover"


def e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def headline(f: WeekFacts, first_trade_ever: bool) -> str:
    lead = f.teams[f.standings[0]["team"]]
    done = f.executed
    if len(done) == 1:
        a, b = f.teams[done[0].proposer], f.teams[done[0].recipient]
        return f"{a.call} and {b.call} Make the League's First Trade" if first_trade_ever else f"{a.call} and {b.call} Make a Deal"
    if len(done) > 1:
        return f"{len(done)} Trades Land as {lead.call} Leads"
    offers = [t for t in f.trades if not t.counter_of]
    if f.trades_open and offers:
        return f"{_plural(len(offers), 'Trade Offer')}, Zero Deals"
    if f.standings[0].get("prev_rank") not in (None, 1):
        return f"{lead.call} Takes Over First Place"
    return f"{lead.call} Leads, the Waiver Wire Heats Up" if f.waivers else f"{lead.call} Stays on Top"


def _movement(s: dict[str, Any]) -> str:
    if s.get("prev_rank") is None:
        return "–"
    d = s["prev_rank"] - s["rank"]
    return f"▲{d}" if d > 0 else f"▼{-d}" if d < 0 else "–"


def _credit(f: WeekFacts, ln: Line) -> str:
    t = f.teams[ln.team]
    where = {"chat": "in the league chat", "presser": "at the weekly presser", "pitch": "in a trade pitch",
             "reply": "answering a trade offer"}[ln.kind]
    who = f"<strong>{e(t.display)}</strong> ({e(t.lab)})" + (f", GM of the {e(t.franchise)}" if t.franchise else "")
    return f"— {who}, {where}"


def _quote(f: WeekFacts, ln: Line) -> str:
    return f'<blockquote><p>{e(ln.text)}</p><p><cite>{_credit(f, ln)}</cite></p></blockquote>'


def _vs_bot(n: int) -> str:
    return ("All 13 AI models are ahead of" if n == 13 else "None of the 13 AI models is ahead of" if n == 0
            else f"{n} of the 13 AI models {'is' if n == 1 else 'are'} ahead of")


def _deal(f: WeekFacts, t: TradeTalk) -> str:
    a, b = f.teams[t.proposer], f.teams[t.recipient]
    return (f"<strong>{e(a.display)}</strong> sends {e(_list([f.label(p) for p in t.give]))} to "
            f"<strong>{e(b.display)}</strong> for {e(_list([f.label(p) for p in t.get]))}")


def build(f: WeekFacts, featured: list[tuple[Line, float]], ok_ids: set[str], *, prev_handle: str | None,
          first_trade_ever: bool, published_iso: str) -> Article:
    n, prev_n = f.number, f.number - 1
    st = f.standings
    lead, second = st[0], st[1]
    L, S = f.teams[lead["team"]], f.teams[second["team"]]
    bot_row = next((s for s in st if f.teams[s["team"]].bot), None)
    ahead_of_bot = sum(1 for s in st if bot_row and not f.teams[s["team"]].bot and s["points"] > bot_row["points"])
    best_week = max(st, key=lambda s: (s["week_points"], -s["rank"]))
    B = f.teams[best_week["team"]]
    span = f"{f.week_start:%B %-d}–{f.week_end:%-d, %Y}" if f.week_start.month == f.week_end.month \
        else f"{f.week_start:%B %-d} – {f.week_end:%B %-d, %Y}"
    gap = lead["points"] - second["points"]
    head = headline(f, first_trade_ever)
    title = f"AI GM League Week {n}: {head}"
    seo_title = f"AI Fantasy Hockey Week {n}: {L.call} Leads | AI GM League"
    answer = (f"After week {prev_n}, <strong>{e(L.display)}</strong> ({e(L.lab)}) leads the AI GM League with "
              f"{lead['points']} fantasy points, "
              + (f"{_plural(gap, 'point')} ahead of {e(S.display)}." if gap else f"tied with {e(S.display)}."))
    if bot_row:
        answer += (f" {_vs_bot(ahead_of_bot)} the control bot, Autodraft, which sits {_ordinal(bot_row['rank'])} "
                   f"with {bot_row['points']} points.")

    trade_line = _trade_summary(f)
    has_prev = lead.get("prev_rank") is not None
    takeaways = [f"<strong>Leader:</strong> {e(L.display)} ({e(L.lab)}), {lead['points']} points."]
    if has_prev:
        takeaways.append(f"<strong>Best week:</strong> {e(B.display)} scored {best_week['week_points']} points in week {prev_n}.")
    takeaways += [
        f"<strong>Trades:</strong> {trade_line}",
        f"<strong>Waivers:</strong> {_plural(len(f.waivers), 'claim')} won"
        + (f" out of {f.waiver_claims} submitted." if f.waiver_claims else "."),
    ]
    if f.top_players:
        p = f.top_players[0]
        takeaways.append(f"<strong>Top performer:</strong> {e(f.label(p['pid']))} put up {p['pts']} points for "
                         f"{e(f.teams[p['team']].display)}.")

    parts: list[str] = [
        f"<p>{answer}</p>",
        f"<p>Every Monday the 13 AI models in our <a href=\"{LAUNCH_POST}\">AI fantasy hockey league</a> set their "
        f"lineups, claim free agents, make trade offers and talk trash. Here's week {n} ({e(span)}): the standings, "
        f"every move, and the best of what they said. Live scores are on the <a href=\"{TRACKER}\">AI GM League "
        f"tracker</a>.</p>",
        "<h2>Key takeaways</h2>",
        "<ul>" + "".join(f"<li>{t}</li>" for t in takeaways) + "</ul>",
        f"<h2>AI GM League standings after week {prev_n}</h2>",
        f'<p><img src="{{{{img:standings}}}}" alt="Bar chart of AI GM League standings after week {prev_n}: '
        f'{e(L.display)} leads with {lead["points"]} points" width="1200" loading="lazy"></p>',
        _standings_table(f),
    ]

    parts.append(f"<h2>Trades in week {n}</h2>")
    parts += _trade_section(f, ok_ids)

    parts.append(f"<h2>Waiver wire</h2>")
    if f.waivers:
        parts.append("<ul>" + "".join(
            f"<li><strong>{e(f.teams[w['team']].display)}</strong> added {e(f.label(w['add']))} and dropped "
            f"{e(f.label(w['drop']))}.</li>" for w in f.waivers) + "</ul>")
    else:
        parts.append("<p>No waiver claims went through this week.</p>")

    if featured:
        parts.append("<h2>Best trash talk of the week</h2>")
        parts.append("<p>Every word is the model's own. A panel of AI judges that don't play in the league picked these: "
                     "they had to make sense, stay friendly and get their facts right.</p>")
        for i, (ln, _) in enumerate(featured):
            if i < 3:  # the card carries the quote; the caption carries the credit (and the alt text the words)
                t = f.teams[ln.team]
                parts.append(f'<figure><img src="{{{{img:quote{i + 1}}}}}" alt="{e(t.display)} ({e(t.lab)}): '
                             f'&quot;{e(ln.text)}&quot;" width="1200" loading="lazy"><figcaption>{_credit(f, ln)}'
                             f'</figcaption></figure>')
            else:
                parts.append(_quote(f, ln))

    if f.top_players:
        parts.append(f"<h2>Top performers in week {prev_n}</h2>")
        parts.append("<ol>" + "".join(
            f"<li>{e(f.label(r['pid']))}: {r['pts']} points for {e(f.teams[r['team']].display)}"
            f"{_statline(r)}</li>" for r in f.top_players) + "</ol>")

    faq = _faq(f, L, lead, S, gap, bot_row, ahead_of_bot, trade_line, n, prev_n)
    parts.append("<h2>FAQ</h2>")
    for q, a in faq:
        parts.append(f"<h3>{e(q)}</h3><p>{a}</p>")

    more = [f'<a href="{TRACKER}">live standings and rosters</a>']
    if prev_handle:
        more.append(f'<a href="/blogs/news/{prev_handle}">the week {n - 1} report</a>')
    more.append(f'<a href="{REPO}">the public ledger</a>')
    parts.append(f"<p>More: {_list(more)}. Numbers come from the NHL's official stats, scored by code and recorded in "
                 f"a tamper-evident ledger.</p>")

    ld = [{
        "@context": "https://schema.org", "@type": "Article", "headline": title,
        "description": _strip(answer), "datePublished": published_iso, "dateModified": published_iso,
        "author": {"@type": "Organization", "name": "Game Day Suits", "url": SITE},
        "publisher": {"@type": "Organization", "name": "Game Day Suits", "url": SITE},
        "mainEntityOfPage": f"{SITE}/blogs/news/{handle_for(n)}",
        "about": ["Fantasy hockey", "Artificial intelligence", "NHL"],
        "image": "{{img:cover}}",
    }, {
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": _strip(a)}}
                       for q, a in faq],
    }]
    parts.append('<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>")

    desc = _strip(answer)
    seo_desc = (f"Week {n} of the AI GM League: {L.call} leads with {lead['points']} points. "
                f"{_strip(trade_line)} Standings, waivers and the best AI trash talk.")
    tags = [TAG, "AI Fantasy Hockey", "Fantasy Hockey", "NHL", "AI", f"Week {n}"] + sorted(
        {f.teams[ln.team].display for ln, _ in featured} | {L.display})
    images = {"cover": {"alt": f"AI GM League week {n}: {head}. Top three: " + ", ".join(
                  f"{f.teams[s['team']].display} {s['points']} points" for s in st[:3])},
              "standings": {"alt": f"AI GM League standings after week {prev_n}"}}
    for i, (ln, _) in enumerate(featured[:3]):
        images[f"quote{i + 1}"] = {"alt": f"{f.teams[ln.team].display}: {ln.text}"}
    return Article(handle=handle_for(n), title=title, seo_title=seo_title[:70], seo_description=_clip(seo_desc, 158),
                   summary=f"<p>{desc}</p>", body="\n".join(parts), tags=tags, images=images)


def _trade_summary(f: WeekFacts) -> str:
    done = f.executed
    offers = [t for t in f.trades if not t.counter_of]
    if not f.trades_open:
        return "Trading opens October 12." if f.week_start.isoformat() < "2026-10-12" else "The trade deadline has passed."
    if done:
        return f"{_plural(len(done), 'trade')} completed out of {_plural(len(f.trades), 'offer')} (counters included)."
    if offers:
        return f"No deals: {_plural(len(f.trades), 'offer')} made, none accepted."
    return "Nobody made an offer."


def _trade_section(f: WeekFacts, ok_ids: set[str]) -> list[str]:
    out: list[str] = []
    if not f.trades_open:
        out.append(f"<p>{_trade_summary(f)} Until then it's lineups and waivers only.</p>")
        return out
    done = f.executed
    if done:
        for t in done:
            out.append(f"<h3>{e(f.teams[t.proposer].call)} ↔ {e(f.teams[t.recipient].call)}</h3>")
            out.append(f"<p>{_deal(f, t)}.</p>")
            out += _talk(f, t, ok_ids)
    else:
        out.append(f"<p>{_trade_summary(f)}</p>")
    misses = [t for t in f.trades if t.status != "executed"]
    if misses:
        out.append("<h3>Offers that didn't land</h3><ul>")
        for t in misses:
            why = {"rejected": "turned down", "voided": "voided (rosters no longer legal)", "countered": "countered",
                   "open": "never answered", "accepted": "accepted, then voided"}.get(t.status, t.status)
            out.append(f"<li>{_deal(f, t)}: {why}.</li>")
        out.append("</ul>")
        for t in misses:
            out += _talk(f, t, ok_ids, only_best=True)
    return out


def _talk(f: WeekFacts, t: TradeTalk, ok_ids: set[str], only_best: bool = False) -> list[str]:
    out = []
    for ln in f.lines:
        if ln.kind in ("pitch", "reply") and ln.id in ok_ids and (ln.id == f"pitch-{t.trade_id}" or ln.id.startswith(f"reply-{t.trade_id}-")):
            out.append(_quote(f, ln))
    return out[:1] if only_best else out


def _standings_table(f: WeekFacts) -> str:
    weekly = any(s.get("prev_rank") is not None for s in f.standings)  # the first week has nothing to compare to
    rows = []
    for s in f.standings:
        t = f.teams[s["team"]]
        name = e(t.credit)
        name = f"<strong>{name}</strong>" if s["rank"] == 1 else name
        team = e(t.franchise) if t.franchise else "—"
        extra = (f"<td style=\"text-align:right\">{s['week_points']}</td><td style=\"text-align:center\">{_movement(s)}</td>"
                 if weekly else "")
        rows.append(f"<tr><td style=\"text-align:right\">{s['rank']}</td><td>{name}</td><td>{team}</td>"
                    f"<td style=\"text-align:right\">{s['points']}</td>{extra}</tr>")
    head = ('<th style="text-align:right">Last week</th><th style="text-align:center">Move</th>' if weekly else "")
    return ('<table style="width:100%;border-collapse:collapse"><thead><tr><th style="text-align:right">Rank</th>'
            '<th style="text-align:left">Model</th><th style="text-align:left">Team</th><th style="text-align:right">Points</th>'
            + head + '</tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def _statline(r: dict[str, Any]) -> str:
    bits = []
    if r["goals"] or r["assists"]:
        bits.append(f"{_plural(r['goals'], 'goal')}, {_plural(r['assists'], 'assist')}")
    if r["wins"]:
        bits.append(_plural(r["wins"], "win"))
    if r["shutouts"]:
        bits.append(_plural(r["shutouts"], "shutout"))
    return f" ({'; '.join(bits)} in {_plural(r['gp'], 'game')})" if bits else ""


def _faq(f, L, lead, S, gap, bot_row, ahead_of_bot, trade_line, n, prev_n) -> list[tuple[str, str]]:
    faq = [("Which AI model is winning the AI GM League?",
            f"{e(L.display)} by {e(L.lab)}, with {lead['points']} fantasy points after week {prev_n}"
            + (f", {_plural(gap, 'point')} ahead of {e(S.display)}." if gap else f", tied with {e(S.display)}."))]
    if bot_row:
        faq.append(("Are the AI models beating the control bot?",
                    f"{_vs_bot(ahead_of_bot)} Autodraft, the rules-based control bot, which has {bot_row['points']} points. The robot "
                    f"doesn't trade or talk; it's the baseline every model has to beat."))
    faq.append((f"What trades did the AI models make in week {n}?",
                _strip(trade_line) + (" " + "; ".join(_strip(_deal(f, t)) for t in f.executed) + "." if f.executed else "")))
    faq.append(("How does the AI GM League work?",
                "Thirteen frontier AI models (from OpenAI, Anthropic, Google, SpaceXAI, Moonshot, Xiaomi, Alibaba, DeepSeek, "
                "Meta, Z.ai and Sakana) and one control bot each drafted a 14-player NHL fantasy team. Skaters score a point "
                "per goal and assist; goalies score for wins, overtime losses and shutouts. Every Monday each model sets its "
                "lineup, claims free agents and trades with the others, using the same tools and data. Scores come from the "
                "NHL's official stats."))
    return faq


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _strip(s: str) -> str:
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"
