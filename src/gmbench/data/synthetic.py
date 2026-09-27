"""Deterministic synthetic snapshot for tests and zero-cost rehearsals (not real players)."""
from __future__ import annotations

import random
from datetime import date, timedelta

from gmbench.data.models import Injury, NewsItem, Player, Projection, SeasonLine, Snapshot

TEAMS = ("ANA BOS BUF CGY CAR CHI COL CBJ DAL DET EDM FLA LAK MIN MTL NSH NJD NYI NYR OTT PHI PIT SJS SEA "
         "STL TBL TOR UTA VAN VGK WSH WPG").split()
SEASONS = ("20252026", "20242025", "20232024")


def synthetic_snapshot(seed: int = 7, *, per_team: dict[str, int] | None = None) -> Snapshot:
    rng = random.Random(seed)
    per_team = per_team or {"C": 5, "L": 5, "R": 5, "D": 8, "G": 3}
    players: dict[int, Player] = {}
    pid = 8_400_000
    for team in TEAMS:
        for pos, n in per_team.items():
            for i in range(n):
                pid += 1
                group = "G" if pos == "G" else ("D" if pos == "D" else "F")
                talent = rng.betavariate(2, 5)
                seasons = []
                for s in SEASONS:
                    gp = rng.randint(40, 82)
                    if group == "G":
                        gs = int(gp * rng.uniform(0.3, 0.8))
                        w = int(gs * (0.4 + talent * 0.3))
                        otl, so = rng.randint(0, 6), rng.randint(0, 5)
                        seasons.append(SeasonLine(season=s, teams=team, gp=gp, gs=gs, wins=w, losses=gs - w, ot_losses=otl,
                                                  shutouts=so, save_pct=0.9, gaa=2.8, fantasy_points=2 * w + otl + 2 * so))
                    else:
                        ppg = talent * (1.5 if group == "F" else 0.9)
                        g = int(gp * ppg * 0.4)
                        a = int(gp * ppg * 0.6)
                        seasons.append(SeasonLine(season=s, teams=team, gp=gp, goals=g, assists=a, points=g + a,
                                                  toi_per_game_s=900 + talent * 600, fantasy_points=g + a))
                proj = sum(x.fantasy_points for x in seasons) / 3
                players[pid] = Player(
                    id=pid, name=f"{team} {pos}{i + 1} Player", first_name=f"{pos}{i + 1}", last_name=f"{team} Player",
                    team=team, position=pos, group=group, age=round(rng.uniform(19, 37), 1), seasons=seasons,
                    injury=Injury(status="Out", note="lower body") if rng.random() < 0.03 else None,
                    projection=Projection(games=78, fantasy_points=round(proj, 1), method="synthetic"),
                )
    start = date(2026, 9, 29)
    team_games = {t: [(start + timedelta(days=2 * k + (i % 2))).isoformat() for k in range(82)] for i, t in enumerate(TEAMS)}
    news = [NewsItem(published="2026-09-25T12:00:00Z", headline=f"{t} finalize opening-night roster", teams=[t]) for t in TEAMS[:5]]
    return Snapshot(kind="synthetic", taken_at="2026-09-26T00:00:00Z", season="20262027", as_of_date="2026-09-28",
                    players=players, team_games=team_games, news=news, sources={"synthetic": str(seed)})
