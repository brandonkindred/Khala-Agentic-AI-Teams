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


def test_warning_when_arrival_rate_drifts_without_dominant_quarter():
    """20 trades whose inter-arrival times decrease monotonically — the
    arrival rate accelerates over the ~2.5-year window. Consecutive
    intervals are similar in magnitude → positive lag-1 autocorrelation
    → autocorr signal fires. No single calendar quarter holds more than
    ~30% of the trades, so the quarter signal does not → verdict is
    warning, not critical."""
    from datetime import date, timedelta

    intervals_days = [
        120,
        110,
        100,
        90,
        80,
        70,
        60,
        50,
        40,
        30,
        25,
        20,
        15,
        12,
        10,
        8,
        6,
        4,
        3,
    ]
    trades: List[TradeRecord] = []
    cursor = date(2023, 1, 1)
    trades.append(_trade(1, cursor.isoformat()))
    for i, iv in enumerate(intervals_days):
        cursor = cursor + timedelta(days=iv)
        trades.append(_trade(i + 2, cursor.isoformat()))

    gate = TradeClusteringGate()
    results = gate.check(trades)
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "burst" in warnings[0].details.lower()
    assert _criticals(results) == []


def test_does_not_flag_negative_lag1_autocorrelation_as_burst():
    """Alternating burst-gap-burst-gap arrangements produce NEGATIVE
    lag-1 autocorrelation. The Ljung-Box ``Q ∝ rho²`` would still cross
    the 3.84 critical value, but labelling anti-bursty patterns as
    bursts is backwards — burst attribution must require positive
    lag-1 autocorrelation.

    Fixture: 4 dates on consecutive days each month, 10 alternating
    months. Intervals = [1, 1, 1, ~58, 1, 1, 1, ~58, ...] — strongly
    negative lag-1 autocorrelation (small deviations alternate with
    large deviations) and no quarter dominance. Must be info-level.
    """
    trades: List[TradeRecord] = []
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
    assert _warnings(results) == []
    assert _criticals(results) == []
    assert all(r.passed for r in results)


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
