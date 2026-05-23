"""Unit tests for :class:`TradeClusteringGate`."""

from __future__ import annotations

from typing import List

from investment_team.models import TradeRecord
from investment_team.strategy_lab.quality_gates.realism.trade_clustering import (
    GATE,
    TradeClusteringGate,
    _lag1_autocorrelation,
    _ljung_box_q_lag1,
    _max_calendar_quarter_share,
)


def _trade(trade_num: int, entry_date: str) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=entry_date,
        exit_date=entry_date,
        symbol="QQQ",
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=1.0,
        hold_days=1,
        outcome="win",
        cumulative_pnl=10.0 * trade_num,
    )


def _criticals(results):
    return [r for r in results if not r.passed and r.severity == "critical"]


def _warnings(results):
    return [r for r in results if not r.passed and r.severity == "warning"]


# ---------------------------------------------------------------------------
# Sample-size skip
# ---------------------------------------------------------------------------


def test_skips_when_fewer_than_10_trades():
    gate = TradeClusteringGate()
    trades = [_trade(i + 1, f"2024-01-{i + 1:02d}") for i in range(5)]
    results = gate.check(trades)
    assert all(r.passed and r.severity == "info" for r in results)
    assert "skipped" in results[0].details.lower()


def test_skips_when_all_dates_unparseable():
    gate = TradeClusteringGate()
    trades = [_trade(i + 1, "") for i in range(20)]
    results = gate.check(trades)
    assert all(r.passed for r in results)
    assert "skipped" in results[0].details.lower()


# ---------------------------------------------------------------------------
# Critical: quarter dominance + autocorrelation
# ---------------------------------------------------------------------------


def test_critical_when_70_pct_trades_clustered_in_quarter_with_bursty_arrivals():
    """20 trades, 16 fire in 2020-Q2 within a 4-day burst (lag-1 highly
    autocorrelated), 4 spread across 2020-Q3/Q4. Both signals fire →
    critical."""
    trades: List[TradeRecord] = []
    # 16 trades in April 2020, days 1, 2, 3, 4, 5, ... (consecutive — high
    # autocorrelation of inter-arrival times because all gaps are 1).
    for i in range(16):
        trades.append(_trade(i + 1, f"2020-04-{i + 1:02d}"))
    # 4 trades scattered across Q3/Q4 with varying gaps.
    trades.append(_trade(17, "2020-07-15"))
    trades.append(_trade(18, "2020-08-22"))
    trades.append(_trade(19, "2020-10-05"))
    trades.append(_trade(20, "2020-12-18"))

    gate = TradeClusteringGate()
    results = gate.check(trades)
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "2020-Q2" in criticals[0].details
    assert criticals[0].gate_name == GATE


# ---------------------------------------------------------------------------
# Warning paths
# ---------------------------------------------------------------------------


def test_warning_when_quarter_dominates_but_no_autocorrelation():
    """All trades in 2020-Q2 with uniform 5-day spacing (constant
    inter-arrival → lag-1 autocorrelation undefined). Quarter signal
    fires; autocorr signal does not → warning, not critical."""
    from datetime import date, timedelta

    trades: List[TradeRecord] = []
    start = date(2020, 4, 5)
    for i in range(14):
        d = start + timedelta(days=i * 5)
        trades.append(_trade(i + 1, d.isoformat()))

    gate = TradeClusteringGate()
    results = gate.check(trades)
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "2020-Q2" in warnings[0].details
    assert "concentrate" in warnings[0].details.lower()
    assert _criticals(results) == []


def test_warning_when_bursts_present_but_no_dominant_quarter():
    """40 trades arriving in clear bursts (lag-1 autocorrelation positive)
    but spread across multiple quarters so no single quarter dominates."""
    trades: List[TradeRecord] = []
    # Bursts at the start of each month, but spread across many months.
    # Trade dates: 4 trades on consecutive days each month for 10 months.
    n = 1
    months = [
        "2023-01",
        "2023-03",
        "2023-05",
        "2023-07",
        "2023-09",
        "2023-11",
        "2024-01",
        "2024-03",
        "2024-05",
        "2024-07",
    ]
    for month in months:
        for day in range(1, 5):
            trades.append(_trade(n, f"{month}-{day:02d}"))
            n += 1

    gate = TradeClusteringGate()
    results = gate.check(trades)
    # No quarter dominates (each month has 4 trades, quarters split across
    # 2-3 months). Inter-arrival times alternate small/large → strong lag-1
    # autocorrelation → warning.
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "burst" in warnings[0].details.lower()
    assert _criticals(results) == []


# ---------------------------------------------------------------------------
# Clean pass
# ---------------------------------------------------------------------------


def test_passes_when_trades_spread_uniformly_across_window():
    """30 trades evenly spaced every 30 days across two years — no quarter
    dominates, inter-arrival times are constant (denominator zero → no
    autocorrelation signal)."""
    trades: List[TradeRecord] = []
    from datetime import date, timedelta

    start = date(2023, 1, 5)
    for i in range(30):
        d = start + timedelta(days=i * 24)  # ~24-day spacing
        trades.append(_trade(i + 1, d.isoformat()))

    gate = TradeClusteringGate()
    results = gate.check(trades)
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed for r in results)
    assert "well-spread" in results[0].details


# ---------------------------------------------------------------------------
# Helper coverage
# ---------------------------------------------------------------------------


def test_max_calendar_quarter_share_handles_empty():
    share, label = _max_calendar_quarter_share([])
    assert share == 0.0
    assert label is None


def test_lag1_autocorrelation_returns_none_for_constant_intervals():
    """A constant-spacing series has zero variance in intervals → lag-1
    autocorrelation is undefined; helper returns None."""
    from datetime import date, timedelta

    dates = [date(2024, 1, 1) + timedelta(days=i * 7) for i in range(10)]
    assert _lag1_autocorrelation(dates) is None


def test_lag1_autocorrelation_returns_none_for_short_series():
    from datetime import date

    assert _lag1_autocorrelation([date(2024, 1, 1)]) is None
    assert _lag1_autocorrelation([date(2024, 1, 1), date(2024, 1, 8)]) is None


def test_ljung_box_q_lag1_returns_none_for_none_input():
    assert _ljung_box_q_lag1(None, 10) is None


def test_ljung_box_q_lag1_returns_none_for_small_n():
    assert _ljung_box_q_lag1(0.5, 1) is None
