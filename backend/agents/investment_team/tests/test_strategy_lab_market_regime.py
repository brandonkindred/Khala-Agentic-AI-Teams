"""Tests for the market-regime summary derivation and prompt rendering.

Covers:
  * the pure classification helpers (trend direction / strength, volatility);
  * :func:`compute_regime_summary` over injected (network-free) OHLCV bars,
    including the fail-open paths (fetch raises, insufficient bars, warmup
    failure);
  * :func:`regime_to_prompt_block` rendering.
"""

from __future__ import annotations

from typing import List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.strategy_lab.market_regime import (
    _MIN_BARS,
    RegimeEntry,
    RegimeSummary,
    _classify_trend,
    _classify_trend_strength,
    _classify_volatility,
    compute_regime_summary,
    regime_to_prompt_block,
)

_COMPUTED_AT = "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Bar builders
# ---------------------------------------------------------------------------


def _make_bars(closes: List[float], spreads: List[float]) -> List[OHLCVBar]:
    """Build OHLCV bars from per-bar close + intrabar (high-low) spread.

    ``open`` = ``close``; ``high``/``low`` straddle the close by half the
    spread, so ATR is driven by ``spreads`` while trend is driven by ``closes``.
    """
    assert len(closes) == len(spreads)
    bars: List[OHLCVBar] = []
    for i, (c, s) in enumerate(zip(closes, spreads)):
        bars.append(
            OHLCVBar(
                date=f"2025-01-{i + 1:03d}",
                open=c,
                high=c + s / 2,
                low=c - s / 2,
                close=c,
                volume=1000.0,
            )
        )
    return bars


def _uptrend_low_vol_bars(n: int = 260) -> List[OHLCVBar]:
    """Steady uptrend whose intrabar range collapses over the final stretch.

    Close rises a constant 0.3/bar (keeps close > SMA50 > SMA200 and ADX high),
    while the high-low spread is large for the first bars and small for the last
    ~30 — so the latest ATR% lands in the low tercile of its own distribution.
    """
    closes = [100.0 + 0.3 * i for i in range(n)]
    spreads = [5.0] * (n - 30) + [0.4] * 30
    return _make_bars(closes, spreads)


def _downtrend_bars(n: int = 260) -> List[OHLCVBar]:
    closes = [200.0 - 0.3 * i for i in range(n)]
    spreads = [3.0] * n
    return _make_bars(closes, spreads)


def _uptrend_high_vol_bars(n: int = 260) -> List[OHLCVBar]:
    """Uptrend whose intrabar range expands over the final stretch → high vol."""
    closes = [100.0 + 0.3 * i for i in range(n)]
    spreads = [0.4] * (n - 40) + [9.0] * 40
    return _make_bars(closes, spreads)


# ---------------------------------------------------------------------------
# Pure classification helpers (deterministic)
# ---------------------------------------------------------------------------


def test_classify_trend_up_down_sideways() -> None:
    assert _classify_trend(close=110, sma50=105, sma200=100) == "up"
    assert _classify_trend(close=90, sma50=95, sma200=100) == "down"
    # Mixed ordering (fast MA above slow but price below fast) → sideways.
    assert _classify_trend(close=98, sma50=105, sma200=100) == "sideways"
    assert _classify_trend(close=100, sma50=100, sma200=100) == "sideways"


def test_classify_trend_strength_buckets() -> None:
    assert _classify_trend_strength(10.0) == "weak"
    assert _classify_trend_strength(19.999) == "weak"
    assert _classify_trend_strength(20.0) == "moderate"
    assert _classify_trend_strength(24.999) == "moderate"
    assert _classify_trend_strength(25.0) == "strong"
    assert _classify_trend_strength(60.0) == "strong"


def test_classify_volatility_terciles() -> None:
    assert _classify_volatility(0.1) == "low"
    assert _classify_volatility(0.5) == "normal"
    assert _classify_volatility(0.9) == "high"
    # Exact tercile edges fall into the middle bucket.
    assert _classify_volatility(1 / 3) == "normal"
    assert _classify_volatility(2 / 3) == "normal"


# ---------------------------------------------------------------------------
# compute_regime_summary — happy path + derivation
# ---------------------------------------------------------------------------


def test_compute_regime_summary_uptrend_low_vol() -> None:
    bars = _uptrend_low_vol_bars()

    def fetch(symbol: str, asset_class: str, days: int) -> List[OHLCVBar]:
        return bars

    summary = compute_regime_summary(
        fetch, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )

    assert not summary.degraded
    assert summary.computed_at == _COMPUTED_AT
    assert len(summary.entries) == 1
    entry = summary.entries[0]
    assert entry.asset_class == "stocks"
    assert entry.benchmark_symbol == "SPY"
    assert entry.trend_direction == "up"
    assert entry.trend_strength == "strong"
    assert entry.volatility_regime == "low"
    assert 0.0 <= entry.atr_pct_percentile <= 1.0


def test_compute_regime_summary_downtrend() -> None:
    bars = _downtrend_bars()
    summary = compute_regime_summary(
        lambda *_a: bars, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    assert summary.entries[0].trend_direction == "down"


def test_compute_regime_summary_high_vol() -> None:
    bars = _uptrend_high_vol_bars()
    summary = compute_regime_summary(
        lambda *_a: bars, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    assert summary.entries[0].volatility_regime == "high"


def test_compute_regime_summary_multiple_benchmarks_default() -> None:
    """Default benchmark map classifies every asset class it can fetch."""
    bars = _uptrend_low_vol_bars()
    calls: List[str] = []

    def fetch(symbol: str, asset_class: str, days: int) -> List[OHLCVBar]:
        calls.append(symbol)
        return bars

    summary = compute_regime_summary(fetch, computed_at=_COMPUTED_AT)
    # Default map covers all five prompt classes: stocks (SPY), crypto
    # (BTC-USD), forex (EURUSD=X), futures (ES=F), commodities (GLD) — every
    # class a design attempt can be pinned to gets a regime read.
    expected = {"SPY", "BTC-USD", "EURUSD=X", "ES=F", "GLD"}
    assert {e.benchmark_symbol for e in summary.entries} == expected
    assert set(calls) == expected
    assert not summary.degraded


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------


def test_compute_regime_summary_fetch_raises_is_fail_open() -> None:
    def boom(symbol: str, asset_class: str, days: int) -> List[OHLCVBar]:
        raise RuntimeError("provider down")

    summary = compute_regime_summary(
        boom, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    assert summary.entries == []
    assert summary.degraded
    assert "SPY" in (summary.degraded_reason or "")


def test_compute_regime_summary_insufficient_bars_skips() -> None:
    short = _uptrend_low_vol_bars(n=_MIN_BARS - 1)
    summary = compute_regime_summary(
        lambda *_a: short, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    assert summary.entries == []
    assert summary.degraded
    assert "insufficient bars" in (summary.degraded_reason or "")


def test_compute_regime_summary_empty_bars_skips() -> None:
    summary = compute_regime_summary(
        lambda *_a: [], computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    assert summary.entries == []
    assert summary.degraded


def test_compute_regime_summary_flat_series_warmup_failure() -> None:
    """A perfectly flat series yields NaN ADX (no directional movement) →
    the benchmark is skipped via the warmup guard, not crashed."""
    flat = _make_bars([100.0] * 260, [0.0] * 260)
    summary = compute_regime_summary(
        lambda *_a: flat, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    assert summary.entries == []
    assert summary.degraded


def test_compute_regime_summary_nan_close_never_leaks() -> None:
    """A NaN close (gap/halt day) must never crash the summary or leak a NaN
    into a classified entry.

    In practice a NaN close poisons the SMA200/ADX warmup and the benchmark
    degrades cleanly; the ATR%-distribution filter (``c == c``) is the
    second line of defence so that, on any path where a benchmark IS still
    classified, no NaN slips into the percentile. Either way the result is
    well-formed: cleanly degraded, or a finite entry — never a NaN-valued one,
    never an exception."""
    bars = _uptrend_low_vol_bars()
    bars[30] = OHLCVBar(
        date="2025-01-031",
        open=float("nan"),
        high=bars[30].high,
        low=bars[30].low,
        close=float("nan"),
        volume=1000.0,
    )
    # Must not raise.
    summary = compute_regime_summary(
        lambda *_a: bars, computed_at=_COMPUTED_AT, benchmarks={"stocks": "SPY"}
    )
    for entry in summary.entries:
        # No NaN leaked into a classified entry.
        assert entry.atr_pct == entry.atr_pct
        assert 0.0 <= entry.atr_pct_percentile <= 1.0


def test_compute_regime_summary_partial_degrade() -> None:
    """One good benchmark + one failing benchmark → one entry, still degraded."""
    good = _uptrend_low_vol_bars()

    def fetch(symbol: str, asset_class: str, days: int) -> List[OHLCVBar]:
        if symbol == "SPY":
            return good
        raise RuntimeError("crypto feed down")

    summary = compute_regime_summary(
        fetch,
        computed_at=_COMPUTED_AT,
        benchmarks={"stocks": "SPY", "crypto": "BTC-USD"},
    )
    assert [e.benchmark_symbol for e in summary.entries] == ["SPY"]
    assert summary.degraded
    assert "BTC-USD" in (summary.degraded_reason or "")


def test_compute_regime_summary_validates_args() -> None:
    with pytest.raises(AssertionError):
        compute_regime_summary(lambda *_a: [], computed_at="")
    with pytest.raises(AssertionError):
        compute_regime_summary("not-callable", computed_at=_COMPUTED_AT)  # type: ignore[arg-type]
    with pytest.raises(AssertionError):
        compute_regime_summary(lambda *_a: [], computed_at=_COMPUTED_AT, days=0)


# ---------------------------------------------------------------------------
# regime_to_prompt_block
# ---------------------------------------------------------------------------


def _entry(**over) -> RegimeEntry:
    base = dict(
        asset_class="stocks",
        benchmark_symbol="SPY",
        trend_direction="up",
        trend_strength="strong",
        volatility_regime="low",
        close=500.0,
        sma50=490.0,
        sma200=470.0,
        adx=32.0,
        atr_pct=0.008,
        atr_pct_percentile=0.15,
    )
    base.update(over)
    return RegimeEntry(**base)


def test_regime_to_prompt_block_renders_lines() -> None:
    summary = RegimeSummary(computed_at=_COMPUTED_AT, entries=[_entry()])
    block = regime_to_prompt_block(summary)
    assert "stocks (SPY)" in block
    assert "trend=up (strong)" in block
    assert "volatility=low" in block


def test_regime_to_prompt_block_includes_degraded_note() -> None:
    summary = RegimeSummary(
        computed_at=_COMPUTED_AT,
        degraded=True,
        degraded_reason="could not classify: BTC-USD (feed down)",
        entries=[_entry()],
    )
    block = regime_to_prompt_block(summary)
    assert "partial regime snapshot" in block
    assert "BTC-USD" in block


def test_regime_to_prompt_block_empty_summary() -> None:
    summary = RegimeSummary(computed_at=_COMPUTED_AT, degraded=True, entries=[])
    assert regime_to_prompt_block(summary) == "No current market-regime read available."
