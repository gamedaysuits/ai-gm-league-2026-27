"""House baseline projection ("gds-baseline-v1") of 2026-27 fantasy points. Pure functions, no I/O.

Hidden from GMs; used by the autodraft bot and for Pick Value Added. Only the
three completed seasons before the target season are used, so the baseline is
identical in every snapshot of a season.

Skaters
  rate   = sum(w * FP) / sum(w * GP) over the window, w = 5/3/2 newest -> oldest
  rate   = (GP * rate + 20 * replacement[F|D]) / (GP + 20)   (GP = raw window total)
  rate  *= age multiplier (+6%/yr under 23, tapering to 0 at 26, flat 26-29, -3%/yr 30-33, -5%/yr 34+)
  games  = season_games * clamp(weighted share of team games played, 0.55, 0.95)
Goalies
  rate   = sum(w * FP) / sum(w * GS), shrunk toward the league-average FP per start (30 GS prior)
  games  = expected starts = season_games * clamp(weighted share of team games started, 0.10, 0.72)
No NHL games in the window: 10 GP at the positional replacement rate (goalies: 5 starts).

GP/GS shares count every season from the player's first full-role season in the
window (>= 50% of club games played; goalies >= 25% started) through the last
completed season; a season missed after that counts as 0. Earlier partial seasons
(debut call-ups) set no role; if no season qualifies, the newest season alone does.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from gmbench.data.models import Projection, SeasonLine

METHOD = "gds-baseline-v1"
SEASON_WEIGHTS = (5.0, 3.0, 2.0)  # last completed season first
SKATER_PRIOR_GP = 20.0
GOALIE_PRIOR_GS = 30.0
SKATER_GP_SHARE = (0.55, 0.95)
GOALIE_GS_SHARE = (0.10, 0.72)
SKATER_ROLE_SHARE = 0.50  # a season below this share before any full-role season = call-up year
GOALIE_ROLE_SHARE = 0.25
ZERO_GP_SKATER_GAMES = 10.0
ZERO_GP_GOALIE_STARTS = 5.0
# Regular-season games per club where it isn't 82 (2026-27 onward: 84, per the 2026-27 club schedules).
SEASON_GAMES = {"20192020": 70, "20202021": 56}
# Replacement pools: players outside the top N per club (32 clubs) by ice time / starts.
REPLACEMENT_SLOTS = {"F": 12 * 32, "D": 6 * 32, "G": 2 * 32}

SKATER_CATEGORIES = {
    "goal": "goals",
    "assist": "assists",
    "point": "points",
    "shot": "shots",
    "pp_point": "pp_points",
    "plus_minus": "plus_minus",
}
GOALIE_CATEGORIES = {"win": "wins", "loss": "losses", "otl": "ot_losses", "shutout": "shutouts"}


@dataclass(frozen=True)
class Baselines:
    """League reference rates, in fantasy points per game (skaters) or per start (goalies)."""

    f_repl: float = 0.30
    d_repl: float = 0.18
    g_avg: float = 1.20
    g_repl: float = 1.00

    def skater_repl(self, group: str) -> float:
        return self.d_repl if group == "D" else self.f_repl


DEFAULT_BASELINES = Baselines()


# --------------------------------------------------------------------------- scoring


def fantasy_points(line: SeasonLine, scoring: Mapping[str, Mapping[str, float]], *, goalie: bool) -> int:
    """Fantasy points for one season line under the league scoring rules (cfg.rules["scoring"]).

    Unknown scoring categories raise KeyError rather than silently scoring zero.
    """
    rules = scoring["goalie" if goalie else "skater"]
    categories = GOALIE_CATEGORIES if goalie else SKATER_CATEGORIES
    total = sum(float(pts) * float(getattr(line, categories[cat]) or 0) for cat, pts in rules.items())
    return round(total)


def is_goalie_line(line: SeasonLine) -> bool:
    return line.gs is not None


# --------------------------------------------------------------------------- seasons


def season_length(season: str) -> int:
    """Regular-season games per club in `season` ("20252026")."""
    if season in SEASON_GAMES:
        return SEASON_GAMES[season]
    return 84 if int(season[:4]) >= 2026 else 82


def season_offset(season: str, target_season: str) -> int:
    """1 for the season just before the target, 2 for the one before that, ... (0 = target)."""
    return int(target_season[:4]) - int(season[:4])


def shift_season(season: str, years: int) -> str:
    start = int(season[:4]) + years
    return f"{start}{start + 1}"


def _window(lines: Sequence[SeasonLine], target_season: str) -> dict[int, SeasonLine]:
    """Completed seasons inside the weighting window, keyed by offset (1 = newest)."""
    out: dict[int, SeasonLine] = {}
    for line in lines:
        off = season_offset(line.season, target_season)
        if 1 <= off <= len(SEASON_WEIGHTS) and off not in out:
            out[off] = line
    return out


def games_share(window: Mapping[int, SeasonLine], target_season: str, *, starts: bool = False) -> float:
    """Weighted share of club games played (or started), from the first full-role season in the window."""
    played = [off for off, line in window.items() if line.gp > 0]
    if not played:
        return 0.0

    def share(off: int) -> float:
        line = window.get(off)
        count = ((line.gs or 0) if starts else line.gp) if line else 0
        return count / season_length(shift_season(target_season, -off))

    first = max(played)
    threshold = GOALIE_ROLE_SHARE if starts else SKATER_ROLE_SHARE
    while first > 1 and share(first) < threshold:
        first -= 1
    offsets = range(1, first + 1)
    return sum(SEASON_WEIGHTS[off - 1] * share(off) for off in offsets) / sum(SEASON_WEIGHTS[off - 1] for off in offsets)


def age_multiplier(age: float | None) -> float:
    """One season of expected change in scoring rate at this age."""
    if age is None:
        return 1.0
    if age < 23:
        growth = 0.06
    elif age < 26:
        growth = 0.06 * (26 - age) / 3
    elif age < 30:
        growth = 0.0
    elif age < 34:
        growth = -0.03
    else:
        growth = -0.05
    return 1.0 + growth


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- projections


def project_skater(
    lines: Sequence[SeasonLine],
    *,
    group: str,
    age: float | None,
    target_season: str,
    baselines: Baselines = DEFAULT_BASELINES,
    season_games: int | None = None,
) -> Projection:
    n_games = season_games or season_length(target_season)
    repl = baselines.skater_repl(group)
    window = _window(lines, target_season)
    gp_total = sum(line.gp for line in window.values())
    if gp_total <= 0:
        return Projection(
            games=ZERO_GP_SKATER_GAMES,
            fantasy_points=round(ZERO_GP_SKATER_GAMES * repl, 1),
            method=f"{METHOD}: no NHL GP in last 3 seasons; {ZERO_GP_SKATER_GAMES:g} GP at {group} replacement rate",
        )
    w_fp = sum(SEASON_WEIGHTS[off - 1] * line.fantasy_points for off, line in window.items())
    w_gp = sum(SEASON_WEIGHTS[off - 1] * line.gp for off, line in window.items())
    rate = (gp_total * (w_fp / w_gp) + SKATER_PRIOR_GP * repl) / (gp_total + SKATER_PRIOR_GP)
    rate *= age_multiplier(age)
    games = n_games * _clamp(games_share(window, target_season), *SKATER_GP_SHARE)
    return Projection(
        games=round(games, 1),
        fantasy_points=round(rate * games, 1),
        method=(
            f"{METHOD}: 5/3/2 GP-weighted FP/GP, {SKATER_PRIOR_GP:g}-GP shrink to {group} replacement, "
            f"age curve; games = {n_games} x GP share clamped {SKATER_GP_SHARE[0]}-{SKATER_GP_SHARE[1]}"
        ),
    )


def project_goalie(
    lines: Sequence[SeasonLine],
    *,
    target_season: str,
    baselines: Baselines = DEFAULT_BASELINES,
    season_games: int | None = None,
) -> Projection:
    """Projection.games is expected starts."""
    n_games = season_games or season_length(target_season)
    window = _window(lines, target_season)
    if sum(line.gp for line in window.values()) <= 0:
        return Projection(
            games=ZERO_GP_GOALIE_STARTS,
            fantasy_points=round(ZERO_GP_GOALIE_STARTS * baselines.g_repl, 1),
            method=f"{METHOD}: no NHL GP in last 3 seasons; {ZERO_GP_GOALIE_STARTS:g} starts at replacement rate",
        )
    gs_total = sum(line.gs or 0 for line in window.values())
    w_fp = sum(SEASON_WEIGHTS[off - 1] * line.fantasy_points for off, line in window.items())
    w_gs = sum(SEASON_WEIGHTS[off - 1] * (line.gs or 0) for off, line in window.items())
    raw = w_fp / w_gs if w_gs > 0 else baselines.g_avg
    rate = (gs_total * raw + GOALIE_PRIOR_GS * baselines.g_avg) / (gs_total + GOALIE_PRIOR_GS)
    starts = n_games * _clamp(games_share(window, target_season, starts=True), *GOALIE_GS_SHARE)
    return Projection(
        games=round(starts, 1),
        fantasy_points=round(rate * starts, 1),
        method=(
            f"{METHOD}: 5/3/2 GS-weighted FP/start, {GOALIE_PRIOR_GS:g}-GS shrink to league average; "
            f"starts = {n_games} x GS share clamped {GOALIE_GS_SHARE[0]}-{GOALIE_GS_SHARE[1]}"
        ),
    )


def project_player(
    group: str,
    lines: Sequence[SeasonLine],
    *,
    age: float | None,
    target_season: str,
    baselines: Baselines = DEFAULT_BASELINES,
    season_games: int | None = None,
) -> Projection:
    if group == "G":
        goalie_lines = [line for line in lines if is_goalie_line(line)]
        return project_goalie(goalie_lines, target_season=target_season, baselines=baselines, season_games=season_games)
    skater_lines = [line for line in lines if not is_goalie_line(line)]
    return project_skater(
        skater_lines, group=group, age=age, target_season=target_season, baselines=baselines, season_games=season_games
    )


def compute_baselines(
    players: Iterable[tuple[str, Sequence[SeasonLine]]],
    target_season: str,
    *,
    slots: Mapping[str, int] = REPLACEMENT_SLOTS,
    fallback: Baselines = DEFAULT_BASELINES,
) -> Baselines:
    """Reference rates from every NHL player's lines (rostered or not) in the weighting window.

    Replacement = pooled FP/GP of skaters outside the top `slots[group]` by total ice time
    in each season (goalies: FP/GS outside the top `slots["G"]` by starts). Goalie
    average = pooled FP/GS of all goalies. Iterate players in a stable order.
    """
    by_season: dict[str, dict[str, list[SeasonLine]]] = defaultdict(lambda: defaultdict(list))
    for group, lines in players:
        for line in _window(lines, target_season).values():
            if line.gp > 0 and (group == "G") == is_goalie_line(line):
                by_season[line.season][group].append(line)
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # key -> [fantasy points, games]

    def add(key: str, pool: Iterable[SeasonLine], starts: bool = False) -> None:
        for line in pool:
            sums[key][0] += line.fantasy_points
            sums[key][1] += (line.gs or 0) if starts else line.gp

    for season in sorted(by_season):
        groups = by_season[season]
        for group in ("F", "D"):
            ranked = sorted(groups.get(group, []), key=lambda l: (l.gp * (l.toi_per_game_s or 0.0), l.gp), reverse=True)
            add(group, ranked[slots[group] :])
        goalies = sorted(groups.get("G", []), key=lambda l: (l.gs or 0, l.gp), reverse=True)
        add("G_avg", goalies, starts=True)
        add("G_repl", goalies[slots["G"] :], starts=True)

    def rate(key: str, default: float) -> float:
        fp, n = sums[key]
        return round(fp / n, 4) if n > 0 else default

    return Baselines(
        f_repl=rate("F", fallback.f_repl),
        d_repl=rate("D", fallback.d_repl),
        g_avg=rate("G_avg", fallback.g_avg),
        g_repl=rate("G_repl", fallback.g_repl),
    )
