"""Deterministic synthetic OHLCV fixtures for trade-simulator regression tests."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Dict, List

from investment_team.market_data_service import OHLCVBar


def _weekday_dates(start: date, n_bars: int) -> List[date]:
    out: List[date] = []
    d = start
    while len(out) < n_bars:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bar_from_close(d: date, prev_close: float, close: float, rng: random.Random) -> OHLCVBar:
    intra = abs(close - prev_close) + prev_close * 0.002
    high = max(prev_close, close) + intra * rng.random()
    low = min(prev_close, close) - intra * rng.random()
    return OHLCVBar(
        date=d.isoformat(),
        open=round(prev_close, 4),
        high=round(high, 4),
        low=round(low, 4),
        close=round(close, 4),
        volume=round(1_000_000 + rng.random() * 500_000, 0),
    )


def sinusoidal_mean_reversion(
    symbol: str = "SIN",
    start: date = date(2020, 1, 1),
    n_bars: int = 260,
    base_price: float = 100.0,
    amplitude: float = 8.0,
    period: int = 20,
    seed: int = 1,
) -> List[OHLCVBar]:
    """A clean sinusoidal close path — good for mean-reversion logic."""
    rng = random.Random(seed)
    dates = _weekday_dates(start, n_bars)
    bars: List[OHLCVBar] = []
    prev_close = base_price
    for i, d in enumerate(dates):
        close = base_price + amplitude * math.sin(2 * math.pi * i / period)
        close += rng.gauss(0, 0.15)
        bars.append(_bar_from_close(d, prev_close, close, rng))
        prev_close = close
    return bars


def trending_with_jumps(
    symbol: str = "TRND",
    start: date = date(2020, 1, 1),
    n_bars: int = 260,
    base_price: float = 50.0,
    daily_drift: float = 0.0008,
    vol: float = 0.012,
    jump_prob: float = 0.01,
    jump_size: float = 0.05,
    seed: int = 2,
) -> List[OHLCVBar]:
    """Geometric brownian motion + occasional jumps — tests drawdown/vol."""
    rng = random.Random(seed)
    dates = _weekday_dates(start, n_bars)
    bars: List[OHLCVBar] = []
    prev_close = base_price
    for d in dates:
        shock = rng.gauss(daily_drift, vol)
        if rng.random() < jump_prob:
            shock += jump_size * rng.choice([-1, 1])
        close = max(1.0, prev_close * (1 + shock))
        bars.append(_bar_from_close(d, prev_close, close, rng))
        prev_close = close
    return bars


def build_fixture_universe() -> Dict[str, List[OHLCVBar]]:
    """Five deterministic symbols spanning two regimes."""
    return {
        "SIN1": sinusoidal_mean_reversion(seed=11, base_price=100, amplitude=8, period=20),
        "SIN2": sinusoidal_mean_reversion(seed=12, base_price=200, amplitude=15, period=25),
        "TRND1": trending_with_jumps(seed=21, base_price=50),
        "TRND2": trending_with_jumps(seed=22, base_price=80, daily_drift=-0.0003),
        "TRND3": trending_with_jumps(seed=23, base_price=25, vol=0.02),
    }
