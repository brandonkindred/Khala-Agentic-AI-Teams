"""Realism-gate wiring inside :class:`StrategyLabOrchestrator`.

Exercises two seams:

* :meth:`_run_realism_gates` — pure helper that runs the verification-phase
  realism gates against the final spec + trade ledger. Empty inputs short-
  circuit; a multi-target spec with a single-symbol ledger emits the
  ``target_symbol_coverage`` breadth warning.
* :meth:`_run_verification_phase` — when realism produces a ``critical``
  result, ``is_winning`` flips to ``False`` and ``acceptance_reason`` gains
  a ``realism_failed: ...`` suffix following the same veto convention as
  ``exit_rule_conformance_failed`` and ``alignment_failed``.

Lower-level gate behaviour is covered by
``test_backtest_anomaly_realism.py`` and the breadth extensions in
``test_target_symbol_coverage.py``. This file only proves the orchestrator
threads them through to the publication decision.
"""

from __future__ import annotations

from typing import List, Optional

from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec(target_symbols: Optional[List[str]] = None) -> StrategySpec:
    return StrategySpec(
        strategy_id="realism-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        target_symbols=target_symbols or [],
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
        walk_forward_enabled=True,
    )


def _cost_stress_payload_passing() -> List[dict]:
    """Cost-stress sweep with a passing 2.0× row so the realism cost-stress
    gate doesn't veto in tests focused on other behaviour."""
    return [
        {
            "multiplier": 1.0,
            "sharpe_ratio": 1.2,
            "annualized_return_pct": 12.0,
            "max_drawdown_pct": 6.0,
            "trade_count": 100,
        },
        {
            "multiplier": 2.0,
            "sharpe_ratio": 0.5,
            "annualized_return_pct": 7.0,
            "max_drawdown_pct": 8.0,
            "trade_count": 100,
        },
        {
            "multiplier": 3.0,
            "sharpe_ratio": 0.1,
            "annualized_return_pct": 2.0,
            "max_drawdown_pct": 10.0,
            "trade_count": 100,
        },
    ]


def _metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=20.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=10.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=_cost_stress_payload_passing(),
        acceptance_reason="walk_forward_passed: all four criteria met",
    )


def _trade(symbol: str, trade_num: int) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 1):02d}",
        exit_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 2):02d}",
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=1.0,
        hold_days=14,
        outcome="win",
        cumulative_pnl=10.0 * trade_num,
        entry_reason="compiled_entry:entry[0]",
    )


def _orch() -> StrategyLabOrchestrator:
    """Minimal orchestrator instance for testing pure helpers.

    The constructor walks through agent and gate initialisation; nothing
    here hits the network or LLM provider, so the bare instance is safe to
    construct in tests.
    """
    return StrategyLabOrchestrator(convergence_tracker=ConvergenceTracker())


# ---------------------------------------------------------------------------
# _run_realism_gates
# ---------------------------------------------------------------------------


def test_run_realism_gates_returns_empty_when_no_trades():
    orch = _orch()
    results = orch._run_realism_gates(
        spec=_spec(target_symbols=["QQQ", "SPY"]),
        trades=[],
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )
    assert results == []


def test_run_realism_gates_returns_empty_when_execution_failed():
    orch = _orch()
    results = orch._run_realism_gates(
        spec=_spec(target_symbols=["QQQ", "SPY"]),
        trades=[_trade("QQQ", 1)],
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=False,
    )
    assert results == []


def test_run_realism_gates_emits_breadth_warning_for_multi_target_single_symbol_ledger():
    orch = _orch()
    spec = _spec(target_symbols=["QQQ", "SPY", "IWM"])
    trades = [_trade("QQQ", i + 1) for i in range(5)]

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    breadth_warnings = [
        r
        for r in results
        if not r.passed and r.severity == "warning" and r.gate_name == "target_symbol_coverage"
    ]
    assert len(breadth_warnings) == 1
    assert breadth_warnings[0].phase == "verification"
    assert "QQQ" in breadth_warnings[0].details


def test_run_realism_gates_passes_when_ledger_uses_full_universe():
    orch = _orch()
    spec = _spec(target_symbols=["QQQ", "SPY"])
    trades = [_trade("QQQ", 1), _trade("SPY", 2)]

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    # No criticals from any gate; breadth specifically passes (the
    # universe test is the focus here — other gates may emit warnings
    # from stub fixture gaps like unfired signal-exit rules).
    breadth = [r for r in results if r.gate_name == "target_symbol_coverage"]
    assert all(r.passed for r in breadth)
    assert not any(not r.passed and r.severity == "critical" for r in results)


def test_run_realism_gates_cost_stress_self_skips_when_not_requested_by_config():
    """When ``config.cost_stress=False`` (legacy single-window OR
    walk-forward-fallback path where the operator hand-built the config
    without enabling the sweep), the cost-stress realism gate must
    self-skip with an info result — never produce a critical that would
    veto a path the realism cycle isn't responsible for.

    Enforcement of "mandatory cost-stress on winning-candidate runs"
    lives at the production entrypoint (``_strategy_lab_worker``
    force-enables the flag), not inside the gate. This test is the
    regression guard against an earlier draft where any
    ``walk_forward_enabled=True`` config without cost-stress would have
    fired critical and broken every hand-built Strategy Lab run.
    """
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"])
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    metrics = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=8.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=None,
    )
    # ``_config()`` here defaults to cost_stress=False (the BacktestConfig
    # default); only ``walk_forward_enabled=True`` is set explicitly.
    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=metrics,
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    cost_stress_results = [r for r in results if r.gate_name == "cost_stress_realism"]
    assert len(cost_stress_results) == 1
    assert cost_stress_results[0].passed is True
    assert cost_stress_results[0].severity == "info"
    assert "not requested" in cost_stress_results[0].details


def test_run_realism_gates_cost_stress_critical_when_2x_sharpe_negative():
    """End-to-end: a cost-stress payload with negative 2.0× Sharpe makes
    the realism cycle emit a critical that the caller will treat as a
    publication veto."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"])
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    bad_payload = [
        {
            "multiplier": 1.0,
            "sharpe_ratio": 1.0,
            "annualized_return_pct": 10.0,
            "max_drawdown_pct": 5.0,
            "trade_count": 50,
        },
        {
            "multiplier": 2.0,
            "sharpe_ratio": -0.4,
            "annualized_return_pct": -2.0,
            "max_drawdown_pct": 15.0,
            "trade_count": 50,
        },
    ]
    metrics = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=8.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=bad_payload,
    )

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=metrics,
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    criticals = [r for r in results if not r.passed and r.severity == "critical"]
    assert len(criticals) == 1
    assert criticals[0].gate_name == "cost_stress_realism"


# ---------------------------------------------------------------------------
# _run_verification_phase: realism critical → veto
# ---------------------------------------------------------------------------


def test_verification_phase_vetoes_is_winning_on_realism_critical(monkeypatch):
    """A synthetic realism critical must flip ``is_winning`` to False and
    rewrite ``acceptance_reason`` with the ``realism_failed:`` suffix.

    The acceptance gate is stubbed to pass cleanly so realism is the only
    veto in play; walk-forward evaluation is a no-op pass-through.
    """
    orch = _orch()

    # Walk-forward returns metrics unchanged so the acceptance branch fires.
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            ),
        ],
    )

    # Realism returns a critical finding (synthetic stand-in for any of the
    # eight realism rules — exercising the veto wiring, not the gate's
    # internal logic).
    monkeypatch.setattr(
        orch,
        "_run_realism_gates",
        lambda **_kwargs: [
            QualityGateResult(
                gate_name="liquidity_realism",
                passed=False,
                severity="critical",
                phase="verification",
                details="adjusted profit factor 0.7 < 1.0 after slippage haircut",
            ),
        ],
    )

    spec = _spec(target_symbols=["QQQ"])
    trades = [_trade("QQQ", i + 1) for i in range(20)]
    metrics = _metrics()
    config = _config()
    market_data: dict = {"QQQ": []}
    all_gate_results: List[QualityGateResult] = []

    outcome = orch._run_verification_phase(
        spec=spec,
        trades=trades,
        metrics=metrics,
        market_data=market_data,
        config=config,
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
    )

    assert outcome.is_winning is False
    reason = outcome.metrics.acceptance_reason or ""
    assert "realism_failed:" in reason
    assert "liquidity_realism" not in reason  # detail string, not gate name
    assert "profit factor" in reason
    # The stale ``walk_forward_passed`` success summary must be REPLACED by
    # the veto cause (matches the conformance + alignment veto convention).
    assert "all four criteria met" not in reason


def test_verification_phase_does_not_veto_on_realism_warning(monkeypatch):
    """Warning-severity realism findings must NOT flip ``is_winning`` — only
    ``critical`` veto. The persisted gate list still records the warning."""
    orch = _orch()

    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            ),
        ],
    )
    monkeypatch.setattr(
        orch,
        "_run_realism_gates",
        lambda **_kwargs: [
            QualityGateResult(
                gate_name="target_symbol_coverage",
                passed=False,
                severity="warning",
                phase="verification",
                details="single_symbol_breadth",
            ),
        ],
    )

    spec = _spec(target_symbols=["QQQ", "SPY"])
    trades = [_trade("QQQ", i + 1) for i in range(20)]
    all_gate_results: List[QualityGateResult] = []

    outcome = orch._run_verification_phase(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
    )

    assert outcome.is_winning is True
    breadth_gates = [g for g in all_gate_results if g.gate_name == "target_symbol_coverage"]
    assert len(breadth_gates) == 1
    assert breadth_gates[0].severity == "warning"
    reason = outcome.metrics.acceptance_reason or ""
    assert "realism_failed:" not in reason
