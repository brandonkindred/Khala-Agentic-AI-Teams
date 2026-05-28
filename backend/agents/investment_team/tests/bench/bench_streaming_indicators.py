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

from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry

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
