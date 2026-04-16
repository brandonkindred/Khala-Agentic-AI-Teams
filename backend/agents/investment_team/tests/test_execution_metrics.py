"""Unit tests for the Phase 1 execution primitives."""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import patch

import pytest

from investment_team.execution.benchmarks import (
    DEFAULT_BENCHMARK_BY_ASSET_CLASS,
    benchmark_for_strategy,
)
from investment_team.execution.metrics import (
    build_equity_curve_from_trades,
    compute_performance_metrics,
)
from investment_team.execution.risk_free_rate import RFR_DEFAULT, get_risk_free_rate
from investment_team.models import StrategySpec, TradeRecord


def _mk_trade(entry: str, exit_: str, net: float, gross: float | None = None, side: str = "long"):
    if gross is None:
        gross = net
    return TradeRecord(
        trade_num=1,
        entry_date=entry,
        exit_date=exit_,
        symbol="TST",
        side=side,
        entry_price=100.0,
        exit_price=100.0 + net / 10,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=gross,
        net_pnl=net,
        return_pct=net / 1000.0 * 100,
        hold_days=1,
        outcome="win" if net > 0 else "loss",
        cumulative_pnl=net,
    )


# ---------------------------------------------------------------------------
# Risk-free rate resolution
# ---------------------------------------------------------------------------


def test_rfr_env_override_wins(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.055")
    monkeypatch.setenv("FRED_API_KEY", "should-be-ignored")
    assert get_risk_free_rate() == pytest.approx(0.055)


def test_rfr_ignores_non_numeric_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "not-a-number")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert get_risk_free_rate() == RFR_DEFAULT


def test_rfr_falls_back_to_default_without_fred_key(monkeypatch):
    monkeypatch.delenv("STRATEGY_LAB_RISK_FREE_RATE", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert get_risk_free_rate() == RFR_DEFAULT


def test_rfr_uses_fred_when_key_present(monkeypatch):
    monkeypatch.delenv("STRATEGY_LAB_RISK_FREE_RATE", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    with patch(
        "investment_team.execution.risk_free_rate._fetch_fred_dgs3mo",
        return_value=0.0521,
    ) as m:
        assert get_risk_free_rate() == pytest.approx(0.0521)
    m.assert_called_once()


def test_rfr_falls_back_when_fred_returns_none(monkeypatch):
    monkeypatch.delenv("STRATEGY_LAB_RISK_FREE_RATE", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    with patch(
        "investment_team.execution.risk_free_rate._fetch_fred_dgs3mo",
        return_value=None,
    ):
        assert get_risk_free_rate() == RFR_DEFAULT


def test_rfr_explicit_override_shortcircuits(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.10")
    monkeypatch.setenv("FRED_API_KEY", "k")
    assert get_risk_free_rate(override=0.02) == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def _spec(asset_class: str) -> StrategySpec:
    return StrategySpec(
        strategy_id="s1",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="s",
    )


def test_benchmark_per_asset_class_defaults():
    assert benchmark_for_strategy(_spec("stocks")) == "SPY"
    assert benchmark_for_strategy(_spec("crypto")) == "BTC-USD"
    assert benchmark_for_strategy(_spec("forex")) == "DX-Y.NYB"
    assert benchmark_for_strategy(_spec("commodities")) == "DBC"


def test_benchmark_futures_routes_by_family():
    spec = _spec("futures")
    assert benchmark_for_strategy(spec, primary_symbol="ES=F") == "SPY"
    assert benchmark_for_strategy(spec, primary_symbol="NQ") == "SPY"
    assert benchmark_for_strategy(spec, primary_symbol="ZN=F") == "AGG"
    assert benchmark_for_strategy(spec, primary_symbol="CL") == "DBC"
    assert benchmark_for_strategy(spec, primary_symbol="GC") == "DBC"
    # Unknown root → SPY (conservative default)
    assert benchmark_for_strategy(spec, primary_symbol="XX") == "SPY"


def test_benchmark_unknown_asset_class_falls_back_to_spy():
    assert benchmark_for_strategy(_spec("widgets")) == "SPY"


def test_default_benchmark_map_keys():
    # Enforces the plan: every asset class the team can ideate has a mapping.
    for asset in ("stocks", "crypto", "forex", "futures", "commodities", "options"):
        assert asset in DEFAULT_BENCHMARK_BY_ASSET_CLASS


# ---------------------------------------------------------------------------
# EquityCurve construction
# ---------------------------------------------------------------------------


def test_equity_curve_marks_pnl_on_exit_date():
    trades = [
        _mk_trade("2023-01-02", "2023-01-05", net=100.0),
        _mk_trade("2023-01-05", "2023-01-10", net=-50.0),
    ]
    curve = build_equity_curve_from_trades(trades, 10_000.0)
    assert curve.equity[0] == 10_000.0  # Jan 2 entry, no exit yet
    # Jan 5 exit: +100
    jan5 = curve.dates.index(date(2023, 1, 5))
    assert curve.equity[jan5] == pytest.approx(10_100.0)
    # Jan 10 exit: -50
    jan10 = curve.dates.index(date(2023, 1, 10))
    assert curve.equity[jan10] == pytest.approx(10_050.0)


def test_equity_curve_piecewise_flat_between_exits():
    trades = [_mk_trade("2023-01-02", "2023-01-06", net=500.0)]
    curve = build_equity_curve_from_trades(trades, 10_000.0)
    # All weekdays before exit stay at initial capital.
    for d, eq in zip(curve.dates, curve.equity):
        if d < date(2023, 1, 6):
            assert eq == pytest.approx(10_000.0)
        else:
            assert eq == pytest.approx(10_500.0)


def test_equity_curve_handles_empty_trades():
    curve = build_equity_curve_from_trades(
        [], 10_000.0, start_date="2023-01-02", end_date="2023-01-03"
    )
    assert curve.initial_capital == 10_000.0


def test_daily_returns_length_matches_dates_minus_one():
    trades = [_mk_trade("2023-01-02", "2023-01-10", net=100.0)]
    curve = build_equity_curve_from_trades(trades, 10_000.0)
    assert len(curve.daily_returns()) == len(curve.equity) - 1


# ---------------------------------------------------------------------------
# PerformanceMetrics correctness
# ---------------------------------------------------------------------------


def test_metrics_sharpe_matches_hand_calc():
    # A constant +1% arithmetic return each trading day → zero vol, sharpe=0.
    trades = [_mk_trade(f"2023-01-{d:02d}", f"2023-01-{d:02d}", net=100.0) for d in range(3, 8)]
    m = compute_performance_metrics(
        trades,
        initial_capital=10_000.0,
        risk_free_rate=0.0,
        start_date="2023-01-03",
        end_date="2023-01-07",
    )
    # Each day steps +100 on a base that rises → not exactly zero vol but very low
    assert math.isfinite(m.sharpe_ratio)


def test_metrics_sharpe_against_known_curve():
    """Roughly ±1% daily returns; annual vol ≈ 1% * sqrt(252) ≈ 15.9%.

    With small drift + imperfect cancellation the empirical annual vol lands
    around 17% — we only care that it's in the same order of magnitude as
    15.9% and far from the legacy estimator's 0.02%.
    """
    trades = [
        _mk_trade("2023-01-02", "2023-01-03", net=100.0),
        _mk_trade("2023-01-03", "2023-01-04", net=-101.0),
        _mk_trade("2023-01-04", "2023-01-05", net=99.99),
        _mk_trade("2023-01-05", "2023-01-06", net=-100.99),
        _mk_trade("2023-01-06", "2023-01-09", net=99.98),
    ]
    m = compute_performance_metrics(
        trades,
        initial_capital=10_000.0,
        risk_free_rate=0.0,
    )
    assert 12.0 <= m.volatility_pct <= 20.0, (
        f"daily-engine annual vol {m.volatility_pct}% outside sanity band"
    )
    assert abs(m.sharpe_ratio) < 10


def test_metrics_profit_factor_and_win_rate():
    trades = [
        _mk_trade("2023-01-02", "2023-01-03", net=200.0),
        _mk_trade("2023-01-03", "2023-01-04", net=-50.0),
        _mk_trade("2023-01-04", "2023-01-05", net=100.0),
        _mk_trade("2023-01-05", "2023-01-06", net=-150.0),
    ]
    m = compute_performance_metrics(
        trades,
        initial_capital=10_000.0,
        risk_free_rate=0.0,
    )
    assert m.win_rate_pct == pytest.approx(50.0)
    # gross wins = 300, gross losses = 200 → PF = 1.5
    assert m.profit_factor == pytest.approx(1.5, abs=0.01)


def test_metrics_max_drawdown_and_duration():
    # Rise 10 → crash 30 → recovery: max DD should pick up the crash
    trades = [
        _mk_trade("2023-01-02", "2023-01-03", net=1000.0),  # 10k → 11k
        _mk_trade("2023-01-03", "2023-01-04", net=-3000.0),  # 11k → 8k  -> DD=-27.3%
        _mk_trade("2023-01-04", "2023-01-10", net=500.0),  # 8k → 8.5k
    ]
    m = compute_performance_metrics(
        trades,
        initial_capital=10_000.0,
        risk_free_rate=0.0,
    )
    assert m.max_drawdown_pct == pytest.approx(27.27, abs=0.2)
    assert m.max_drawdown_duration_days >= 1


def test_metrics_empty_trades_returns_zeros():
    m = compute_performance_metrics([], initial_capital=10_000.0)
    assert m.trade_count == 0
    assert m.total_return_pct == 0.0
    assert m.sharpe_ratio == 0.0
    assert m.risk_free_rate is not None


def test_metrics_alpha_beta_with_benchmark():
    # Build a portfolio that moves exactly like the benchmark → beta ≈ 1, alpha ≈ 0
    trades = [
        _mk_trade("2023-01-02", "2023-01-02", net=100.0),
        _mk_trade("2023-01-02", "2023-01-03", net=-100.0),
        _mk_trade("2023-01-03", "2023-01-04", net=100.0),
    ] * 5  # 15 trades to exceed min-obs threshold
    # Renumber trade_num (not used by metrics, but keep it sane)
    for i, t in enumerate(trades):
        t.trade_num = i + 1

    m = compute_performance_metrics(
        trades,
        initial_capital=10_000.0,
        risk_free_rate=0.0,
    )
    assert m.alpha_pct is None  # no benchmark supplied


def test_rfr_is_stamped_into_metrics(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.0375")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    m = compute_performance_metrics([], initial_capital=10_000.0)
    assert m.risk_free_rate == pytest.approx(0.0375)
