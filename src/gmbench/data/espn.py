"""ESPN sources (unofficial site API): injuries and news headlines.

ESPN athlete IDs are not NHL IDs (and are null in the injuries feed), so players
are matched to NHL IDs by normalized name + club, with the ESPN position group
splitting namesakes (the two Vancouver Elias Petterssons). News keeps headline
and date only, never article bodies.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterable
from typing import Any

from gmbench.data.http import get_json
from gmbench.data.models import Group, Injury, NewsItem, Player
from gmbench.data.nhl import TEAM_NAMES

log = logging.getLogger(__name__)

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news?limit={limit}"

ESPN_ABBREV = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA", "UTA": "UTA"}
ESPN_TEAM_IDS = {
    "1": "BOS", "2": "BUF", "3": "CGY", "4": "CHI", "5": "DET", "6": "EDM", "7": "CAR", "8": "LAK",
    "9": "DAL", "10": "MTL", "11": "NJD", "12": "NYI", "13": "NYR", "14": "OTT", "15": "PHI", "16": "PIT",
    "17": "COL", "18": "SJS", "19": "STL", "20": "TBL", "21": "TOR", "22": "VAN", "23": "WSH", "25": "ANA",
    "26": "FLA", "27": "NSH", "28": "WPG", "29": "CBJ", "30": "MIN", "37": "VGK", "124292": "SEA", "129764": "UTA",
}  # fmt: skip
ESPN_POSITION_GROUP: dict[str, Group] = {"C": "F", "LW": "F", "RW": "F", "F": "F", "W": "F", "D": "D", "G": "G"}

_TRANSLIT = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ł": "l", "Ł": "L",
     "đ": "d", "Đ": "D", "ð": "d", "þ": "th", "ı": "i"}
)  # fmt: skip
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_NICKNAMES = [
    {"alex", "alexander", "alexandre", "aleksander", "aleksandr", "alexandr", "sasha"},
    {"anthony", "tony"}, {"robert", "rob", "robbie", "bob", "bobby"}, {"william", "will", "bill", "billy"},
    {"john", "jack", "johnny", "jon", "jonathan", "jonny"}, {"andrew", "drew", "andy"}, {"michael", "mike", "mikey"},
    {"james", "jim", "jimmy", "jamie"}, {"joseph", "joe", "joey"}, {"thomas", "tom", "tommy"},
    {"nathan", "nate", "nathaniel"}, {"jacob", "jake", "jakob"}, {"daniel", "dan", "danny"},
    {"matthew", "matt", "matty", "mathew"}, {"nicholas", "nick", "nicolas", "nikolas", "nico"},
    {"zachary", "zach", "zack", "zac"}, {"frederik", "frederick", "fred", "freddie", "freddy"},
    {"yegor", "egor"}, {"evgeny", "evgeni", "evgenii", "yevgeni", "yevgeny"}, {"dmitri", "dmitry", "dmitrij"},
    {"nikolai", "nikolaj", "nicolai", "nikolay"}, {"maxim", "maxime", "max", "maksim"},
]  # fmt: skip
_NICK = {name: i for i, group in enumerate(_NICKNAMES) for name in group}


def normalize_name(name: str | None) -> str:
    """ASCII-fold, lowercase, drop punctuation and suffixes: "J.T. Miller Jr." -> "jt miller"."""
    s = unicodedata.normalize("NFKD", (name or "").translate(_TRANSLIT))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[-_/]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(t for t in s.split() if t not in _SUFFIXES)


_TEAM_BY_NAME = {normalize_name(name): abbrev for abbrev, name in TEAM_NAMES.items()} | {"utah hockey club": "UTA"}


def nhl_team(name: str | None = None, abbreviation: str | None = None, espn_id: Any = None) -> str | None:
    """Map an ESPN team (display name first, then abbreviation, then ESPN id) to the NHL abbreviation."""
    if name and (hit := _TEAM_BY_NAME.get(normalize_name(name))):
        return hit
    if abbreviation:
        ab = abbreviation.upper()
        if ab in TEAM_NAMES:
            return ab
        if ab in ESPN_ABBREV:
            return ESPN_ABBREV[ab]
    if espn_id is not None:
        return ESPN_TEAM_IDS.get(str(espn_id))
    return None


def _compatible_first(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return a in _NICK and _NICK.get(a) == _NICK.get(b)


class PlayerIndex:
    """Name lookups over snapshot players; every hit is an NHL player ID."""

    def __init__(self, players: Iterable[Player]):
        self.by_full: dict[str, list[Player]] = defaultdict(list)
        self.by_last: dict[str, list[Player]] = defaultdict(list)
        for p in sorted(players, key=lambda p: p.id):
            self.by_full[normalize_name(f"{p.first_name} {p.last_name}")].append(p)
            self.by_last[normalize_name(p.last_name)].append(p)

    def match(
        self,
        name: str,
        *,
        first: str | None = None,
        last: str | None = None,
        teams: Collection[str] = (),
        group: str | None = None,
    ) -> int | None:
        """NHL ID for a third-party name, or None when absent or ambiguous.

        1. exact normalized full name (prefer the named club, then the position group; else unique league-wide)
        2. same last name + compatible first name (nickname/prefix), same preference order
        3. same last name + same first initial, on the named club only
        """
        full = normalize_name(name or f"{first or ''} {last or ''}")
        hit = self._pick(self.by_full.get(full, []), teams, group)
        if hit is not None:
            return hit
        splits = [(normalize_name(first), normalize_name(last))] if first and last else _splits(full)
        same_last = _dedupe(p for _, l in splits for p in self.by_last.get(l, []))
        compatible = [p for p in same_last if any(_compatible_first(f, normalize_name(p.first_name)) for f, _ in splits)]
        hit = self._pick(compatible, teams, group)
        if hit is not None or not teams:
            return hit
        initial = [
            p for p in same_last
            if p.team in teams and any(f[:1] == normalize_name(p.first_name)[:1] for f, _ in splits)
        ]  # fmt: skip
        return self._pick(initial, teams, group)

    @staticmethod
    def _pick(cands: list[Player], teams: Collection[str], group: str | None) -> int | None:
        if not cands:
            return None
        on_team = [p for p in cands if p.team in teams]
        pool = on_team or cands
        if group and len(pool) > 1:
            pool = [p for p in pool if p.group == group] or pool
        if len(pool) != 1:
            return None
        # A league-wide (off-club) hit must not contradict a known position group.
        if not on_team and group and pool[0].group != group:
            return None
        return pool[0].id


def _splits(full: str) -> list[tuple[str, str]]:
    tokens = full.split()
    return [(" ".join(tokens[:k]), " ".join(tokens[k:])) for k in range(1, len(tokens))]


def _dedupe(players: Iterable[Player]) -> list[Player]:
    seen: dict[int, Player] = {}
    for p in players:
        seen.setdefault(p.id, p)
    return list(seen.values())


# --------------------------------------------------------------------------- injuries


def parse_injuries(
    payload: dict[str, Any], players: Iterable[Player], *, stats: dict[str, Any] | None = None
) -> tuple[dict[int, Injury], list[dict[str, Any]]]:
    """(player_id -> Injury, unmatched entries). Keeps the newest report per player."""
    index = PlayerIndex(players)
    found: dict[int, Injury] = {}
    unmatched: list[dict[str, Any]] = []
    entries = 0
    for block in payload.get("injuries") or []:
        block_team = nhl_team(block.get("displayName"), None, block.get("id"))
        for item in block.get("injuries") or []:
            entries += 1
            ath = item.get("athlete") or {}
            ath_team = ath.get("team") or {}
            team = block_team or nhl_team(ath_team.get("displayName"), ath_team.get("abbreviation"), ath_team.get("id"))
            group = ESPN_POSITION_GROUP.get(((ath.get("position") or {}).get("abbreviation") or "").upper())
            name = ath.get("displayName") or f"{ath.get('firstName', '')} {ath.get('lastName', '')}".strip()
            pid = index.match(
                name, first=ath.get("firstName"), last=ath.get("lastName"), teams={team} if team else (), group=group
            )
            if pid is None:
                unmatched.append({"name": name, "team": team or block.get("displayName"), "status": item.get("status")})
                continue
            injury = Injury(status=item.get("status") or "Unknown", updated=item.get("date"), note=_injury_note(item))
            prev = found.get(pid)
            if prev is None or (injury.updated or "") > (prev.updated or ""):
                found[pid] = injury
    if stats is not None:
        stats.update(entries=entries, matched=entries - len(unmatched), players=len(found))
    return found, unmatched


def _injury_note(item: dict[str, Any]) -> str | None:
    details = item.get("details") or {}
    what = " - ".join(
        v for v in (details.get("type"), details.get("detail")) if v and v not in ("Not Specified", "Other")
    )
    tags = [t for t in (what, f"est. return {details['returnDate']}" if details.get("returnDate") else "") if t]
    short, long = (item.get("shortComment") or "").strip(), (item.get("longComment") or "").strip()
    comment = short if len(short) >= 20 else (long or short)
    note = " ".join(([f"[{'; '.join(tags)}]"] if tags else []) + ([comment] if comment else []))
    return note or None


def fetch_injuries_payload(*, sources: dict[str, str] | None = None) -> dict[str, Any]:
    payload, sha = get_json(INJURIES_URL)
    if sources is not None:
        sources["espn:injuries"] = sha
    return payload


def fetch_injuries(
    players: Iterable[Player], *, sources: dict[str, str] | None = None, stats: dict[str, Any] | None = None
) -> tuple[dict[int, Injury], list[dict[str, Any]]]:
    return parse_injuries(fetch_injuries_payload(sources=sources), players, stats=stats)


# --------------------------------------------------------------------------- news


def parse_news(
    payload: dict[str, Any], players: Iterable[Player], *, stats: dict[str, Any] | None = None
) -> list[NewsItem]:
    """Headline + publish time per article, tagged with NHL clubs and player IDs. Newest first."""
    index = PlayerIndex(players)
    items: dict[tuple[str, str], NewsItem] = {}
    tags = matched = 0
    unmatched: list[str] = []
    for art in payload.get("articles") or []:
        headline = " ".join((art.get("headline") or "").split())
        published = art.get("published") or art.get("lastModified")
        if not headline or not published:
            continue
        teams: set[str] = set()
        names: list[str] = []
        for cat in art.get("categories") or []:
            if cat.get("type") == "team":
                t = cat.get("team") or {}
                ab = nhl_team(cat.get("description") or t.get("description"), t.get("abbreviation"), cat.get("teamId"))
                if ab:
                    teams.add(ab)
            elif cat.get("type") == "athlete":
                name = cat.get("description") or (cat.get("athlete") or {}).get("description")
                if name:
                    names.append(name)
        pids: set[int] = set()
        for name in names:
            tags += 1
            pid = index.match(name, teams=teams)
            if pid is None:
                unmatched.append(name)
            else:
                matched += 1
                pids.add(pid)
        items.setdefault(
            (published, headline),
            NewsItem(published=published, headline=headline, teams=sorted(teams), player_ids=sorted(pids)),
        )
    if stats is not None:
        stats.update(athlete_tags=tags, athlete_tags_matched=matched, unmatched_athletes=sorted(set(unmatched)))
    return [items[k] for k in sorted(items, reverse=True)]


def fetch_news(
    players: Iterable[Player],
    limit: int = 50,
    *,
    sources: dict[str, str] | None = None,
    stats: dict[str, Any] | None = None,
) -> list[NewsItem]:
    payload, sha = get_json(NEWS_URL.format(limit=limit))
    if sources is not None:
        sources["espn:news"] = sha
    return parse_news(payload, players, stats=stats)
