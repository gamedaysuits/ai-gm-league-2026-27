"""Unit tests for the house baseline projection (pure functions, no network)."""
from __future__ import annotations

import pytest

from gmbench.config import load_config
from gmbench.data.models import SeasonLine
from gmbench.data.projections import (
    Baselines,
    age_multiplier,
    compute_baselines,
    fantasy_points,
    games_share,
    project_goalie,
    project_player,
    project_skater,
)

TARGET = "20262027"
B = Baselines(f_repl=0.25, d_repl=0.18, g_avg=1.2, g_repl=1.0)
SCORING = {"skater": {"goal": 1, "assist": 1}, "goalie": {"win": 2, "otl": 1, "shutout": 2}}


def sk(season: str, gp: int, fp: int, toi: float = 900.0) -> SeasonLine:
    return SeasonLine(season=season, gp=gp, goals=fp // 2, assists=fp - fp // 2, points=fp, fantasy_points=fp,
                      toi_per_game_s=toi)  # fmt: skip


def gl(season: str, gs: int, wins: int, otl: int, so: int, gp: int | None = None) -> SeasonLine:
    line = SeasonLine(season=season, gp=gp if gp is not None else gs, gs=gs, wins=wins, losses=gs - wins - otl,
                      ot_losses=otl, shutouts=so)  # fmt: skip
    line.fantasy_points = fantasy_points(line, SCORING, goalie=True)
    return line


def three_full(fp_new: int, fp_mid: int, fp_old: int) -> list[SeasonLine]:
    return [sk("20252026", 82, fp_new), sk("20242025", 82, fp_mid), sk("20232024", 82, fp_old)]


def proj(lines, group="F", age=27.0):
    return project_skater(lines, group=group, age=age, target_season=TARGET, baselines=B, season_games=84)


# --------------------------------------------------------------------------- scoring


def test_fantasy_points_use_league_scoring():
    scoring = load_config().rules["scoring"]
    assert fantasy_points(SeasonLine(season="20252026", gp=82, goals=30, assists=40), scoring, goalie=False) == 70
    goalie = SeasonLine(season="20252026", gp=60, gs=58, wins=30, losses=20, ot_losses=5, shutouts=4)
    assert fantasy_points(goalie, scoring, goalie=True) == 2 * 30 + 5 + 2 * 4
    with pytest.raises(KeyError):
        fantasy_points(goalie, {"goalie": {"save": 0.1}}, goalie=True)


# --------------------------------------------------------------------------- age curve


def test_age_curve_shape():
    assert age_multiplier(None) == 1.0
    assert age_multiplier(20) == pytest.approx(1.06)
    assert age_multiplier(24.5) == pytest.approx(1.03)
    assert age_multiplier(27) == age_multiplier(29.9) == 1.0
    assert age_multiplier(31) == pytest.approx(0.97)
    assert age_multiplier(35) == pytest.approx(0.95)
    ages = [18 + 0.5 * i for i in range(46)]
    assert all(age_multiplier(a) >= age_multiplier(b) for a, b in zip(ages, ages[1:]))


def test_age_changes_projection_in_expected_direction():
    lines = three_full(70, 70, 70)
    young, prime, old = proj(lines, age=21), proj(lines, age=27), proj(lines, age=35)
    assert young.fantasy_points > prime.fantasy_points > old.fantasy_points
    assert young.games == prime.games == old.games


# --------------------------------------------------------------------------- skaters


def test_skater_projection_is_monotonic_in_production():
    points = [proj(three_full(fp, fp, fp)).fantasy_points for fp in (10, 30, 50, 70, 90, 110)]
    assert points == sorted(points) and len(set(points)) == len(points)


def test_newer_seasons_weigh_more():
    rising, falling = proj(three_full(90, 60, 60)), proj(three_full(60, 60, 90))
    assert rising.fantasy_points > falling.fantasy_points


def test_shrinkage_pulls_small_samples_toward_replacement():
    tiny = proj([sk("20252026", 5, 10)])  # 2.0 FP/GP over 5 games
    big = proj(three_full(164, 164, 164))  # 2.0 FP/GP over 246 games
    assert tiny.fantasy_points / tiny.games == pytest.approx((5 * 2.0 + 20 * 0.25) / 25, rel=1e-3)
    assert big.fantasy_points / big.games == pytest.approx((246 * 2.0 + 20 * 0.25) / 266, rel=1e-3)
    assert tiny.fantasy_points / tiny.games < big.fantasy_points / big.games < 2.0


def test_games_are_clamped():
    assert proj(three_full(60, 60, 60)).games == pytest.approx(84 * 0.95, abs=0.05)
    assert proj([sk("20252026", 30, 12)]).games == pytest.approx(84 * 0.55, abs=0.05)


def test_debut_callup_season_does_not_cut_expected_games():
    # A full rookie season after a 2-game call-up projects like a full-time player.
    assert proj([sk("20252026", 82, 62), sk("20242025", 2, 2)]).games == pytest.approx(84 * 0.95, abs=0.05)


def test_missed_season_counts_against_expected_games():
    healthy = proj(three_full(60, 60, 60))
    missed_last = proj([sk("20242025", 82, 60), sk("20232024", 82, 60)])
    assert missed_last.games == pytest.approx(84 * 0.55, abs=0.05)
    assert missed_last.games < healthy.games


def test_weights_follow_season_offsets_not_list_positions():
    # 2024-25 missing: 2025-26 gets weight 5 and 2023-24 weight 2 (not 3).
    lines = [sk("20252026", 82, 82), sk("20232024", 82, 41)]
    raw = (5 * 82 + 2 * 41) / (5 * 82 + 2 * 82)
    expected_rate = (164 * raw + 20 * 0.25) / 184
    p = proj(lines)
    assert p.fantasy_points / p.games == pytest.approx(expected_rate, rel=1e-3)
    window = {1: lines[0], 3: lines[1]}
    assert games_share(window, TARGET) == pytest.approx((5 * 1.0 + 3 * 0.0 + 2 * 1.0) / 10)


def test_zero_gp_default_uses_positional_replacement():
    f = proj([])
    d = project_skater([], group="D", age=20, target_season=TARGET, baselines=B)
    stale = proj([sk("20192020", 70, 50)])  # outside the three-season window
    assert (f.games, f.fantasy_points) == (10.0, 2.5)
    assert (d.games, d.fantasy_points) == (10.0, 1.8)
    assert stale.fantasy_points == f.fantasy_points
    assert "no NHL GP" in f.method


# --------------------------------------------------------------------------- goalies


def test_goalie_starter_beats_backup_and_uses_starts():
    starter = [gl("20252026", 58, 34, 7, 4), gl("20242025", 60, 35, 8, 5), gl("20232024", 55, 30, 6, 3)]
    backup = [gl("20252026", 25, 12, 3, 1), gl("20242025", 24, 11, 3, 1), gl("20232024", 22, 10, 2, 0)]
    ps = project_goalie(starter, target_season=TARGET, baselines=B, season_games=84)
    pb = project_goalie(backup, target_season=TARGET, baselines=B, season_games=84)
    assert ps.fantasy_points > pb.fantasy_points
    assert ps.games == pytest.approx(84 * (5 * 58 + 3 * 60 + 2 * 55) / (10 * 82), abs=0.05)  # 0.707: under the cap
    assert pb.games < ps.games
    # per-start rate is shrunk toward the league average
    raw = sum(w * l.fantasy_points for w, l in zip((5, 3, 2), starter)) / sum(w * l.gs for w, l in zip((5, 3, 2), starter))
    assert min(raw, B.g_avg) <= ps.fantasy_points / ps.games <= max(raw, B.g_avg)


def test_goalie_start_share_is_clamped_and_zero_gp_defaults():
    workhorse = [gl("20252026", 70, 40, 8, 6), gl("20242025", 70, 40, 8, 6), gl("20232024", 70, 40, 8, 6)]
    assert project_goalie(workhorse, target_season=TARGET, baselines=B).games == pytest.approx(84 * 0.72, abs=0.05)
    relief = [gl("20252026", 0, 0, 0, 0, gp=3)]
    assert project_goalie(relief, target_season=TARGET, baselines=B).games == pytest.approx(84 * 0.10, abs=0.05)
    none = project_goalie([], target_season=TARGET, baselines=B)
    assert (none.games, none.fantasy_points) == (5.0, 5.0)


def test_project_player_dispatches_by_group():
    goalie_lines = [gl("20252026", 50, 28, 6, 3)]
    skater_lines = [sk("20252026", 82, 60)]
    g = project_player("G", goalie_lines + skater_lines, age=28, target_season=TARGET, baselines=B)
    f = project_player("F", goalie_lines + skater_lines, age=28, target_season=TARGET, baselines=B)
    assert "start" in g.method and g.games <= 84 * 0.72
    assert "GP-weighted" in f.method and f.games > g.games
    # a skater's goalie-report lines are ignored, and vice versa
    assert project_player("F", goalie_lines, age=28, target_season=TARGET, baselines=B).games == 10.0
    assert g.method.startswith("gds-baseline-v1")


# --------------------------------------------------------------------------- baselines


def test_compute_baselines_pools_players_outside_top_slots():
    players = [
        ("F", [sk("20252026", 82, 80, toi=1200)]),
        ("F", [sk("20252026", 40, 10, toi=700)]),
        ("F", [sk("20252026", 20, 5, toi=600)]),
        ("D", [sk("20252026", 82, 40, toi=1400)]),
        ("G", [gl("20252026", 60, 35, 6, 4)]),
        ("G", [gl("20252026", 20, 8, 2, 1)]),
        ("F", [sk("20192020", 70, 70)]),  # outside the window: ignored
    ]
    b = compute_baselines(players, TARGET, slots={"F": 1, "D": 1, "G": 1}, fallback=B)
    assert b.f_repl == pytest.approx(15 / 60)
    assert b.d_repl == B.d_repl  # nobody beyond the top slot -> fallback
    starter_fp, backup_fp = 2 * 35 + 6 + 2 * 4, 2 * 8 + 2 + 2 * 1
    assert b.g_avg == pytest.approx((starter_fp + backup_fp) / 80, abs=1e-4)
    assert b.g_repl == pytest.approx(backup_fp / 20, abs=1e-4)
