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


def test_lookahead_intraday_exact_match_uses_specific_bar_not_prefix():
    """When a trade carries a full ISO timestamp that exact-matches one
    specific intraday bar, the rule must use THAT bar's direction even when
    other bars on the same calendar day disagree.

    The day publishes a 09:30 bar with bearish direction and a 10:30 bar
    with bullish direction; each trade's exact ``entry_date`` resolves to
    the 10:30 bar, which agrees with the trade return. Were the rule to
    accidentally fall back to a calendar-prefix lookup it would see the
    09:30 bar and the agreement statistic would invert.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-03-{((i % 27) + 1):02d}T10:30:00",
            exit_date=f"2024-03-{((i % 27) + 2):02d}T15:30:00",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(20)
    ]
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        day_prefix = t.entry_date[:10]
        # Counter-direction 09:30 bar so prefix-fallback would miscount.
        bars.append(_bar(f"{day_prefix}T09:30:00", open_=101.5, close=100.0))
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
    assert len(look) == 1
    assert "20/20" in look[0].details
    assert look[0].severity == "critical"


def test_lookahead_falls_back_to_day_aggregate_when_trade_date_is_date_only():
    """Production trade ledgers truncate ``entry_date`` to ``YYYY-MM-DD``
    (``trade_builder.py:55`` and ``fill_simulator.py:1049``). When the
    market data is intraday, exact-match would silently fail on every
    trade and the rule would never fire — defeating the realism veto.

    The rule must fall back to a day-aggregate direction (first same-day
    bar's open → last same-day bar's close) so look-ahead at day
    granularity still gets caught.
    """
    # 20 date-only trades whose returns track the day's NET intraday move
    # (09:30 open → 16:00 close). Returns alternate sign across days.
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-03-{((i % 27) + 1):02d}",
            exit_date=f"2024-03-{((i % 27) + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(20)
    ]
    # Each day has TWO intraday bars (09:30 open + 16:00 close) whose
    # combined direction (first.open → last.close) matches the trade's
    # return sign. Exact-match against the date-only ``entry_date`` fails
    # for every trade — only the day-aggregate fallback can resolve them.
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        day = t.entry_date  # already date-only
        if t.return_pct >= 0:
            bars.append(_bar(f"{day}T09:30:00", open_=100.0, close=100.2))
            bars.append(_bar(f"{day}T16:00:00", open_=100.2, close=102.0))
        else:
            bars.append(_bar(f"{day}T09:30:00", open_=101.5, close=101.3))
            bars.append(_bar(f"{day}T16:00:00", open_=101.3, close=100.0))

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
    # All 20 trades resolve via day-aggregate; perfect agreement → critical.
    assert len(look) == 1
    assert "20/20" in look[0].details
    assert look[0].severity == "critical"


def test_lookahead_returns_no_result_when_no_same_day_bars_exist():
    """When neither the exact-match path nor the day-aggregate path can
    resolve any trade (no bars share the calendar date), the rule emits
    nothing — there's genuinely no signal to evaluate."""
    trades = _twenty_trades()
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        start_date="2024-01-01",
        end_date="2024-12-31",
        timeframe="1d",
        market_data={"QQQ": [_bar("2099-12-31", open_=100.0, close=101.0)]},
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert look == []


def test_lookahead_critical_for_short_only_strategy_picking_down_bars():
    """A look-ahead-biased SHORT strategy enters when the bar moves DOWN.
    In this codebase profitable shorts carry ``return_pct > 0`` on down
    bars, so a naive ``bar_dir == sign(return_pct)`` would systematically
    DISAGREE and the rule would miss the bias. The side-aware comparison
    must catch it.

    Fixture: 20 short trades, alternating winners and losers. Winners
    enter on down bars and earn positive return_pct; losers enter on up
    bars and earn negative return_pct. Both arrangements agree under the
    side-aware comparison ``effective_bar_dir = bar_dir * side_sign``.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}",
            exit_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            side="short",
            hold_days=14,
        )
        for i in range(20)
    ]
    # Winning shorts (return_pct > 0) → down bar; losing shorts → up bar.
    market: dict = {}
    for t in trades:
        bars = market.setdefault(t.symbol, [])
        if t.return_pct >= 0:
            bars.append(_bar(t.entry_date, open_=101.5, close=100.0))
        else:
            bars.append(_bar(t.entry_date, open_=100.0, close=101.5))

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
    assert "20/20" in look[0].details
    assert look[0].severity == "critical"


def test_lookahead_critical_on_mixed_long_short_ledger_with_aligned_bets():
    """Mixed long+short ledger where every trade's directional bet aligned
    with the entry bar's move (longs on up bars, shorts on down bars), and
    every trade won. The side-aware comparison sees both subsets as
    agreeing and fires critical against the union."""
    trades: List[TradeRecord] = []
    for i in range(20):
        side = "long" if i % 2 == 0 else "short"
        trades.append(
            _trade(
                trade_num=i + 1,
                entry_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}",
                exit_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 2):02d}",
                return_pct=1.5,  # every trade wins
                side=side,
                hold_days=14,
            )
        )
    market: dict = {}
    for t in trades:
        bars = market.setdefault(t.symbol, [])
        if t.side == "long":
            bars.append(_bar(t.entry_date, open_=100.0, close=101.5))
        else:
            bars.append(_bar(t.entry_date, open_=101.5, close=100.0))

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
    # The fixture sets ret_dir = +1 for every trade, which collapses the
    # ret-sign distribution to a single value → degenerate-sample guard
    # short-circuits. Make some trades losers to satisfy the guard while
    # keeping the side/bar alignment intact for the agreement count.
    assert look == [], (
        "all-winners fixture is degenerate by design — guard should skip; "
        "see following non-degenerate test for the positive case"
    )


def test_lookahead_critical_on_mixed_long_short_with_winners_and_losers():
    """Non-degenerate mixed ledger: longs on up bars + shorts on down bars
    when winning; longs on down bars + shorts on up bars when losing. The
    side-aware comparison treats both winning subsets as agreeing and
    both losing subsets as disagreeing → high but not 100% agreement."""
    trades: List[TradeRecord] = []
    for i in range(20):
        side = "long" if i % 2 == 0 else "short"
        is_winner = i % 5 != 0  # 16 winners, 4 losers → distribution spans both signs
        trades.append(
            _trade(
                trade_num=i + 1,
                entry_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}",
                exit_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 2):02d}",
                return_pct=1.5 if is_winner else -1.0,
                side=side,
                hold_days=14,
            )
        )
    market: dict = {}
    for t in trades:
        bars = market.setdefault(t.symbol, [])
        winning = t.return_pct > 0
        # Long+winning → up bar; long+losing → down bar.
        # Short+winning → down bar; short+losing → up bar.
        if (t.side == "long") == winning:
            bars.append(_bar(t.entry_date, open_=100.0, close=101.5))
        else:
            bars.append(_bar(t.entry_date, open_=101.5, close=100.0))

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
    # 20 eligible trades, all agree under the side-aware comparison
    # (winners' bets aligned with bar, losers' bets opposed bar — both
    # consistent with the heuristic's hypothesis), and ret_dir spans both
    # signs → degenerate guard passes → critical.
    assert len(look) == 1
    assert "20/20" in look[0].details
    assert look[0].severity == "critical"


def test_lookahead_passes_when_short_strategy_is_genuinely_signal_driven():
    """A genuinely signal-driven short strategy enters on some down bars
    (winners) and some up bars (losers, due to noise/whipsaws), with no
    systematic alignment. Agreement around chance → no finding.

    Outcomes are keyed on ``i % 2`` so the ret_dir distribution spans both
    signs; bar directions are keyed on ``i % 3`` so the two axes are
    uncorrelated. Across 20 trades the agreement rate is well below the
    80% warning threshold.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}",
            exit_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            side="short",
            hold_days=14,
        )
        for i in range(20)
    ]
    market: dict = {}
    for i, t in enumerate(trades):
        bars = market.setdefault(t.symbol, [])
        # i % 3 != i % 2, so bar direction is uncorrelated with outcome.
        if i % 3 == 0:
            bars.append(_bar(t.entry_date, open_=101.5, close=100.0))  # down bar
        else:
            bars.append(_bar(t.entry_date, open_=100.0, close=101.5))  # up bar

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


def test_lookahead_skips_trades_with_unrecognised_side():
    """A malformed ledger row with a non-canonical ``side`` value must be
    skipped rather than silently defaulted to long — defaulting would hide
    short-side look-ahead in legacy/typo data."""
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}",
            exit_date=f"2024-{((i % 12) + 1):02d}-{((i % 27) + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            side="SELL",  # non-canonical
            hold_days=14,
        )
        for i in range(20)
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
    assert look == []
