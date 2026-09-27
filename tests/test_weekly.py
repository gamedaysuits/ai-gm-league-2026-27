"""A full scripted week: proposal → acceptance → execution, waivers, lineups, lock."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gmbench.config import TeamSpec, load_config
from gmbench.draft.run import DraftDeps, run_draft
from gmbench.league import init_league, load_run_snapshot, set_draft_order
from gmbench.ledger import Ledger
from gmbench.llm.fake import FakeClient
from gmbench.llm.openrouter import LLMResult
from gmbench.postdraft import run_post_draft
from gmbench.rules import RosterRules, lineup_problem
from gmbench.season.weekly import run_week
from gmbench.state import LeagueState, replay


class ScriptedWeek:
    """astra offers sol a 1-for-1 forward swap and claims a free agent; sol accepts; everyone else passes."""

    def __init__(self, state_fn: Callable[[], LeagueState], snapshot) -> None:
        self.state_fn, self.snap, self.n = state_fn, snapshot, 0

    def chat(self, spec: TeamSpec, messages, tools=None, **_: Any) -> LLMResult:
        names = {t["function"]["name"] for t in tools or []}
        st = self.state_fn()
        calls: list[tuple[str, dict]] = []
        if "propose_trade" in names and spec.id == "astra":
            mine = [p for p in st.teams["astra"].roster if st.player_group[p] == "F"]
            theirs = [p for p in st.teams["sol"].roster if st.player_group[p] == "F"]
            fa = next(p.id for p in sorted(self.snap.players.values(), key=lambda p: p.id)
                      if p.id not in st.owner and p.group == "F")
            calls = [("propose_trade", {"to_team": "sol", "give": [mine[-1]], "get": [theirs[-1]], "message": "Fair swap."}),
                     ("submit_waiver_claims", {"claims": [{"add": fa, "drop": mine[-2]}]}),
                     ("post_chat", {"message": "Busy week in the war room."}),
                     ("submit_presser", {"line": "We're just getting started."})]
        elif "respond_trade" in names and "set_lineup" not in names and spec.id == "sol":
            open_ = [t for t in st.trades.values() if t["recipient"] == "sol" and t["status"] == "open"]
            calls = [("respond_trade", {"trade_id": t["trade_id"], "action": "accept", "message": "Deal."}) for t in open_]
        calls.append(("end_turn", {}))
        self.n += 1
        tcs = [{"id": f"c{self.n}_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
               for i, (n, a) in enumerate(calls)]
        return LLMResult(message={"role": "assistant", "content": "", "tool_calls": tcs}, finish_reason="tool_calls",
                         model=spec.model, provider="Scripted", usage={}, cost_usd=0.0, latency_s=0.0, gen_id=str(self.n))


def test_scripted_trading_week(tmp_path: Path) -> None:
    cfg = load_config()
    ledger = Ledger(tmp_path / "ledger" / "league.jsonl")
    init_league(ledger, cfg, "synthetic:7")
    set_draft_order(ledger, cfg, {"round": None, "randomness": "22" * 32}, method="test-seed")
    snap = load_run_snapshot("synthetic:7")
    deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snap, client=FakeClient(), root=tmp_path,
                     today="2026-09-28", as_of="2026-09-28", log=lambda _: None)
    run_draft(deps, reactions=False)
    run_post_draft(deps, week_start=date(2026, 9, 29))
    before = replay(ledger.events())
    astra_before, sol_before = list(before.teams["astra"].roster), list(before.teams["sol"].roster)

    deps.client = ScriptedWeek(lambda: replay(ledger.events()), snap)
    state = run_week(deps, week_start=date(2026, 10, 12), games_dir=tmp_path / "games",
                     now=datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc))

    kinds = [e["type"] for e in ledger.events()]
    assert "TRADE_EXECUTED" in kinds and "WAIVERS_PROCESSED" in kinds and kinds[-1] == "WEEK_LOCKED"
    executed = next(e["payload"] for e in ledger.events({"TRADE_EXECUTED"}))
    assert executed["give"][0] in state.teams["sol"].roster and executed["get"][0] in state.teams["astra"].roster
    assert set(state.teams["astra"].roster) != set(astra_before) and set(state.teams["sol"].roster) != set(sol_before)
    rules = RosterRules.from_config(cfg.rules)
    for team in state.teams.values():
        assert len(team.roster) == rules.roster_size
        lu = team.lineup
        assert lu["week"] == "2026-10-12"
        assert lineup_problem({p: state.player_group[p] for p in team.roster}, lu["active"], lu["bench"], rules) is None
    assert any(c["team"] == "astra" for c in state.chat)
    assert ledger.verify()[0] == len(kinds)


def test_week_after_lock_only_carries_over(tmp_path: Path) -> None:
    cfg = load_config()
    ledger = Ledger(tmp_path / "ledger" / "league.jsonl")
    init_league(ledger, cfg, "synthetic:7")
    set_draft_order(ledger, cfg, {"round": None, "randomness": "33" * 32}, method="test-seed")
    snap = load_run_snapshot("synthetic:7")
    deps = DraftDeps(cfg=cfg, ledger=ledger, snapshot=snap, client=FakeClient(), root=tmp_path,
                     today="2026-09-28", as_of="2026-09-28", log=lambda _: None)
    run_draft(deps, reactions=False)
    run_post_draft(deps, week_start=date(2026, 9, 29))
    state = run_week(deps, week_start=date(2026, 10, 5), games_dir=tmp_path / "games",
                     now=datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc))
    sessions = [e for e in ledger.events({"SESSION_COMPLETED"}) if e["payload"]["kind"].startswith("week_")]
    assert sessions == []
    assert all(t.lineup["week"] == "2026-10-05" for t in state.teams.values())
