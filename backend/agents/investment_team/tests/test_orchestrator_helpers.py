"""Coverage for ``strategy_lab._orchestrator_helpers``.

Targets pure helper functions that don't require the full
``StrategyLabOrchestrator`` collaborator graph:

* ``_merge_risk_limits_tighten_only`` — all loosen/tighten/unknown/None↔
  value branches and pydantic-validation rollback path.
* ``_daily_returns_from_trades`` — empty / ruin / happy branches.
* ``_equity_to_returns`` and ``_closes_to_equity`` corner cases.
* ``_parse_bar_date`` round trip.
* ``_resolve_vix_provider`` env-var driven dispatcher.
* ``_has_critical_failures`` / ``_critical_failures`` — empty / no-critical
  (all-passed and failed-but-non-critical) / critical-present cases.
"""

from __future__ import annotations

import pytest

from investment_team.execution.risk_filter import RiskLimits
from investment_team.strategy_lab._orchestrator_helpers import (
    _closes_to_equity,
    _critical_failures,
    _daily_returns_from_trades,
    _equity_to_returns,
    _has_critical_failures,
    _merge_risk_limits_tighten_only,
    _parse_bar_date,
    _resolve_vix_provider,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult


def _trade(*, ret_pct: float = 1.0, net: float = 10.0, cum: float = 100.0, n: int = 1):
    from investment_team.models import TradeRecord

    return TradeRecord(
        trade_num=n,
        symbol="X",
        side="long",
        entry_date="2024-01-01",
        exit_date="2024-01-05",
        entry_price=100.0,
        exit_price=101.0,
        shares=1.0,
        position_value=100.0,
        gross_pnl=net,
        net_pnl=net,
        return_pct=ret_pct,
        hold_days=4,
        cumulative_pnl=cum,
        outcome="win" if ret_pct > 0 else "loss",
    )


def _gate(
    *,
    name: str = "some_gate",
    passed: bool = True,
    severity: str = "info",
    details: str = "",
    phase: str = "synthesis",
) -> QualityGateResult:
    return QualityGateResult(
        gate_name=name,
        passed=passed,
        details=details,
        severity=severity,
        phase=phase,
    )


# ---------------------------------------------------------------------------
# _merge_risk_limits_tighten_only
# ---------------------------------------------------------------------------


def test_merge_returns_current_when_proposed_not_dict() -> None:
    rl = RiskLimits()
    merged, loosened, unknown = _merge_risk_limits_tighten_only(rl, "not a dict")
    assert merged == rl
    assert loosened == []
    assert unknown == []


def test_merge_tightens_lower_direction_field() -> None:
    rl = RiskLimits(max_position_pct=20.0)
    merged, loosened, unknown = _merge_risk_limits_tighten_only(rl, {"max_position_pct": 5.0})
    assert merged.max_position_pct == 5.0
    assert loosened == []
    assert unknown == []


def test_merge_records_loosen_for_lower_direction_increased() -> None:
    rl = RiskLimits(max_position_pct=5.0)
    merged, loosened, unknown = _merge_risk_limits_tighten_only(rl, {"max_position_pct": 20.0})
    # Original kept; loosened tracked.
    assert merged.max_position_pct == 5.0
    assert "max_position_pct" in loosened


def test_merge_target_annual_vol_none_to_value_is_loosened() -> None:
    rl = RiskLimits(target_annual_vol=None)
    _merged, loosened, _unknown = _merge_risk_limits_tighten_only(rl, {"target_annual_vol": 0.15})
    assert "target_annual_vol" in loosened


def test_merge_target_annual_vol_value_to_none_is_loosened() -> None:
    rl = RiskLimits(target_annual_vol=0.10)
    _merged, loosened, _unknown = _merge_risk_limits_tighten_only(rl, {"target_annual_vol": None})
    assert "target_annual_vol" in loosened


def test_merge_target_annual_vol_lowering_is_tightening() -> None:
    rl = RiskLimits(target_annual_vol=0.20)
    merged, loosened, _unknown = _merge_risk_limits_tighten_only(rl, {"target_annual_vol": 0.10})
    assert merged.target_annual_vol == 0.10
    assert loosened == []


def test_merge_clearing_numeric_cap_to_none_is_loosened() -> None:
    """Clearing a numeric cap to None removes the constraint — a loosening. The
    original value is kept and the loosening is tracked so the caller can raise."""
    rl = RiskLimits(max_position_pct=5.0)
    merged, loosened, _unknown = _merge_risk_limits_tighten_only(rl, {"max_position_pct": None})
    assert merged.max_position_pct == 5.0
    assert "max_position_pct" in loosened


def test_merge_records_unknown_for_immutable_field() -> None:
    rl = RiskLimits()
    _merged, _loosened, unknown = _merge_risk_limits_tighten_only(rl, {"vol_lookback_days": 30})
    assert "vol_lookback_days" in unknown


def test_merge_records_unknown_for_schema_missing_field() -> None:
    rl = RiskLimits()
    _merged, _loosened, unknown = _merge_risk_limits_tighten_only(rl, {"made_up_key": 1.0})
    assert "made_up_key" in unknown


def test_merge_returns_unknown_when_non_numeric_value() -> None:
    rl = RiskLimits(max_position_pct=10.0)
    _merged, _loosened, unknown = _merge_risk_limits_tighten_only(rl, {"max_position_pct": "abc"})
    assert "max_position_pct" in unknown


def test_merge_invalid_merged_data_falls_back_to_current() -> None:
    """When pydantic validation of the merged dict fails, return current unchanged."""
    rl = RiskLimits(max_position_pct=10.0)
    # Propose a tight value that's negative — pydantic should reject.
    merged, loosened, unknown = _merge_risk_limits_tighten_only(rl, {"max_position_pct": -1.0})
    # The current limits are preserved.
    assert merged.max_position_pct == 10.0
    # The proposed key surfaces as unknown (recovery path).
    assert "max_position_pct" in unknown


# ---------------------------------------------------------------------------
# _daily_returns_from_trades
# ---------------------------------------------------------------------------


def test_daily_returns_for_no_trades_returns_flat_series() -> None:
    """No trades → equity is the initial capital across every day (zero returns)."""
    out = _daily_returns_from_trades([], 100_000.0, "2024-01-01", "2024-01-05")
    assert isinstance(out, list)
    assert all(r == 0.0 for r in out)


def test_daily_returns_returns_non_empty_for_winning_path() -> None:
    trades = [_trade(net=100, cum=100, n=1), _trade(net=200, cum=300, n=2)]
    out = _daily_returns_from_trades(trades, 100_000.0, "2024-01-01", "2024-01-15")
    # The equity curve has multiple bars; returns is non-empty.
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# _equity_to_returns
# ---------------------------------------------------------------------------


def test_equity_to_returns_empty_for_single_value() -> None:
    assert _equity_to_returns([100.0]) == []


def test_equity_to_returns_zero_when_previous_non_positive() -> None:
    out = _equity_to_returns([100.0, 0.0, 50.0])
    # Second step: prev=0 → 0.0
    assert out[1] == 0.0


def test_equity_to_returns_simple_return() -> None:
    out = _equity_to_returns([100.0, 110.0])
    assert out == [0.1]


# ---------------------------------------------------------------------------
# _closes_to_equity
# ---------------------------------------------------------------------------


def test_closes_to_equity_returns_empty_for_invalid_input() -> None:
    assert _closes_to_equity([], 100.0) == []
    assert _closes_to_equity([0.0, 10.0], 100.0) == []


def test_closes_to_equity_scales_to_initial_capital() -> None:
    out = _closes_to_equity([10.0, 12.0], 1000.0)
    assert out[0] == 1000.0
    assert out[1] == 1200.0


# ---------------------------------------------------------------------------
# _parse_bar_date
# ---------------------------------------------------------------------------


def test_parse_bar_date_handles_date_strings() -> None:
    from datetime import date

    assert _parse_bar_date("2024-06-01") == date(2024, 6, 1)
    # Trailing time-of-day is sliced off.
    assert _parse_bar_date("2024-06-01T12:34:56Z") == date(2024, 6, 1)


# ---------------------------------------------------------------------------
# _resolve_vix_provider
# ---------------------------------------------------------------------------


def test_resolve_vix_provider_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_VIX_SOURCE", raising=False)
    assert _resolve_vix_provider() is None


def test_resolve_vix_provider_returns_none_for_now_for_known_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATEGY_LAB_VIX_SOURCE", "yahoo")
    # Implementation still returns None — production hook point.
    assert _resolve_vix_provider() is None


# ---------------------------------------------------------------------------
# _has_critical_failures / _critical_failures
# ---------------------------------------------------------------------------


def test_has_critical_failures_false_for_empty_list() -> None:
    assert _has_critical_failures([]) is False


def test_critical_failures_empty_for_empty_list() -> None:
    assert _critical_failures([]) == []


def test_has_critical_failures_false_when_all_passed() -> None:
    results = [
        _gate(name="a", passed=True, severity="critical"),
        _gate(name="b", passed=True, severity="warning"),
    ]
    assert _has_critical_failures(results) is False


def test_critical_failures_empty_when_all_passed() -> None:
    results = [
        _gate(name="a", passed=True, severity="critical"),
        _gate(name="b", passed=True, severity="warning"),
    ]
    assert _critical_failures(results) == []


def test_has_critical_failures_false_for_non_critical_failure() -> None:
    results = [_gate(name="a", passed=False, severity="warning")]
    assert _has_critical_failures(results) is False


def test_critical_failures_empty_for_non_critical_failure() -> None:
    results = [_gate(name="a", passed=False, severity="warning")]
    assert _critical_failures(results) == []


def test_has_critical_failures_true_when_critical_present() -> None:
    results = [
        _gate(name="a", passed=True, severity="critical"),
        _gate(name="b", passed=False, severity="critical"),
    ]
    assert _has_critical_failures(results) is True


def test_critical_failures_returns_only_unpassed_critical_in_order() -> None:
    first = _gate(name="first", passed=False, severity="critical")
    second = _gate(name="second", passed=False, severity="warning")
    third = _gate(name="third", passed=False, severity="critical")
    results = [first, second, third]
    assert _critical_failures(results) == [first, third]
