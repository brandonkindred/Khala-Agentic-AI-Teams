"""Unit tests for purged walk-forward fold construction (issue #247, step 1)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from investment_team.execution.walk_forward import (
    DateRange,
    Fold,
    build_purged_walk_forward,
    filter_trades_in_fold_training,
    filter_trades_in_range,
    max_hold_days_from_trades,
)
from investment_team.models import TradeRecord


def _mk_trade(entry: str, exit_: str, hold: int, net: float = 10.0) -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date=entry,
        exit_date=exit_,
        symbol="TST",
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=net,
        net_pnl=net,
        return_pct=net / 1000.0 * 100,
        hold_days=hold,
        outcome="win" if net > 0 else "loss",
        cumulative_pnl=net,
    )


# ---------------------------------------------------------------------------
# Fold construction
# ---------------------------------------------------------------------------


def test_builds_k_equal_folds_covering_span_without_overlap():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=5, embargo_days=0, purge_hold_days=0
    )
    assert len(folds) == 5
    # Test blocks tile the span with no overlap and no gaps on weekdays.
    for i, f in enumerate(folds):
        assert f.fold_index == i
        assert f.test_range.start <= f.test_range.end
        if i > 0:
            prev_end = folds[i - 1].test_range.end
            # next block starts on the next weekday after prev_end
            nxt = prev_end + timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
            assert f.test_range.start == nxt


def test_purge_on_pre_test_segment_removes_hold_day_buffer():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=5, embargo_days=0, purge_hold_days=10
    )
    # Second fold: training is [span_start, test_start - 11 days] + [test_end + 1, span_end]
    second = folds[1]
    test_start = second.test_range.start
    assert len(second.train_ranges) == 2
    pre = second.train_ranges[0]
    assert pre.end == test_start - timedelta(days=11)


def test_embargo_on_post_test_segment_skips_calendar_window():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=5, embargo_days=7, purge_hold_days=0
    )
    second = folds[1]
    test_end = second.test_range.end
    post = second.train_ranges[1]
    assert post.start == test_end + timedelta(days=8)


def test_first_fold_has_no_pre_test_training():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=5, embargo_days=0, purge_hold_days=0
    )
    # Fold 0's test block starts at span_start, so there is no pre-test segment.
    first = folds[0]
    assert len(first.train_ranges) == 1
    assert first.train_ranges[0].start > first.test_range.end


def test_last_fold_has_no_post_test_training():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=5, embargo_days=0, purge_hold_days=0
    )
    last = folds[-1]
    assert len(last.train_ranges) == 1
    assert last.train_ranges[0].end < last.test_range.start


def test_k_folds_1_returns_single_fold_full_span_test():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=1, embargo_days=0, purge_hold_days=0
    )
    assert len(folds) == 1
    f = folds[0]
    assert f.test_range.start == date(2022, 1, 3)
    # 2022-12-30 is a Friday (weekday).
    assert f.test_range.end == date(2022, 12, 30)
    assert f.train_ranges == ()


def test_span_end_before_start_raises():
    with pytest.raises(ValueError, match="precedes"):
        build_purged_walk_forward("2022-12-30", "2022-01-03", k_folds=5)


def test_invalid_k_folds_raises():
    with pytest.raises(ValueError, match="k_folds"):
        build_purged_walk_forward("2022-01-03", "2022-12-30", k_folds=0)


def test_negative_embargo_or_purge_raises():
    with pytest.raises(ValueError, match="embargo_days"):
        build_purged_walk_forward("2022-01-03", "2022-12-30", k_folds=5, embargo_days=-1)
    with pytest.raises(ValueError, match="purge_hold_days"):
        build_purged_walk_forward("2022-01-03", "2022-12-30", k_folds=5, purge_hold_days=-1)


def test_span_too_short_for_k_folds_raises():
    with pytest.raises(ValueError, match="need at least"):
        build_purged_walk_forward("2022-01-03", "2022-01-05", k_folds=10)


# ---------------------------------------------------------------------------
# Trade filtering
# ---------------------------------------------------------------------------


def test_filter_trades_in_range_uses_exit_date():
    trades = [
        _mk_trade("2022-01-03", "2022-02-01", hold=29),  # exit in Jan-Feb
        _mk_trade("2022-06-01", "2022-06-15", hold=14),  # exit in mid-year
        _mk_trade("2022-12-01", "2022-12-29", hold=28),  # exit in Dec
    ]
    result = filter_trades_in_range(trades, "2022-05-01", "2022-07-31")
    assert len(result) == 1
    assert result[0].exit_date == "2022-06-15"


def test_filter_trades_in_range_inclusive_boundaries():
    trades = [_mk_trade("2022-01-03", "2022-06-15", hold=163)]
    assert len(filter_trades_in_range(trades, "2022-06-15", "2022-06-15")) == 1
    assert len(filter_trades_in_range(trades, "2022-06-16", "2022-12-31")) == 0


def test_filter_trades_in_fold_training_excludes_trades_exiting_in_test_or_purge():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=2, embargo_days=0, purge_hold_days=5
    )
    second = folds[1]
    test_start = second.test_range.start
    test_end = second.test_range.end
    far_before = (test_start - timedelta(days=60)).isoformat()
    in_purge = (test_start - timedelta(days=3)).isoformat()  # within 5-day cushion
    inside_test = (test_start + timedelta(days=10)).isoformat()
    trades = [
        _mk_trade("2022-01-10", far_before, hold=30),  # kept (training)
        _mk_trade("2022-01-10", in_purge, hold=30),  # excluded (purge cushion)
        _mk_trade("2022-01-10", inside_test, hold=30),  # excluded (test window)
    ]
    # Past the test window, embargo=0, so anything after test_end is back in training.
    after_test = (test_end + timedelta(days=2)).isoformat()
    trades.append(
        _mk_trade("2022-01-10", after_test, hold=30)
    )  # excluded: not a fold here (last block)
    kept = filter_trades_in_fold_training(trades, second)
    assert [t.exit_date for t in kept] == [far_before]


def test_purge_cushion_excludes_training_trade_whose_label_overlaps_test():
    folds = build_purged_walk_forward(
        "2022-01-03", "2022-12-30", k_folds=2, embargo_days=0, purge_hold_days=30
    )
    second = folds[1]
    test_start = second.test_range.start
    # A trade entered 10 days before test, exiting 20 days into test. Its label
    # horizon straddles the boundary — must be excluded from training.
    entry = (test_start - timedelta(days=10)).isoformat()
    exit_ = (test_start + timedelta(days=20)).isoformat()
    trade = _mk_trade(entry, exit_, hold=30)
    kept = filter_trades_in_fold_training([trade], second)
    assert kept == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_max_hold_days_empty_returns_zero():
    assert max_hold_days_from_trades([]) == 0


def test_max_hold_days_returns_max():
    trades = [
        _mk_trade("2022-01-03", "2022-01-05", hold=2),
        _mk_trade("2022-02-01", "2022-03-15", hold=42),
        _mk_trade("2022-05-01", "2022-05-10", hold=9),
    ]
    assert max_hold_days_from_trades(trades) == 42


def test_date_range_contains():
    r = DateRange(date(2022, 1, 1), date(2022, 12, 31))
    assert r.contains(date(2022, 6, 15))
    assert r.contains(date(2022, 1, 1))  # inclusive
    assert r.contains(date(2022, 12, 31))
    assert not r.contains(date(2021, 12, 31))
    assert not r.contains(date(2023, 1, 1))


def test_fold_is_hashable_and_frozen():
    folds = build_purged_walk_forward("2022-01-03", "2022-12-30", k_folds=3)
    # Frozen dataclass → hashable.
    _ = {f for f in folds}
    with pytest.raises((AttributeError, TypeError)):
        folds[0].fold_index = 99  # type: ignore[misc]


def test_fold_properties_delegate_to_test_range():
    f = Fold(
        fold_index=0,
        train_ranges=(),
        test_range=DateRange(date(2022, 1, 3), date(2022, 6, 30)),
    )
    assert f.test_start == date(2022, 1, 3)
    assert f.test_end == date(2022, 6, 30)


# ---------------------------------------------------------------------------
# Schema round-trip for step 3 extensions (BacktestConfig, BacktestResult)
# ---------------------------------------------------------------------------


def test_backtest_config_accepts_walk_forward_fields():
    from investment_team.models import BacktestConfig

    cfg = BacktestConfig(
        start_date="2022-01-03",
        end_date="2022-12-30",
        walk_forward_enabled=True,
        n_folds=5,
        embargo_days=10,
        min_oos_trades=30,
        dsr_threshold=0.95,
        max_is_oos_degradation_pct=25.0,
        benchmark_composition="60_40",
    )
    assert cfg.n_folds == 5
    assert cfg.walk_forward_enabled is True
    assert cfg.benchmark_composition == "60_40"


def test_backtest_config_defaults_preserve_legacy_callers():
    from investment_team.models import BacktestConfig

    cfg = BacktestConfig(start_date="2022-01-03", end_date="2022-12-30")
    assert cfg.n_folds == 5
    assert cfg.embargo_days == 0
    assert cfg.walk_forward_enabled is True


def test_backtest_config_rejects_invalid_n_folds():
    from investment_team.models import BacktestConfig

    with pytest.raises(Exception):
        BacktestConfig(start_date="2022-01-03", end_date="2022-12-30", n_folds=0)


def test_backtest_result_accepts_walk_forward_diagnostics():
    from investment_team.models import BacktestResult

    r = BacktestResult(
        total_return_pct=12.5,
        annualized_return_pct=10.0,
        volatility_pct=15.0,
        sharpe_ratio=0.9,
        max_drawdown_pct=8.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
        deflated_sharpe=0.72,
        sharpe_ci_low=0.4,
        sharpe_ci_high=1.3,
        is_sharpe=1.2,
        oos_sharpe=0.9,
        is_oos_degradation_pct=25.0,
        oos_trade_count=42,
        n_trials_when_accepted=85,
        acceptance_reason="all four criteria met",
        regime_results=[{"regime": "vix_q1", "sharpe": 1.1, "beat_benchmark": True}],
        fold_results=[{"fold_index": 0, "oos_sharpe": 0.8}],
    )
    assert r.deflated_sharpe == 0.72
    assert r.oos_trade_count == 42
    assert r.regime_results[0]["regime"] == "vix_q1"


def test_backtest_result_legacy_fields_still_required():
    from investment_team.models import BacktestResult

    with pytest.raises(Exception):
        BacktestResult()  # missing required core metrics
