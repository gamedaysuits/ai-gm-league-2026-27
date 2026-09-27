"""Engine invariants: rules, ledger integrity, and a full zero-cost draft."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from gmbench.config import load_config
from gmbench.draft.order import derive_order, round_at, round_time
from gmbench.draft.run import DraftDeps, run_draft
from gmbench.ledger import ChainError, Ledger
from gmbench.league import init_league, set_draft_order
from gmbench.llm.fake import FakeClient
from gmbench.rules import RosterRules, lineup_problem, pick_problem, snake_order
from gmbench.state import replay, state_sha256

RULES = RosterRules(minimums={"F": 6, "D": 4, "G": 2}, bench=2, max_goalies=3)


def test_snake_order_reverses_each_round() -> None:
    slots = snake_order(["a", "b", "c"], 3)
    assert [s.team_id for s in slots] == ["a", "b", "c", "c", "b", "a", "a", "b", "c"]
    assert slots[-1].pick_no == 9 and slots[-1].round == 3


def test_feasibility_blocks_picks_that_strand_minimums() -> None:
    assert pick_problem(Counter({"F": 6, "D": 4, "G": 1}), "F", RULES) is None  # 12th pick: 2 spots left, 1 G needed
    assert pick_problem(Counter({"F": 7, "D": 4, "G": 1}), "F", RULES) is None  # 13th pick: 1 spot left for the G
    assert pick_problem(Counter({"F": 8, "D": 4}), "F", RULES) is not None  # 13th pick: 1 spot left, 2 G needed
    assert pick_problem(Counter({"F": 6, "D": 4, "G": 3}), "G", RULES) == "goalie limit is 3"


def test_lineup_validation() -> None:
    roster = {1: "F", 2: "F", 3: "F", 4: "F", 5: "F", 6: "F", 7: "D", 8: "D", 9: "D", 10: "D", 11: "G", 12: "G", 13: "F", 14: "D"}
    assert lineup_problem(roster, list(range(1, 13)), [13, 14], RULES) is None
    assert lineup_problem(roster, [1, 2, 3, 4, 5, 13, 7, 8, 9, 10, 11, 12], [6, 14], RULES) is None
    assert "exactly" in lineup_problem(roster, [1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 11, 13], [10, 12], RULES)


def test_ledger_detects_tampering(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    for i in range(5):
        ledger.append("TEST", "t", {"i": i})
    assert ledger.verify()[0] == 5
    lines = ledger.path.read_text().splitlines()
    doc = json.loads(lines[2])
    doc["payload"]["i"] = 99
    lines[2] = json.dumps(doc)
    ledger.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainError) as err:
        ledger.verify()
    assert err.value.seq == 3


def test_ledger_repairs_torn_tail(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    ledger.append("TEST", "t", {"i": 1})
    with open(ledger.path, "a") as fh:
        fh.write('{"seq": 2, "partial')
    assert ledger.repair_torn_tail()
    assert ledger.verify()[0] == 1
    ledger.append("TEST", "t", {"i": 2})
    assert ledger.verify()[0] == 2


def test_drand_round_math_and_order_is_deterministic() -> None:
    r = 32576212
    assert round_at(round_time(r)) == r
    teams = ["a", "b", "c", "d"]
    assert derive_order("ab" * 32, teams) == derive_order("ab" * 32, list(reversed(teams)))


def test_full_fake_draft(tmp_path: Path) -> None:
    cfg = load_config()
    ledger = Ledger(tmp_path / "ledger" / "league.jsonl")
    init_league(ledger, cfg, "synthetic:7")
    set_draft_order(ledger, cfg, {"round": None, "randomness": "11" * 32}, method="test-seed")
    snapshot_ref = replay(ledger.events()).snapshot["ref"]
    from gmbench.league import load_run_snapshot

    deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=load_run_snapshot(snapshot_ref), client=FakeClient(),
                     root=tmp_path, today="2026-09-28", as_of="2026-09-28", log=lambda _: None)
    state = run_draft(deps, on_failure="pause")

    rules = RosterRules.from_config(cfg.rules)
    assert len(state.picks) == 14 * 14
    assert len(state.owner) == 14 * 14  # no player drafted twice
    for team in state.teams.values():
        assert len(team.roster) == rules.roster_size
        for g, n in rules.minimums.items():
            assert team.groups[g] >= n, (team.id, dict(team.groups))
        assert team.groups["G"] <= rules.max_goalies
    assert not any(p["auto"] and p["auto"]["reason"] != "control_bot" for p in state.picks)
    count, _ = ledger.verify()
    assert state_sha256(replay(ledger.events())) == state_sha256(replay(ledger.events()))
    assert (tmp_path / "exports" / "draft.json").exists()
    assert count > 196
