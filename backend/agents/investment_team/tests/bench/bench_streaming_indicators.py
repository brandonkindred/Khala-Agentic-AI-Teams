"""Benchmark: streaming indicator registry vs. cold-start-per-bar.

Targets the ``500-bar × 10-indicator`` shape from the design discussion:
the legacy MACD template ran an outer
``for end in range(slow, len(bars) + 1)`` loop and recomputed both
windowed EMAs inside it on every call. At a 500-bar history that worked
out to ~18,000 EMA iterations per bar. The streaming registry maintains
the ``macd_line`` deque incrementally and single-steps from the cached
state, so per-bar cost stays ``O(slow + signal)``.

The hard assertion here is loose (≥ 3× speedup) to survive CI noise; the
issue's headline ≥10× target is exercised by the local-print path below.
Set ``BENCH_STREAMING_INDICATORS_VERBOSE=1`` to surface the printed
ratios when running locally.

Marked ``@pytest.mark.bench`` so the default suite skips it; opt in with
``pytest -m bench``.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import List

import pytest

from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry
from investment_team.strategy_lab.spec_dsl import IndicatorRef

pytestmark = pytest.mark.bench


@dataclass
class _Bar:
    timestamp: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1000.0


def _build_bars(n: int = 500, seed: int = 17) -> List[_Bar]:
    rng = random.Random(seed)
    bars: List[_Bar] = []
    for i in range(n):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.2
        spread = 0.5
        bars.append(
            _Bar(
                timestamp=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=close - 0.1,
                high=close + spread,
                low=close - spread,
                close=close,
            )
        )
    return bars


def _drive_streaming(bars: List[_Bar]) -> float:
    reg = IndicatorRegistry()
    t0 = time.perf_counter()
    for n in range(35, len(bars) + 1):
        sub = bars[:n]
        # 10 indicators on each bar — covers EMA, SMA, RSI, ATR, ADX,
        # Bollinger, Stochastic, VWAP, and MACD's three selects.
        reg.ema(sub, period=12)
        reg.sma(sub, period=20)
        reg.rsi(sub, period=14)
        reg.atr(sub, period=14)
        reg.adx(sub, period=14)
        reg.bollinger_bands(sub, period=20, select="middle")
        reg.stochastic(sub, k_period=14, d_period=3, select="k")
        reg.macd(sub, fast=12, slow=26, signal=9, select="signal")
        reg.macd(sub, fast=12, slow=26, signal=9, select="histogram")
        reg.vwap(sub)
    return time.perf_counter() - t0


def _drive_cold_start(bars: List[_Bar]) -> float:
    """Same workload, but reset state every bar so each call is a cold-start.

    Simulates the legacy "no caching" behaviour without re-shipping the
    full O(N²) template — the registry's cold-start path runs the exact
    same outer loop the legacy template did for MACD.
    """
    reg = IndicatorRegistry()
    t0 = time.perf_counter()
    for n in range(35, len(bars) + 1):
        sub = bars[:n]
        reg._state.clear()
        reg.ema(sub, period=12)
        reg.sma(sub, period=20)
        reg.rsi(sub, period=14)
        reg.atr(sub, period=14)
        reg.adx(sub, period=14)
        reg.bollinger_bands(sub, period=20, select="middle")
        reg.stochastic(sub, k_period=14, d_period=3, select="k")
        reg.macd(sub, fast=12, slow=26, signal=9, select="signal")
        reg.macd(sub, fast=12, slow=26, signal=9, select="histogram")
        reg.vwap(sub)
    return time.perf_counter() - t0


def test_streaming_beats_cold_start_on_500_bars() -> None:
    """10-indicator workload on a 500-bar history beats the cold-start path.

    The mixed workload includes per-bar O(period) indicators (RSI, ATR,
    ADX) whose cost the registry can deduplicate but cannot reduce
    asymptotically, so the realised speedup here is lower than the
    MACD-only ratio.
    """
    bars = _build_bars(n=500)
    streaming_t = _drive_streaming(bars)
    cold_t = _drive_cold_start(bars)
    ratio = cold_t / max(streaming_t, 1e-9)
    if os.environ.get("BENCH_STREAMING_INDICATORS_VERBOSE"):
        print(
            f"\nmixed 10-indicator workload (500 bars): "
            f"streaming={streaming_t * 1000:7.1f} ms   "
            f"cold-start={cold_t * 1000:7.1f} ms   "
            f"speedup={ratio:6.2f}x"
        )
    # Hard floor — leaves headroom for slow CI.
    assert ratio > 3.0, (
        f"streaming-vs-cold-start speedup too small: {ratio:.2f}x "
        f"(streaming={streaming_t * 1000:.1f}ms, cold={cold_t * 1000:.1f}ms)"
    )


def test_macd_streaming_hits_headline_speedup_target() -> None:
    """MACD-only 500-bar benchmark must hit the headline ≥10× target.

    The legacy MACD template ran an outer
    ``for end in range(slow, len(bars) + 1)`` loop and recomputed both
    windowed EMAs inside it — the recurrence cost scaled with the size
    of ``bars``. The streaming registry single-steps from cached state
    once warmed up, so per-bar cost is bounded by ``fast + slow`` instead
    of ``(N - slow) × (fast + slow)``.
    """
    bars = _build_bars(n=500)

    reg_streaming = IndicatorRegistry()
    t0 = time.perf_counter()
    for n in range(35, len(bars) + 1):
        reg_streaming.macd(bars[:n], fast=12, slow=26, signal=9, select="signal")
    streaming_t = time.perf_counter() - t0

    reg_cold = IndicatorRegistry()
    t0 = time.perf_counter()
    for n in range(35, len(bars) + 1):
        reg_cold._state.clear()
        reg_cold.macd(bars[:n], fast=12, slow=26, signal=9, select="signal")
    cold_t = time.perf_counter() - t0

    ratio = cold_t / max(streaming_t, 1e-9)
    if os.environ.get("BENCH_STREAMING_INDICATORS_VERBOSE"):
        print(
            f"\nMACD-only (500 bars): "
            f"streaming={streaming_t * 1000:7.1f} ms   "
            f"cold-start={cold_t * 1000:7.1f} ms   "
            f"speedup={ratio:6.2f}x"
        )
    # 10× is the acceptance criterion; CI noise headroom takes us to 8×
    # as the hard floor. Local runs typically observe 30-60×.
    assert ratio > 8.0, (
        f"MACD speedup too small: {ratio:.2f}x "
        f"(streaming={streaming_t * 1000:.1f}ms, cold={cold_t * 1000:.1f}ms)"
    )


_VIEW_REFS = [
    IndicatorRef(name="ema", params={"period": 12}),
    IndicatorRef(name="sma", params={"period": 20}),
    IndicatorRef(name="rsi", params={"period": 14}),
    IndicatorRef(name="atr", params={"period": 14}),
    IndicatorRef(name="adx", params={"period": 14}),
    IndicatorRef(name="bollinger", params={"period": 20, "band": "middle"}),
    IndicatorRef(name="stochastic", params={"output": "k"}),
    IndicatorRef(name="macd", params={"output": "signal"}),
    IndicatorRef(name="macd", params={"output": "histogram"}),
    IndicatorRef(name="vwap", params={}),
]

_MACD_VIEW_REFS = [
    IndicatorRef(name="macd", params={"output": "signal"}),
    IndicatorRef(name="macd", params={"output": "histogram"}),
]


def _measure_view_window(
    total_bars: int, lo: int, hi: int, max_bars: int = 500, refs: List[IndicatorRef] = _VIEW_REFS
) -> float:
    """Drive a view over ``total_bars`` and time the per-bar reads in ``[lo, hi)``.

    Each measured bar reads every ref in ``refs`` at the trailing bar AND ``i - 1``
    (the ``cross_*`` shape), the realistic engine workload. Defaults to all ten
    ``_VIEW_REFS``; pass a narrower list to isolate a single indicator's cost.
    """
    view = StreamingHistoryView(max_bars=max_bars)
    rng = random.Random(23)
    elapsed = 0.0
    for i in range(total_bars):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.2
        view.append(
            BarRecord(
                timestamp=f"d{i}",
                open=close - 0.1,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=1000.0,
            )
        )
        trailing = view.length() - 1
        measure = lo <= i < hi
        t0 = time.perf_counter() if measure else 0.0
        for ref in refs:
            view.indicator(ref, trailing)
            if trailing > 0:
                view.indicator(ref, trailing - 1)
        if measure:
            elapsed += time.perf_counter() - t0
    return elapsed


def test_streaming_view_per_bar_cost_is_flat_in_history() -> None:
    """The engine's per-bar indicator cost must NOT grow with how many bars
    have streamed through — the issue's "O(1) amortised, no full-deque recompute
    per bar" criterion. Both measured windows are past the ``max_bars`` cap, so
    the deque and every scalar buffer are at steady-state size; a regression to
    a per-bar full-window recompute (or an unbounded buffer) would make the late
    window materially slower than the early one.
    """
    window = 100
    early = _measure_view_window(2200, 600, 600 + window)
    late = _measure_view_window(2200, 2100, 2100 + window)
    ratio = late / max(early, 1e-9)
    if os.environ.get("BENCH_STREAMING_INDICATORS_VERBOSE"):
        print(
            f"\nStreamingHistoryView per-bar cost (10 indicators x i/i-1, 100 bars): "
            f"early[600:700]={early * 1000:6.1f} ms   "
            f"late[2100:2200]={late * 1000:6.1f} ms   "
            f"late/early={ratio:5.2f}x"
        )
    # Flat within noise: late steady-state cost stays close to the early one.
    # A per-bar O(history) regression would blow this well past 2x.
    assert ratio < 2.0, (
        f"per-bar cost grew with history: late/early={ratio:.2f}x "
        f"(early={early * 1000:.1f}ms, late={late * 1000:.1f}ms) — "
        "the engine view should be O(window), independent of bars seen"
    )


def test_macd_view_per_bar_cost_is_flat_in_history() -> None:
    """MACD signal/histogram reads alone must be O(1)-amortized per bar.

    Isolates the MACD selects from ``test_streaming_view_per_bar_cost_is_flat_in_history``'s
    mixed 10-indicator workload — the incremental signal-line EMA step (see
    ``IndicatorRegistry._macd_value``) must keep per-bar cost flat as total
    history grows, consistent with the other streaming indicators (OBV, MFI,
    Bollinger, etc.) that already maintain O(1)-amortized state instead of
    re-walking the full ``macd_line`` deque on every read.

    Uses a ``max_bars`` at least as large as ``total_bars`` so the view's
    bars deque never hits its retention cap and every appended bar stays an
    ``expand`` step — the transition the incremental fix actually targets.
    ``test_streaming_view_per_bar_cost_is_flat_in_history`` uses the default
    ``max_bars=500`` and measures windows starting at bar 600, so once past
    that cap every subsequent bar is a ``slide`` (fixed-size window,
    deliberately a full re-walk — see ``_macd_value``'s ``slide`` branch);
    that path's cost is bounded by window depth regardless of the ``expand``
    optimization, so it would stay flat even without this fix. Only an
    unbounded, ever-growing ``macd_line`` (i.e. ``expand`` throughout)
    exercises the O(N) regression this benchmark guards against.
    """
    window = 100
    total_bars = 2200
    early = _measure_view_window(total_bars, 600, 600 + window, max_bars=total_bars, refs=_MACD_VIEW_REFS)
    late = _measure_view_window(total_bars, 2100, 2100 + window, max_bars=total_bars, refs=_MACD_VIEW_REFS)
    ratio = late / max(early, 1e-9)
    if os.environ.get("BENCH_STREAMING_INDICATORS_VERBOSE"):
        print(
            f"\nMACD signal/histogram per-bar cost (100 bars): "
            f"early[600:700]={early * 1000:6.1f} ms   "
            f"late[2100:2200]={late * 1000:6.1f} ms   "
            f"late/early={ratio:5.2f}x"
        )
    # Flat within noise: late steady-state cost stays close to the early one.
    # A regression to a per-bar full-macd_line-deque recompute would blow this
    # well past 2x as history accumulates.
    assert ratio < 2.0, (
        f"MACD per-bar cost grew with history: late/early={ratio:.2f}x "
        f"(early={early * 1000:.1f}ms, late={late * 1000:.1f}ms) — "
        "MACD signal/histogram reads should be O(1)-amortized, independent of bars seen"
    )
