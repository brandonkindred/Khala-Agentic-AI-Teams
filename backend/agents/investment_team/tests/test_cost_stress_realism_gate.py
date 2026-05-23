"""Unit tests for :class:`CostStressRealismGate`.

Covers the four result paths:

* ``cost_stress_results`` missing on a winning-candidate run → critical
  (mandatory cost-stress).
* ``cost_stress_results`` missing on a legacy single-window run
  (``walk_forward_enabled=False``, ``cost_stress=False``) → info.
* ``cost_stress_results`` present with 2.0× Sharpe < 0 → critical.
* ``cost_stress_results`` present with 2.0× Sharpe ≥ 0 → info pass.

Plus the soft-warning paths for missing 2.0× row and missing Sharpe field.
"""

from __future__ import annotations

from typing import Any, List

from investment_team.execution.cost_stress import CostStressReport, CostStressRow
from investment_team.models import BacktestConfig, BacktestResult
from investment_team.strategy_lab.quality_gates.cost_stress_realism import (
    GATE,
    CostStressRealismGate,
)


def _config(*, walk_forward_enabled: bool = True, cost_stress: bool = True) -> BacktestConfig:
    return BacktestConfig(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
        walk_forward_enabled=walk_forward_enabled,
        cost_stress=cost_stress,
    )


def _metrics(*, cost_stress_results: Any = None) -> BacktestResult:
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
        cost_stress_results=cost_stress_results,
    )


def _payload_with_sharpe_at_2x(sharpe: float) -> List[dict]:
    """A typical cost-stress payload with three rows; only the 2.0×
    row's Sharpe is parameterised."""
    report = CostStressReport(
        rows=[
            CostStressRow(
                multiplier=1.0,
                sharpe_ratio=1.4,
                annualized_return_pct=15.0,
                max_drawdown_pct=5.0,
                trade_count=120,
            ),
            CostStressRow(
                multiplier=2.0,
                sharpe_ratio=sharpe,
                annualized_return_pct=8.0,
                max_drawdown_pct=7.0,
                trade_count=120,
            ),
            CostStressRow(
                multiplier=3.0,
                sharpe_ratio=sharpe - 0.5,
                annualized_return_pct=2.0,
                max_drawdown_pct=10.0,
                trade_count=120,
            ),
        ]
    )
    return report.to_payload()


def _criticals(results):
    return [r for r in results if not r.passed and r.severity == "critical"]


def _warnings(results):
    return [r for r in results if not r.passed and r.severity == "warning"]


# ---------------------------------------------------------------------------
# Missing cost_stress_results
# ---------------------------------------------------------------------------


def test_critical_when_results_missing_but_cost_stress_was_requested():
    """When the operator opted into cost-stress (``cost_stress=True``)
    but the engine returned no payload, the sweep was silently dropped —
    surface that as critical rather than swallow it."""
    gate = CostStressRealismGate()
    config = _config(walk_forward_enabled=True, cost_stress=True)
    results = gate.check(_metrics(cost_stress_results=None), config)

    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "engine appears to have dropped the sweep" in criticals[0].details
    assert criticals[0].gate_name == GATE
    assert criticals[0].phase == "verification"


def test_info_when_results_missing_and_cost_stress_not_requested():
    """Hand-built configs that didn't enable cost-stress don't get vetoed
    by the gate. Enforcement of "mandatory cost-stress on winning-candidate
    runs" lives at the production entrypoint
    (``_strategy_lab_worker`` force-enables the flag) — the gate just
    verifies what the config requested.

    This is the regression guard: under the prior logic, any
    ``walk_forward_enabled=True`` config without cost-stress would have
    fired critical and broken every Strategy Lab run by default.
    """
    gate = CostStressRealismGate()
    config = _config(walk_forward_enabled=True, cost_stress=False)
    results = gate.check(_metrics(cost_stress_results=None), config)

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_info_when_results_missing_on_legacy_single_window_path():
    """``walk_forward_enabled=False`` AND ``cost_stress=False`` — the
    realism cycle has nothing to enforce; the acceptance gate's own
    legacy criteria speak for this path."""
    gate = CostStressRealismGate()
    config = _config(walk_forward_enabled=False, cost_stress=False)
    results = gate.check(_metrics(cost_stress_results=None), config)

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_critical_when_results_is_empty_list_and_cost_stress_requested():
    """A persisted empty list with ``cost_stress=True`` is just as bad as
    None — the sweep was requested but produced no rows."""
    gate = CostStressRealismGate()
    config = _config(walk_forward_enabled=True, cost_stress=True)
    results = gate.check(_metrics(cost_stress_results=[]), config)

    criticals = _criticals(results)
    assert len(criticals) == 1


# ---------------------------------------------------------------------------
# Results present
# ---------------------------------------------------------------------------


def test_critical_when_2x_sharpe_below_zero():
    gate = CostStressRealismGate()
    payload = _payload_with_sharpe_at_2x(-0.2)
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "2×" in criticals[0].details
    assert "-0.20" in criticals[0].details


def test_info_when_2x_sharpe_at_zero():
    """Boundary — exactly 0 passes (``>= 0`` per the gate's contract)."""
    gate = CostStressRealismGate()
    payload = _payload_with_sharpe_at_2x(0.0)
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_info_when_2x_sharpe_positive():
    gate = CostStressRealismGate()
    payload = _payload_with_sharpe_at_2x(0.6)
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed for r in results)
    info = next(r for r in results if r.passed)
    assert "Sharpe 0.60" in info.details


def test_warning_when_2x_row_absent_from_present_payload():
    """Operator configured multipliers without 2.0× — gate can't evaluate
    the floor, so warn rather than veto or silently pass."""
    gate = CostStressRealismGate()
    payload = [
        {
            "multiplier": 1.0,
            "sharpe_ratio": 1.4,
            "annualized_return_pct": 15.0,
            "max_drawdown_pct": 5.0,
            "trade_count": 100,
        },
        {
            "multiplier": 1.5,
            "sharpe_ratio": 1.0,
            "annualized_return_pct": 11.0,
            "max_drawdown_pct": 6.0,
            "trade_count": 100,
        },
        {
            "multiplier": 3.0,
            "sharpe_ratio": 0.5,
            "annualized_return_pct": 6.0,
            "max_drawdown_pct": 8.0,
            "trade_count": 100,
        },
    ]
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "2×" in warnings[0].details
    assert _criticals(results) == []


def test_warning_when_2x_row_present_but_sharpe_field_missing():
    gate = CostStressRealismGate()
    payload = [
        {
            "multiplier": 2.0,
            "annualized_return_pct": 8.0,
            "max_drawdown_pct": 7.0,
            "trade_count": 100,
        },
    ]
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "sharpe_ratio is missing" in warnings[0].details


# ---------------------------------------------------------------------------
# Format/source tolerance
# ---------------------------------------------------------------------------


def test_accepts_floating_point_multiplier_within_tolerance():
    """Stored multiplier of 2.0000001 (round-trip drift) still resolves."""
    gate = CostStressRealismGate()
    payload = [
        {
            "multiplier": 2.0000001,
            "sharpe_ratio": 0.4,
            "annualized_return_pct": 5.0,
            "max_drawdown_pct": 7.0,
            "trade_count": 100,
        },
    ]
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    assert _criticals(results) == []
    assert _warnings(results) == []


def test_skips_unparseable_multiplier_entries():
    """A row with a non-numeric multiplier shouldn't crash the gate; the
    rule should keep scanning for a usable 2.0× row."""
    gate = CostStressRealismGate()
    payload = [
        {"multiplier": "bad", "sharpe_ratio": 1.0},
        {
            "multiplier": 2.0,
            "sharpe_ratio": 0.5,
            "annualized_return_pct": 7.0,
            "max_drawdown_pct": 6.0,
            "trade_count": 100,
        },
    ]
    results = gate.check(_metrics(cost_stress_results=payload), _config())

    assert _criticals(results) == []
    assert _warnings(results) == []
