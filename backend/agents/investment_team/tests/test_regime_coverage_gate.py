"""Unit tests for :class:`RegimeCoverageGate`."""

from __future__ import annotations

from typing import List, Optional

from investment_team.models import BacktestResult
from investment_team.strategy_lab.quality_gates.realism.regime_coverage import (
    GATE,
    RegimeCoverageGate,
)


def _metrics(regime_results: Optional[List[dict]] = None) -> BacktestResult:
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
        regime_results=regime_results,
    )


def _row(*, regime: str, n_obs: int, strategy_cumret: float) -> dict:
    return {
        "regime": regime,
        "n_obs": n_obs,
        "strategy_cumret": strategy_cumret,
        "benchmark_cumret": 0.05,
        "beat_benchmark": strategy_cumret > 0.05,
    }


def _criticals(results):
    return [r for r in results if not r.passed and r.severity == "critical"]


def _warnings(results):
    return [r for r in results if not r.passed and r.severity == "warning"]


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


def test_skips_when_regime_results_none():
    gate = RegimeCoverageGate()
    results = gate.check(_metrics(regime_results=None))
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_skips_when_regime_results_empty():
    gate = RegimeCoverageGate()
    results = gate.check(_metrics(regime_results=[]))
    assert all(r.passed and r.severity == "info" for r in results)


def test_info_when_no_regime_has_observations():
    gate = RegimeCoverageGate()
    payload = [
        _row(regime="vix_q1", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q2", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q3", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q4", n_obs=0, strategy_cumret=0.0),
    ]
    results = gate.check(_metrics(payload))
    assert all(r.passed for r in results)
    assert "no regime has any observations" in results[0].details


# ---------------------------------------------------------------------------
# Single-regime warning
# ---------------------------------------------------------------------------


def test_warning_when_only_one_regime_covered():
    gate = RegimeCoverageGate()
    payload = [
        _row(regime="vix_q1", n_obs=120, strategy_cumret=0.12),
        _row(regime="vix_q2", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q3", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q4", n_obs=0, strategy_cumret=0.0),
    ]
    results = gate.check(_metrics(payload))
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "vix_q1" in warnings[0].details
    assert _criticals(results) == []


# ---------------------------------------------------------------------------
# Critical: losing regime
# ---------------------------------------------------------------------------


def test_critical_when_any_covered_regime_has_negative_cumret():
    gate = RegimeCoverageGate()
    payload = [
        _row(regime="vix_q1", n_obs=80, strategy_cumret=0.10),
        _row(regime="vix_q2", n_obs=60, strategy_cumret=0.05),
        _row(regime="vix_q3", n_obs=40, strategy_cumret=-0.08),
        _row(regime="vix_q4", n_obs=20, strategy_cumret=0.02),
    ]
    results = gate.check(_metrics(payload))
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "vix_q3" in criticals[0].details
    assert "-8.00%" in criticals[0].details
    assert criticals[0].gate_name == GATE


def test_critical_lists_every_losing_regime():
    gate = RegimeCoverageGate()
    payload = [
        _row(regime="vix_q1", n_obs=80, strategy_cumret=-0.03),
        _row(regime="vix_q2", n_obs=60, strategy_cumret=0.05),
        _row(regime="vix_q3", n_obs=40, strategy_cumret=-0.08),
        _row(regime="vix_q4", n_obs=20, strategy_cumret=0.02),
    ]
    results = gate.check(_metrics(payload))
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "vix_q1" in criticals[0].details
    assert "vix_q3" in criticals[0].details
    # Both losers listed in regime-order from the payload
    assert criticals[0].details.find("vix_q1") < criticals[0].details.find("vix_q3")


def test_single_regime_winning_emits_only_warning_not_critical():
    """A single-regime strategy that didn't lose money in that regime
    gets the warning but not a critical."""
    gate = RegimeCoverageGate()
    payload = [
        _row(regime="vix_q1", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q2", n_obs=80, strategy_cumret=0.07),
        _row(regime="vix_q3", n_obs=0, strategy_cumret=0.0),
        _row(regime="vix_q4", n_obs=0, strategy_cumret=0.0),
    ]
    results = gate.check(_metrics(payload))
    assert _criticals(results) == []
    warnings = _warnings(results)
    assert len(warnings) == 1


def test_pass_when_multiple_regimes_all_positive():
    gate = RegimeCoverageGate()
    payload = [
        _row(regime="vix_q1", n_obs=80, strategy_cumret=0.10),
        _row(regime="vix_q2", n_obs=60, strategy_cumret=0.05),
        _row(regime="vix_q3", n_obs=40, strategy_cumret=0.02),
        _row(regime="vix_q4", n_obs=20, strategy_cumret=0.01),
    ]
    results = gate.check(_metrics(payload))
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed for r in results)
    assert "clean" in results[0].details


# ---------------------------------------------------------------------------
# Payload-shape tolerance
# ---------------------------------------------------------------------------


def test_handles_payload_entries_with_missing_fields():
    """Malformed entries shouldn't crash the gate; valid entries still
    drive the verdict."""
    gate = RegimeCoverageGate()
    payload = [
        {"regime": "vix_q1"},  # missing n_obs and strategy_cumret
        _row(regime="vix_q2", n_obs=60, strategy_cumret=-0.12),
        {},  # empty dict — no useful fields
    ]
    results = gate.check(_metrics(payload))
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "vix_q2" in criticals[0].details


def test_handles_non_string_regime_label():
    gate = RegimeCoverageGate()
    payload = [
        {"regime": 42, "n_obs": 10, "strategy_cumret": -0.5},
        _row(regime="vix_q3", n_obs=30, strategy_cumret=0.05),
    ]
    results = gate.check(_metrics(payload))
    # The non-string regime is dropped; vix_q3 is positive → single-regime
    # warning, no critical.
    assert _criticals(results) == []
    warnings = _warnings(results)
    assert len(warnings) == 1
