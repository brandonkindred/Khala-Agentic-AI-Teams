"""Unit tests for :class:`LiquidityRealismGate`."""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Dict, List

from investment_team.market_data_service import OHLCVBar, compute_adv_from_bars
from investment_team.models import TradeRecord
from investment_team.strategy_lab.quality_gates.realism import liquidity_realism
from investment_team.strategy_lab.quality_gates.realism.liquidity_realism import (
    GATE,
    LiquidityRealismGate,
    _build_adv_series,
)


def _trade(
    *,
    trade_num: int,
    position_value: float,
    net_pnl: float,
    symbol: str = "QQQ",
    entry_date: str = "2024-03-01",
) -> TradeRecord:
    shares = position_value / 100.0
    return TradeRecord(
        trade_num=trade_num,
        entry_date=entry_date,
        exit_date=entry_date,
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=100.0 + net_pnl / max(shares, 1.0),
        shares=shares,
        position_value=position_value,
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        return_pct=(net_pnl / position_value) * 100 if position_value else 0.0,
        hold_days=5,
        outcome="win" if net_pnl > 0 else "loss",
        cumulative_pnl=net_pnl * trade_num,
    )


def _bar(date_str: str, *, close: float, volume: float) -> OHLCVBar:
    return OHLCVBar(
        date=date_str,
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=volume,
    )


def _market_data_with_adv(
    symbol: str, *, daily_dollar_volume: float, lookback: int = 20
) -> Dict[str, List[OHLCVBar]]:
    """Build a market_data dict whose ADV equals ``daily_dollar_volume`` over
    ``lookback`` bars."""
    bars = []
    close = 100.0
    volume = daily_dollar_volume / close
    for i in range(lookback):
        bars.append(_bar(f"2024-02-{i + 1:02d}", close=close, volume=volume))
    return {symbol: bars}


def _criticals(results):
    return [r for r in results if not r.passed and r.severity == "critical"]


def _warnings(results):
    return [r for r in results if not r.passed and r.severity == "warning"]


# ---------------------------------------------------------------------------
# Skip / no-input paths
# ---------------------------------------------------------------------------


def test_skips_when_market_data_is_none():
    gate = LiquidityRealismGate()
    results = gate.check([_trade(trade_num=1, position_value=1000.0, net_pnl=10.0)], None)
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)
    assert "skipped" in results[0].details.lower()


def test_skips_when_trade_ledger_is_empty():
    gate = LiquidityRealismGate()
    results = gate.check([], _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0))
    assert all(r.passed and r.severity == "info" for r in results)
    assert "empty" in results[0].details.lower()


def test_skips_per_trade_when_adv_unresolvable():
    """When a symbol has insufficient bars, ADV is None — that trade is
    counted as ``unresolvable`` and its P&L kept as-is. With no oversized
    trades the verdict is info."""
    gate = LiquidityRealismGate()
    # Only 5 bars (default lookback is 20) → compute_adv_from_bars returns
    # None.
    market = {"QQQ": [_bar(f"2024-02-{i + 1:02d}", close=100.0, volume=10_000.0) for i in range(5)]}
    results = gate.check(
        [_trade(trade_num=1, position_value=1000.0, net_pnl=10.0)],
        market,
    )
    assert all(r.passed for r in results)
    assert "unresolvable" in results[0].details


# ---------------------------------------------------------------------------
# Clean / oversized / critical paths
# ---------------------------------------------------------------------------


def test_clean_when_all_trades_fit_envelope():
    """ADV $10M; envelope 1% = $100k; trades of $50k each fit cleanly."""
    gate = LiquidityRealismGate()
    market = _market_data_with_adv("QQQ", daily_dollar_volume=10_000_000.0)
    trades = [_trade(trade_num=i + 1, position_value=50_000.0, net_pnl=200.0) for i in range(10)]
    results = gate.check(trades, market)
    assert all(r.passed and r.severity == "info" for r in results)
    assert "clean" in results[0].details


def test_warning_when_oversized_but_pf_still_positive():
    """Half the trades are oversized but the haircut doesn't flip the
    profit factor below 1.0 → warning."""
    gate = LiquidityRealismGate()
    market = _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0)
    # Envelope = 1% of $1M = $10k. Trades at $30k are 3× envelope.
    trades = []
    for i in range(20):
        if i < 10:
            trades.append(_trade(trade_num=i + 1, position_value=5_000.0, net_pnl=200.0))
        else:
            trades.append(_trade(trade_num=i + 1, position_value=30_000.0, net_pnl=2_000.0))
    results = gate.check(trades, market)
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "10 of 20" in warnings[0].details or "10" in warnings[0].details
    assert _criticals(results) == []


def test_critical_when_oversized_trades_flip_pf_below_one():
    """All trades 10× the envelope with modest P&L; the slippage haircut
    (25 bps × 9 multiples × position value = much larger than the P&L)
    eats the profit and the adjusted PF collapses → critical."""
    gate = LiquidityRealismGate()
    market = _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0)
    # Envelope = $10k. Trades at $100k = 10× envelope. Extra slippage
    # 25 bps * (10 - 1) = 225 bps. Haircut = 0.0225 * $100k = $2,250 per
    # trade. Reported net_pnl per trade is $1,000 → adjusted = -$1,250.
    trades = [_trade(trade_num=i + 1, position_value=100_000.0, net_pnl=1_000.0) for i in range(20)]
    results = gate.check(trades, market)
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "< 1.0" in criticals[0].details
    assert criticals[0].gate_name == GATE


def test_borderline_oversized_does_not_flip_critical():
    """Trades at 1.1× envelope cost ~2.5 bps extra; on a $100k position
    that's $25 — negligible vs the $500 trade P&L. Stays warning."""
    gate = LiquidityRealismGate()
    market = _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0)
    # 1.1× envelope = $11k; let's use $11k with $500 net P&L per trade.
    trades = [_trade(trade_num=i + 1, position_value=11_000.0, net_pnl=500.0) for i in range(20)]
    results = gate.check(trades, market)
    # All 20 are oversized → fraction 100% → warning, not critical (PF intact).
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert _criticals(results) == []


def test_skips_oversize_check_for_symbols_with_no_market_data():
    """A trade on a symbol absent from market_data is counted as
    unresolvable, not oversized."""
    gate = LiquidityRealismGate()
    market = _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0)
    trades = [_trade(trade_num=1, position_value=10_000_000.0, symbol="UNKNOWN", net_pnl=100.0)]
    results = gate.check(trades, market)
    assert _criticals(results) == []
    assert _warnings(results) == []


def test_critical_message_cites_envelope_and_haircut():
    gate = LiquidityRealismGate(liquidity_envelope_pct=0.02)
    market = _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0)
    trades = [_trade(trade_num=i + 1, position_value=200_000.0, net_pnl=500.0) for i in range(20)]
    results = gate.check(trades, market)
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "2%" in criticals[0].details


def test_does_not_emit_critical_when_pf_below_1_with_zero_oversized_trades():
    """A losing strategy whose trades all fit inside the liquidity envelope
    is not a liquidity failure — vetoing here would mislabel the cause.
    The acceptance gate and anomaly detector own the "strategy lost
    money" verdict; this gate only fires when the loss is attributable
    to oversized fills."""
    gate = LiquidityRealismGate()
    market = _market_data_with_adv("QQQ", daily_dollar_volume=10_000_000.0)
    # All trades $50k against $100k envelope (well under). Net losers.
    trades = [_trade(trade_num=i + 1, position_value=50_000.0, net_pnl=-200.0) for i in range(20)]
    results = gate.check(trades, market)
    # PF would be 0 (no winners, all losers) but no oversized trades →
    # the gate must NOT veto. Verdict is clean info.
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_adv_resolves_to_window_preceding_each_trade_not_endofsample():
    """The gate must evaluate each trade against the liquidity that
    existed at the time the trade was entered, not a single snapshot
    from the tail of the bar series. Fixture: a symbol whose ADV is
    low in early 2024 (1k shares/day at $100 = $100k ADV → $1k
    envelope at 1%) and high in late 2024 (1M shares/day at $100 =
    $100M ADV → $1M envelope). A $5k trade entered in February is
    oversized against the contemporaneous February ADV; the same
    trade entered in November would not be. Without the per-timestamp
    fix it would be evaluated against the December tail and incorrectly
    pass."""
    bars: List[OHLCVBar] = []
    # 50 bars Jan-Feb 2024 at LOW volume ($100k dollar ADV).
    for i in range(50):
        month = 1 if i < 31 else 2
        day = i + 1 if i < 31 else i - 30
        bars.append(_bar(f"2024-{month:02d}-{day:02d}", close=100.0, volume=1_000.0))
    # 50 bars Nov-Dec 2024 at HIGH volume ($100M dollar ADV).
    for i in range(50):
        month = 11 if i < 30 else 12
        day = i + 1 if i < 30 else i - 29
        bars.append(_bar(f"2024-{month:02d}-{day:02d}", close=100.0, volume=1_000_000.0))
    market = {"QQQ": bars}

    # A $5k trade in February — well above the $1k contemporaneous envelope.
    early_trade = _trade(
        trade_num=1, position_value=5_000.0, net_pnl=-200.0, entry_date="2024-02-15"
    )
    # Same-sized trade in November — well inside the $1M contemporaneous envelope.
    late_trade = _trade(
        trade_num=2, position_value=5_000.0, net_pnl=-200.0, entry_date="2024-11-15"
    )

    gate = LiquidityRealismGate()
    # Single early trade: oversized → adjusted PF would collapse since
    # haircut > net P&L magnitude → critical attributable to liquidity.
    results_early = gate.check([early_trade], market)
    criticals_early = _criticals(results_early)
    assert len(criticals_early) == 1, (
        "early-window trade should be flagged against early-window ADV"
    )

    # Single late trade: not oversized → no liquidity finding (info clean).
    results_late = gate.check([late_trade], market)
    assert _criticals(results_late) == []
    assert _warnings(results_late) == []


def test_does_not_emit_critical_when_pf_below_1_with_only_unresolvable_adv():
    """When every symbol's ADV is unresolvable (insufficient bars),
    oversized_count stays zero so no liquidity attribution exists for
    the PF collapse. The gate must skip critical and surface the
    unresolvable count instead."""
    gate = LiquidityRealismGate()
    # Only 5 bars (default lookback 20) → ADV is None.
    market = {"QQQ": [_bar(f"2024-02-{i + 1:02d}", close=100.0, volume=10_000.0) for i in range(5)]}
    trades = [_trade(trade_num=i + 1, position_value=10_000.0, net_pnl=-500.0) for i in range(20)]
    results = gate.check(trades, market)
    assert _criticals(results) == []
    assert "unresolvable" in results[0].details.lower()


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_positive_envelope():
    import pytest

    with pytest.raises(ValueError, match="liquidity_envelope_pct"):
        LiquidityRealismGate(liquidity_envelope_pct=0.0)


def test_constructor_rejects_negative_slippage_scale():
    import pytest

    with pytest.raises(ValueError, match="slippage_scale_bps"):
        LiquidityRealismGate(slippage_scale_bps=-1.0)


def test_constructor_rejects_non_positive_lookback():
    import pytest

    with pytest.raises(ValueError, match="adv_lookback"):
        LiquidityRealismGate(adv_lookback=0)


def test_adv_as_of_trade_excludes_imputed_bars_consistently() -> None:
    """The gate's own lookback-count guard must exclude imputed bars exactly as
    compute_adv_from_bars does — otherwise a window padded out to ``lookback``
    by synthetic forward-fills clears the length check only for the downstream
    ADV to come back None, silently flipping the trade to 'unresolvable'.
    """
    from investment_team.strategy_lab.quality_gates.realism.liquidity_realism import (
        _adv_as_of_trade,
    )

    real = [
        OHLCVBar(date=f"2024-01-{i:02d}", open=100, high=100, low=100, close=100, volume=1_000_000)
        for i in range(1, 16)
    ]
    imputed = [
        OHLCVBar(
            date=f"2024-01-{i:02d}",
            open=100,
            high=100,
            low=100,
            close=100,
            volume=0.0,
            is_imputed=True,
        )
        for i in range(16, 21)
    ]
    # 15 real + 5 imputed prior bars: only 15 real < lookback=20 -> None.
    assert _adv_as_of_trade(real + imputed, "2024-02-01", lookback=20) is None
    # 20 real prior bars compute normally.
    full = [
        OHLCVBar(date=f"2024-03-{i:02d}", open=100, high=100, low=100, close=100, volume=1_000_000)
        for i in range(1, 21)
    ]
    assert _adv_as_of_trade(full, "2024-04-01", lookback=20) == 100_000_000.0


# ---------------------------------------------------------------------------
# Precomputed ADV series — regression and performance guards
# ---------------------------------------------------------------------------


def _random_bars(
    rng: random.Random, *, num_days: int, start: str, imputed_rate: float = 0.0
) -> List[OHLCVBar]:
    """Deterministic (seeded) chronological daily bars for regression/perf
    fixtures. ``imputed_rate`` fraction of days are synthesized as imputed
    (zero-volume, forward-filled) bars to exercise the imputed-skip path."""
    start_date = date.fromisoformat(start)
    bars: List[OHLCVBar] = []
    last_close = 100.0
    for i in range(num_days):
        bar_date = (start_date + timedelta(days=i)).isoformat()
        if rng.random() < imputed_rate:
            bars.append(
                OHLCVBar(
                    date=bar_date,
                    open=last_close,
                    high=last_close,
                    low=last_close,
                    close=last_close,
                    volume=0.0,
                    is_imputed=True,
                )
            )
            continue
        last_close = round(rng.uniform(50.0, 500.0), 2)
        bars.append(_bar(bar_date, close=last_close, volume=rng.uniform(1_000.0, 5_000_000.0)))
    return bars


def _reference_adv_as_of_trade(bars, entry_date, *, lookback):
    """Verbatim copy of the pre-refactor ``_adv_as_of_trade`` body — kept
    independent of production code (not imported) so the regression test
    below cannot pass merely because both sides call the same helper.
    Calling ``compute_adv_from_bars`` here is intentional and not
    circular: the refactor under test only changes *how/when* ADV is
    resolved per trade, not the ADV formula itself (covered independently
    in test_market_data_service.py).
    """
    if not bars or lookback <= 0 or not entry_date:
        return None
    cutoff = entry_date[:10]
    prior_bars = [b for b in bars if b.date[:10] < cutoff]
    return compute_adv_from_bars(prior_bars, lookback=lookback)


def test_adv_series_matches_reference_algorithm_and_verdict_is_unchanged(monkeypatch):
    """Regression guard: the precomputed-series + binary-search lookup must
    resolve to the exact same ADV as the pre-refactor per-trade filter-and-
    scan algorithm, and the gate's overall verdict must be byte-identical,
    across a multi-year, multi-symbol, multi-hundred-trade fixture that
    mixes in imputed bars and trades too early in the history to have a
    full lookback window.
    """
    rng = random.Random(1800)
    lookback = 20
    symbols = ["AAA", "BBB", "CCC"]
    market: Dict[str, List[OHLCVBar]] = {
        symbol: _random_bars(rng, num_days=500, start="2022-01-03", imputed_rate=0.05)
        for symbol in symbols
    }

    trades: List[TradeRecord] = []
    for i in range(300):
        symbol = rng.choice(symbols)
        entry_bar = rng.choice(market[symbol])
        trades.append(
            _trade(
                trade_num=i + 1,
                position_value=rng.uniform(1_000.0, 500_000.0),
                net_pnl=rng.uniform(-5_000.0, 5_000.0),
                symbol=symbol,
                entry_date=entry_bar.date,
            )
        )

    # Layer 1: per-ADV-value comparison against the independent oracle.
    series_by_symbol = {
        symbol: _build_adv_series(bars, lookback=lookback) for symbol, bars in market.items()
    }
    for trade in trades:
        expected = _reference_adv_as_of_trade(
            market[trade.symbol], trade.entry_date, lookback=lookback
        )
        actual = series_by_symbol[trade.symbol].lookup(trade.entry_date)
        assert actual == expected, (
            f"ADV mismatch for {trade.symbol}@{trade.entry_date}: "
            f"reference={expected!r} new={actual!r}"
        )

    # Layer 2: downstream-verdict comparison. Run the gate with the fast
    # (production) path, then again with _build_adv_series swapped for a
    # reference-oracle-backed stand-in, and assert the QualityGateResults
    # are identical (barring the wall-clock evaluated_at timestamp).
    gate = LiquidityRealismGate(adv_lookback=lookback)
    fast_results = gate.check(trades, market)

    class _ReferenceSeries:
        def __init__(self, bars, lookback):
            self._bars = bars
            self._lookback = lookback

        def lookup(self, entry_date):
            return _reference_adv_as_of_trade(self._bars, entry_date, lookback=self._lookback)

    monkeypatch.setattr(
        liquidity_realism,
        "_build_adv_series",
        lambda bars, *, lookback: _ReferenceSeries(bars, lookback),
    )
    reference_results = gate.check(trades, market)

    fast_dump = [r.model_dump(exclude={"evaluated_at"}) for r in fast_results]
    reference_dump = [r.model_dump(exclude={"evaluated_at"}) for r in reference_results]
    assert fast_dump == reference_dump


def test_compute_adv_from_bars_call_count_independent_of_trade_count(monkeypatch):
    """Characterization test: the number of calls to compute_adv_from_bars
    must depend only on the bar history (each symbol's series is built
    once), not on how many trades reference that symbol. This is the
    behavioral proof of the O(bars + trades*log bars) shape, replacing the
    old O(trades*bars) per-trade rebuild.
    """
    rng = random.Random(42)
    symbols = ["AAA", "BBB", "CCC"]
    market = {
        symbol: _random_bars(rng, num_days=300, start="2023-01-02", imputed_rate=0.0)
        for symbol in symbols
    }
    total_bars = sum(len(bars) for bars in market.values())

    real_compute = liquidity_realism.compute_adv_from_bars
    call_count = {"n": 0}

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(liquidity_realism, "compute_adv_from_bars", _counting)

    def _ledger(n: int) -> List[TradeRecord]:
        trades = []
        for i in range(n):
            symbol = symbols[i % len(symbols)]
            bar = market[symbol][20 + i % (len(market[symbol]) - 20)]
            trades.append(
                _trade(
                    trade_num=i + 1,
                    position_value=10_000.0,
                    net_pnl=100.0,
                    symbol=symbol,
                    entry_date=bar.date,
                )
            )
        return trades

    gate = LiquidityRealismGate()

    gate.check(_ledger(50), market)
    calls_for_50 = call_count["n"]

    call_count["n"] = 0
    gate.check(_ledger(500), market)
    calls_for_500 = call_count["n"]

    assert calls_for_50 == calls_for_500
    assert 0 < calls_for_50 <= total_bars


def test_build_adv_series_called_once_per_distinct_symbol(monkeypatch):
    """The per-symbol ADV series must be built exactly once regardless of
    how many trades reference that symbol — the mechanism behind the
    O(bars) (not O(trades*bars)) guarantee above."""
    market = _market_data_with_adv("QQQ", daily_dollar_volume=1_000_000.0)
    real_build = liquidity_realism._build_adv_series
    call_count = {"n": 0}

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(liquidity_realism, "_build_adv_series", _counting)

    gate = LiquidityRealismGate()
    trades = [_trade(trade_num=i + 1, position_value=5_000.0, net_pnl=10.0) for i in range(50)]
    gate.check(trades, market)

    assert call_count["n"] == 1


def test_build_adv_series_and_lookup_handle_degenerate_inputs():
    assert _build_adv_series(None, lookback=20).lookup("2024-01-01") is None
    assert _build_adv_series([], lookback=20).lookup("2024-01-01") is None
    bars = [_bar("2024-01-01", close=100.0, volume=1_000.0)]
    assert _build_adv_series(bars, lookback=0).lookup("2024-02-01") is None
    series = _build_adv_series(bars, lookback=1)
    assert series.lookup("") is None
