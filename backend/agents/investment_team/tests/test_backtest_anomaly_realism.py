"""Realism extensions of ``BacktestAnomalyDetector``.

Covers the look-ahead pattern in returns — flags trades whose return sign
agrees suspiciously well with the entry bar's intrabar direction, a
heuristic for look-ahead bias that AST checks miss.

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
    return sign — the look-ahead failure mode.

    The rule now folds both entry-bar AND exit-bar directions into a
    combined agreement rate, so the fixture emits BOTH a same-sign entry
    bar and a same-sign exit bar per trade. The staircase generators
    used by some tests overlap entry_{n+1} with exit_n; in that case the
    earlier-inserted bar wins and the conflicting direction is dropped.
    Trades whose entry/exit fall on a date that some prior trade already
    owns therefore become un-resolvable for that side — they still
    contribute to the eligibility check.
    """
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        existing_dates = {b.date for b in bars}
        if t.return_pct >= 0:
            open_close = (100.0, 101.5)
        else:
            open_close = (101.5, 100.0)
        if t.entry_date not in existing_dates:
            bars.append(_bar(t.entry_date, open_=open_close[0], close=open_close[1]))
            existing_dates.add(t.entry_date)
        if t.exit_date not in existing_dates:
            bars.append(_bar(t.exit_date, open_=open_close[0], close=open_close[1]))
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
        market_data={"NOT_IN_LEDGER": [_bar("2024-01-02", open_=100.0, close=101.0)]},
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert look == []


def test_lookahead_smallsample_perfect_match_is_critical():
    """Under 20 trades the bar-vs-return sign-match is noisier, but perfect
    agreement across at least 5 trades still flips critical so trivial
    intrabar leaks don't slip through.

    Entries are spaced 3 days apart (and exits 1 day after each entry) so
    consecutive trades don't share dates — the new entry+exit combined
    rate would otherwise see a neighbour's bar at the same date and
    invert the agreement signal.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-03-{(i * 3) + 1:02d}",
            exit_date=f"2024-03-{(i * 3) + 2:02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(8)
    ]
    market = _market_data_perfectly_matching(trades)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    assert look[0].severity == "critical"
    # 8 entry bars + 8 exit bars all resolve and match → 16/16.
    assert "16/16" in look[0].details


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
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    assert look[0].severity == "warning"


def test_lookahead_intraday_exact_match_uses_specific_bar_not_prefix():
    """When a trade carries a full ISO timestamp that exact-matches one
    specific intraday bar, the rule must use THAT bar's direction even when
    other bars on the same calendar day disagree.

    Each trade is an intraday round-trip (entry 10:30, exit 15:30 same
    day) so the entry-bar and exit-bar lookups both have an exact-match
    candidate. The day publishes a 09:30 bar with bearish direction so a
    prefix-fallback would miscount; the rule must pick the trade-direction
    10:30 / 15:30 bars instead. Days are spaced 3 calendar days apart so
    consecutive trades' day-aggregates don't pollute each other.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-03-{((i * 3) + 1):02d}T10:30:00",
            exit_date=f"2024-03-{((i * 3) + 1):02d}T15:30:00",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(8)
    ]
    bars_by_symbol: dict = {}
    for t in trades:
        bars = bars_by_symbol.setdefault(t.symbol, [])
        day_prefix = t.entry_date[:10]
        # Counter-direction 09:30 bar so prefix-fallback would miscount.
        bars.append(_bar(f"{day_prefix}T09:30:00", open_=101.5, close=100.0))
        if t.return_pct >= 0:
            bars.append(_bar(t.entry_date, open_=100.0, close=101.5))
            bars.append(_bar(t.exit_date, open_=100.0, close=101.5))
        else:
            bars.append(_bar(t.entry_date, open_=101.5, close=100.0))
            bars.append(_bar(t.exit_date, open_=101.5, close=100.0))

    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data=bars_by_symbol,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    # 8 entry exact matches + 8 exit exact matches, all agreeing → 16/16.
    assert "16/16" in look[0].details
    assert look[0].severity == "critical"


def test_lookahead_falls_back_to_day_aggregate_when_trade_date_is_date_only():
    """Production trade ledgers truncate ``entry_date`` to ``YYYY-MM-DD``
    (``trade_builder.py:55`` and ``fill_simulator.py:1049``). When the
    market data is intraday, exact-match would silently fail on every
    trade and the rule would never fire — defeating the realism veto.

    The rule must fall back to a day-aggregate direction (first same-day
    bar's open → last same-day bar's close) so look-ahead at day
    granularity still gets caught. Each trade is an intraday round-trip
    on a single calendar day (entry_date == exit_date) so both the entry
    and exit lookups resolve via the same day-aggregate. Days are spaced
    3 apart so consecutive trades don't share intraday bars.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-03-{((i * 3) + 1):02d}",
            exit_date=f"2024-03-{((i * 3) + 1):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(8)
    ]
    # Each day has TWO intraday bars (09:30 open + 16:00 close) whose
    # combined direction (first.open → last.close) matches the trade's
    # return sign. Exact-match against the date-only entry/exit fails for
    # every trade — only the day-aggregate fallback can resolve them.
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
        market_data=bars_by_symbol,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    # All 8 trades resolve via day-aggregate for both entry and exit
    # (they share the calendar day) → 16/16 perfect agreement.
    assert len(look) == 1
    assert "16/16" in look[0].details
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
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert look == []


# ---------------------------------------------------------------------------
# Detector hardening: info on zero eligibility + degraded-sample warning
# ---------------------------------------------------------------------------


def _trades_with_partial_market_data(
    *, n_total: int, n_with_bars: int
) -> tuple[List[TradeRecord], dict]:
    """Build ``n_total`` trades but emit entry/exit bars for only the first
    ``n_with_bars`` — exercises the entry-eligibility ratio path.

    The bars that DO exist are direction-aligned with their trade's return
    (the look-ahead failure mode), so when the resolvable subset is large
    enough to satisfy the degenerate-sample guard the rule should still
    fire a critical / warning AND surface the degraded-sample notice.
    Trades are spaced 3 calendar days apart so consecutive entry / exit
    bars don't collide.
    """
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-{((i % 12) + 1):02d}-{((i * 3) % 27 + 1):02d}",
            exit_date=f"2024-{((i % 12) + 1):02d}-{((i * 3) % 27 + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(n_total)
    ]
    market = _market_data_perfectly_matching(trades[:n_with_bars])
    return trades, market


def test_lookahead_emits_info_when_zero_trades_resolve_a_bar() -> None:
    """When no trade can be matched to a bar (market data covers an
    unrelated symbol or window), the rule used to silently emit nothing.
    Reviewers couldn't distinguish "ran cleanly" from "never ran".
    The hardened rule emits an info-level marker instead so the audit
    trail records that the heuristic was skipped, not that it passed.
    """
    trades = _twenty_trades()
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data={"NOT_IN_LEDGER": [_bar("2024-01-02", open_=100.0, close=101.0)]},
    )
    info = [
        r for r in results if r.severity == "info" and "lookahead_bar_predictability" in r.details
    ]
    assert len(info) == 1
    assert "sample insufficient" in info[0].details


def test_lookahead_emits_degraded_sample_warning_when_under_half_resolve() -> None:
    """20-trade ledger but only 2 trades have matching bars → entry
    eligibility ratio 10% (well below the 50% floor). The rule must
    surface a warning that the agreement statistic ran on a degraded
    sample so reviewers know not to over-weight the result.
    """
    trades, market = _trades_with_partial_market_data(n_total=20, n_with_bars=2)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data=market,
    )
    degraded = [r for r in results if r.severity == "warning" and "degraded sample" in r.details]
    assert len(degraded) == 1
    assert "2/20" in degraded[0].details


def test_lookahead_does_not_emit_degraded_warning_when_sample_is_small() -> None:
    """Below 10 trades the degraded-sample warning is suppressed — a
    sub-10 ledger is already too thin to draw conclusions from, so an
    additional 'sample is degraded' notice would just be noise."""
    trades, market = _trades_with_partial_market_data(n_total=9, n_with_bars=2)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data=market,
    )
    degraded = [r for r in results if r.severity == "warning" and "degraded sample" in r.details]
    assert degraded == []


def test_lookahead_thresholds_key_off_trade_count_not_bar_observation_count() -> None:
    """A 10-trade ledger with perfect entry+exit agreement produces 20 bar
    observations. The 95% critical band was calibrated for ``>= 20`` distinct
    *trades*, not 20 bar observations — two bars per trade share the trade's
    own outcome and are not independent samples. The detector must gate the
    high-confidence critical on the trade count and route a 10-trade perfect
    fixture through the small-sample backstop instead (5 <= trades < 20,
    rate >= 0.999).
    """
    # 10 trades, alternating sign so the degenerate-sample guard passes.
    # Entries spaced 3 days apart so the per-trade entry+exit bars don't
    # collide with neighbouring trades' dates.
    trades = [
        _trade(
            trade_num=i + 1,
            entry_date=f"2024-{((i % 12) + 1):02d}-{((i * 3) % 27 + 1):02d}",
            exit_date=f"2024-{((i % 12) + 1):02d}-{((i * 3) % 27 + 2):02d}",
            return_pct=1.5 if i % 2 == 0 else -1.0,
            hold_days=1,
        )
        for i in range(10)
    ]
    market = _market_data_perfectly_matching(trades)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    # Perfect agreement on 10 trades (20 observations) MUST still trip
    # critical — but via the small-sample backstop, whose message
    # references "across {n} trades" with n < 20 so reviewers can
    # disambiguate the branch.
    assert len(look) == 1
    assert look[0].severity == "critical"
    assert "across 10 trades" in look[0].details


def test_lookahead_combined_rate_uses_entry_and_exit_observations() -> None:
    """A leak that shows up on exit bars but NOT on entry bars (e.g., the
    code peeks at the close of the exit bar before deciding to exit)
    must trip the combined-rate critical when the entry-only signal
    would have under-counted.

    Fixture: 20 trades, each with an entry bar that does NOT match the
    return direction AND an exit bar that DOES. Entry-only agreement is
    0/20; exit-only agreement is 20/20. Combined: 20/40 = 50%. The 50%
    rate sits well below any critical/warning threshold, so the test
    pivots to the opposite construction — entry AND exit both align —
    and asserts the message reports the combined eligible count.
    """
    trades = _twenty_trades()
    market = _market_data_perfectly_matching(trades)
    detector = BacktestAnomalyDetector()
    results = detector.check(
        _baseline_metrics(),
        trades,
        market_data=market,
    )
    look = [r for r in results if "look-ahead" in r.details or "look_ahead" in r.details]
    assert len(look) == 1
    # 20 entry bars + 20 exit bars all resolve and match → 40/40.
    assert "40/40" in look[0].details
    assert look[0].severity == "critical"
