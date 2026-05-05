"""AcceptanceGate composite OOS checks (issue #247, step 5)."""

from __future__ import annotations

import pytest

from investment_team.models import BacktestConfig, BacktestResult
from investment_team.strategy_lab.quality_gates.acceptance_gate import (
    AcceptanceGate,
    summarize_acceptance_reason,
)


def _cfg(**overrides) -> BacktestConfig:
    base = dict(start_date="2022-01-03", end_date="2022-12-30")
    base.update(overrides)
    return BacktestConfig(**base)


def _result(
    *,
    deflated_sharpe=0.95,
    oos_sharpe=1.1,
    is_oos_degradation_pct=20.0,
    oos_trade_count=40,
    regime_results=None,
) -> BacktestResult:
    if regime_results is None:
        regime_results = [
            {"regime": "vix_q1", "beat_benchmark": True},
            {"regime": "vix_q2", "beat_benchmark": True},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ]
    return BacktestResult(
        total_return_pct=15.0,
        annualized_return_pct=12.0,
        volatility_pct=10.0,
        sharpe_ratio=1.2,
        max_drawdown_pct=8.0,
        win_rate_pct=55.0,
        profit_factor=1.5,
        deflated_sharpe=deflated_sharpe,
        oos_sharpe=oos_sharpe,
        is_oos_degradation_pct=is_oos_degradation_pct,
        oos_trade_count=oos_trade_count,
        regime_results=regime_results,
    )


def test_pass_when_all_four_criteria_met():
    gate = AcceptanceGate()
    results = gate.check(
        _result(), _cfg(dsr_threshold=0.9, max_is_oos_degradation_pct=25, min_oos_trades=30)
    )
    assert len(results) == 4
    assert all(r.passed for r in results)


def test_fail_when_dsr_below_threshold():
    gate = AcceptanceGate()
    results = gate.check(_result(deflated_sharpe=0.5), _cfg(dsr_threshold=0.9))
    dsr_result = results[0]
    assert not dsr_result.passed
    assert dsr_result.severity == "critical"
    assert "below" in dsr_result.details.lower()


def test_fail_when_is_oos_degradation_exceeds_ceiling():
    gate = AcceptanceGate()
    results = gate.check(_result(is_oos_degradation_pct=45.0), _cfg(max_is_oos_degradation_pct=30))
    deg_result = results[1]
    assert not deg_result.passed
    assert "exceeds" in deg_result.details


def test_fail_when_oos_trades_below_minimum():
    gate = AcceptanceGate()
    results = gate.check(_result(oos_trade_count=10), _cfg(min_oos_trades=30))
    count_result = results[2]
    assert not count_result.passed
    assert "below" in count_result.details.lower()


def test_fail_when_regime_beats_below_threshold():
    gate = AcceptanceGate()
    # Beats only 1 of 4 regimes.
    regimes = [{"regime": f"vix_q{i}", "beat_benchmark": (i == 1)} for i in (1, 2, 3, 4)]
    results = gate.check(_result(regime_results=regimes), _cfg())
    regime_result = results[3]
    assert not regime_result.passed
    assert "1 of 4" in regime_result.details


def test_missing_fields_emit_single_incomplete_warning():
    gate = AcceptanceGate()
    r = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=1.0,
        volatility_pct=1.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=1.0,
        win_rate_pct=50.0,
        profit_factor=1.0,
    )
    results = gate.check(r, _cfg())
    assert len(results) == 1
    assert not results[0].passed
    assert "missing" in results[0].details.lower()
    # Enumerates each missing field so the orchestrator can report precisely.
    for field in ("oos_sharpe", "is_oos_degradation_pct", "oos_trade_count"):
        assert field in results[0].details


def test_summarize_acceptance_reason_all_pass():
    gate = AcceptanceGate()
    results = gate.check(_result(), _cfg(dsr_threshold=0.9))
    assert summarize_acceptance_reason(results) == "all four criteria met"


def test_summarize_acceptance_reason_lists_failing_gates():
    gate = AcceptanceGate()
    results = gate.check(
        _result(deflated_sharpe=0.3, oos_trade_count=5),
        _cfg(dsr_threshold=0.9, min_oos_trades=30),
    )
    summary = summarize_acceptance_reason(results)
    assert "DSR" in summary
    assert "trade count" in summary.lower()


def test_n_trials_appears_in_dsr_detail_when_provided():
    gate = AcceptanceGate()
    results = gate.check(_result(), _cfg(dsr_threshold=0.9), n_trials=42)
    assert "n_trials=42" in results[0].details


def test_invalid_min_regime_beats_raises():
    with pytest.raises(ValueError, match="min_regime_beats"):
        AcceptanceGate(min_regime_beats=-1)


def test_regime_subwindows_empty_fails_regime_gate():
    gate = AcceptanceGate()
    results = gate.check(_result(regime_results=[]), _cfg())
    regime_result = results[3]
    assert not regime_result.passed
    assert "No regime subwindows" in regime_result.details
