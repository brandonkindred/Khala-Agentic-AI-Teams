"""Realism extensions of ``BacktestAnomalyDetector``.

Covers two additions:

* Frequency-aware trade-count adequacy — scales the floor by
  ``window_days / expected_hold_days`` instead of the legacy hard
  ``< 5 trades`` minimum.
* Look-ahead pattern in returns — flags trades whose return sign agrees
  suspiciously well with the entry bar's intrabar direction, a heuristic
  for look-ahead bias that AST checks miss.

The legacy gates (Sharpe, win-rate, profit factor, hold time, ...) are
covered by ``test_backtest_anomaly_dsr_aware.py`` and
``test_backtest_anomaly_zero_trade_diagnostics.py``; this file only
exercises the realism additions.
"""

from __future__ import annotations

from typing import List

from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestResult, TradeRecord
from investment_team.strategy_lab.quality_gates.backtest_anomaly import (
    BacktestAnomalyDetector,
)


def _baseline_metrics() -> BacktestResult:
    """Realistic, anomaly-clean metrics so only the gate under test fires."""
    return BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=8.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=9.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _trade(
    *,
    trade_num: int,
    entry_date: str,
    exit_date: str,
    symbol: str = "QQQ",
    return_pct: float = 1.2,
    hold_days: int = 10,
    side: str = "long",
) -> TradeRecord:
    net = 12.0 if return_pct > 0 else -8.0
    return TradeRecord(
        trade_num=trade_num,
        entry_date=entry_date,
        exit_date=exit_date,
        symbol=symbol,
        side=side,
        entry_price=100.0,
        exit_price=100.0 + net / 10.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=net,
        net_pnl=net,
        return_pct=return_pct,
        hold_days=hold_days,
        outcome="win" if return_pct > 0 else "loss",
        cumulative_pnl=net,
    )


def _trade_count_results(results):
    return [
        r
        for r in results
        if "expected" in r.details and ("trades" in r.details or "Observed" in r.details)
    ]


# ---------------------------------------------------------------------------
# Trade-count adequacy
# ---------------------------------------------------------------------------


def test_trade_count_adequacy_critical_when_under_25_pct_of_expected():
    """5-year daily window with 2-week observed holds expects ~130 trades; 8
    realised trades is ~6% of expected → critical."""
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2020-{(i % 12) + 1:02d}-15",
            exit_date=f"2020-{(i % 12) + 1:02d}-28",
            hold_days=14,
        )
        for i in range(8)
    ]
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2020-01-01",
        end_date="2025-01-01",
        timeframe="1d",
    )
    tc = _trade_count_results(results)
    assert len(tc) == 1
    assert tc[0].severity == "critical"
    assert "below the 25%" in tc[0].details or "25%" in tc[0].details


def test_trade_count_adequacy_warning_between_25_and_50_pct():
    """About 40 trades against ~130 expected is ~30% → warning, not critical."""
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2021-{((i % 12) + 1):02d}-10",
            exit_date=f"2021-{((i % 12) + 1):02d}-24",
            hold_days=14,
        )
        for i in range(40)
    ]
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2020-01-01",
        end_date="2025-01-01",
        timeframe="1d",
    )
    tc = _trade_count_results(results)
    assert len(tc) == 1
    assert tc[0].severity == "warning"


def test_trade_count_adequacy_passes_above_50_pct():
    """About 100 trades vs ~130 expected (~77%) → no result emitted."""
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"202{((i // 24) % 5)}-{((i % 12) + 1):02d}-05",
            exit_date=f"202{((i // 24) % 5)}-{((i % 12) + 1):02d}-19",
            hold_days=14,
        )
        for i in range(100)
    ]
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2020-01-01",
        end_date="2025-01-01",
        timeframe="1d",
    )
    assert _trade_count_results(results) == []


def test_trade_count_adequacy_falls_back_to_legacy_floor_when_kwargs_missing():
    """Without dates/timeframe the gate must keep the legacy ``< 5`` floor so
    older call sites that haven't been updated still produce the prior
    verdict."""
    trades = [
        _trade(trade_num=i + 1, entry_date="2024-01-02", exit_date="2024-01-09") for i in range(3)
    ]
    detector = BacktestAnomalyDetector()
    results = detector.check(_baseline_metrics(), trades)
    legacy = [r for r in results if "statistically meaningless" in r.details]
    assert len(legacy) == 1
    assert legacy[0].severity == "critical"


def test_trade_count_adequacy_skipped_in_paper_mode():
    """Paper sessions run over short windows; the trade-count gate is skipped
    entirely regardless of which kwargs were threaded in."""
    trades = [_trade(trade_num=1, entry_date="2024-05-01", exit_date="2024-05-08")]
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        mode="paper",
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
    )
    assert _trade_count_results(results) == []
    legacy = [r for r in results if "statistically meaningless" in r.details]
    assert legacy == []


# ---------------------------------------------------------------------------
# Look-ahead in returns
# ---------------------------------------------------------------------------


def _bar(date_str: str, *, open_: float, close: float) -> OHLCVBar:
    return OHLCVBar(
        date=date_str,
        open=open_,
        high=max(open_, close) + 0.5,
        low=min(open_, close) - 0.5,
        close=close,
        volume=1_000_000.0,
    )


def _market_data_perfectly_matching(trades: List[TradeRecord]) -> dict:
    """Build per-symbol bars whose intrabar direction matches each trade's
    return sign — the look-ahead failure mode."""
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        # Same-sign open→close as the trade's return.
        if t.return_pct >= 0:
            bars.append(_bar(t.entry_date, open_=100.0, close=101.5))
        else:
            bars.append(_bar(t.entry_date, open_=101.5, close=100.0))
    return bars_by_symbol


def _market_data_uncorrelated(trades: List[TradeRecord]) -> dict:
    """Bars whose intrabar direction is independent of the trade outcome —
    50/50 by trade_num parity."""
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        if t.trade_num % 2 == 0:
            bars.append(_bar(t.entry_date, open_=100.0, close=101.0))
        else:
            bars.append(_bar(t.entry_date, open_=101.0, close=100.0))
    return bars_by_symbol


def _twenty_trades() -> List[TradeRecord]:
    return [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}",
            exit_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=14,
        )
        for i in range(20)
    ]


def test_lookahead_critical_when_perfect_intrabar_sign_match():
    """≥ 20 trades and every entry-bar direction matches the trade outcome →
    critical look-ahead-bias finding."""
    trades = _twenty_trades()
    market = _market_data_perfectly_matching(trades)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    assert look[0].severity == "critical"


def test_lookahead_passes_when_agreement_under_threshold():
    """≥ 20 trades and ~50% agreement → no look-ahead finding."""
    trades = _twenty_trades()
    market = _market_data_uncorrelated(trades)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert look == []


def test_lookahead_skipped_when_market_data_missing():
    """Without market_data the rule can't look at entry bars — it must
    emit no result rather than guessing."""
    trades = _twenty_trades()
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data=None,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert look == []


def test_lookahead_skipped_when_no_entry_bars_resolve():
    """Market data is present but no symbol matches the trade ledger →
    eligible count is zero and the rule emits nothing."""
    trades = _twenty_trades()
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data={"NOT_IN_LEDGER": [_bar("2024-01-02", open_=100.0, close=101.0)]},
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert look == []


def test_lookahead_smallsample_perfect_match_is_critical():
    """Under 20 trades the bar-vs-return sign-match is noisier, but perfect
    agreement across at least 5 trades still flips critical so trivial
    intrabar leaks don't slip through."""
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-03-{(i % 27) + 1:02d}",
            exit_date=f"2024-03-{(i % 27) + 2:02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=14,
        )
        for i in range(8)
    ]
    market = _market_data_perfectly_matching(trades)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    assert look[0].severity == "critical"


def test_lookahead_warning_between_80_and_95_pct():
    """Agreement in the 80–95% band on ≥ 20 trades is a warning, not critical."""
    trades = _twenty_trades()
    # Make 17/20 = 85% of trades match by flipping the last three bars to
    # disagree with their trade direction.
    market = _market_data_perfectly_matching(trades)
    for t in trades[-3:]:
        bars = market[t.symbol]
        # Replace the bar at this trade's entry date with the opposite
        # direction. Same date list, last three in trade order.
        for i, b in enumerate(bars):
            if b.date == t.entry_date:
                if t.return_pct >= 0:
                    bars[i] = _bar(t.entry_date, open_=101.5, close=100.0)
                else:
                    bars[i] = _bar(t.entry_date, open_=100.0, close=101.5)
                break
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    assert look[0].severity == "warning"


def test_lookahead_skips_trades_whose_intraday_timestamp_does_not_exact_match():
    """On intraday timeframes many bars share a calendar day; the rule must
    require an exact ``bar.date == trade.entry_date`` match and skip trades
    whose entry timestamp can't be resolved, so the agreement statistic
    isn't computed against the wrong bar."""
    # Twenty trades, 1h timeframe. Half carry the bar's exact intraday
    # timestamp; the other half carry the calendar date only. The latter
    # must be skipped, leaving only the former in the eligible set.
    trades: List[TradeRecord] = []
    for i in range(20):
        entry_ts = (
            f"2024-03-{((i % 27) + 1):02d}T10:30:00"
            if i % 2 == 0
            else f"2024-03-{((i % 27) + 1):02d}"
        )
        exit_ts = (
            f"2024-03-{((i % 27) + 2):02d}T15:30:00"
            if i % 2 == 0
            else f"2024-03-{((i % 27) + 2):02d}"
        )
        trades.append(
            _trade(
                trade_num=i + 1,
                entry_date=entry_ts,
                exit_date=exit_ts,
                return_pct=1.5 if i % 4 < 2 else -1.0,
                hold_days=1,
            )
        )

    # Build bars whose intrabar direction PERFECTLY matches the exact-match
    # trades' return signs. For the calendar-date trades we deliberately
    # publish only an intraday timestamp ("...T09:30:00") so the exact-match
    # lookup misses them. If the rule fell back to date-prefix matching, it
    # would pick the 09:30 bar and the agreement stat would be wrong; the
    # correct behaviour is to skip those trades entirely.
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        # Always publish the day's open bar at 09:30 with one direction so
        # date-prefix fallback would see homogeneous bars.
        day_prefix = t.entry_date[:10]
        bars.append(_bar(f"{day_prefix}T09:30:00", open_=100.0, close=101.0))
        if "T" in t.entry_date:
            # Add the bar that exact-matches the intraday trade's entry,
            # with direction aligned to the trade's return sign.
            if t.return_pct >= 0:
                bars.append(_bar(t.entry_date, open_=100.0, close=101.5))
            else:
                bars.append(_bar(t.entry_date, open_=101.5, close=100.0))

    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1h",
        market_data=bars_by_symbol,
    )

    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    # 10 eligible trades (the intraday-timestamped half), all in perfect
    # agreement with their exact-match bar → small-sample critical fires.
    # The other 10 trades were correctly skipped because their calendar-date
    # entry_date didn't exact-match any bar.
    assert len(look) == 1
    assert "10/10" in look[0].details
    assert look[0].severity == "critical"
