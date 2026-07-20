"""Regression test: ``_run_synthesis_loop`` must not crash on an unbound
``round_num`` if ``MAX_CODE_REFINEMENT_ROUNDS`` is ever 0.

``round_num`` is bound by the loop's own ``for round_num in
range(MAX_CODE_REFINEMENT_ROUNDS):`` statement. If the cap were 0, the loop
body never runs and, absent a pre-loop initializer, the post-loop invariant
check's f-string reference to ``round_num`` would raise ``NameError``
instead of the intended diagnostic. This test drives the loop directly with
the cap patched to 0 so the zero-rounds path is exercised without needing to
stub any per-round gate/sandbox collaborators (they're never reached).
"""

from __future__ import annotations

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="zero-rounds-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        target_symbols=["QQQ"],
        strategy_code="from contract import Strategy\n",
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
    )


def test_zero_refinement_rounds_does_not_raise(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_module, "MAX_CODE_REFINEMENT_ROUNDS", 0)
    orch = StrategyLabOrchestrator()

    outcome = orch._run_synthesis_loop(
        spec=_spec(),
        code="from contract import Strategy\n",
        config=_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *a, **k: None,
    )

    # Neither flag is set — the loop never ran, so nothing succeeded and
    # nothing exhausted a (zero-length) round budget. The regression this
    # test guards against is a NameError, not this specific flag state.
    assert outcome.execution_succeeded is False
    assert outcome.max_rounds_exhausted is False
