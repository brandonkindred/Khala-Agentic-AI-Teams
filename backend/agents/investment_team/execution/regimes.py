"""Volatility-regime subwindows for the regime-conditional acceptance gate.

Two providers are exposed:

- :func:`realized_vol_quartile_subwindows` (default) — quartiles computed from
  the benchmark's rolling-window realized volatility. Network-free and
  deterministic; every unit test uses this.
- :func:`vix_quartile_subwindows` — accepts a pluggable ``vix_provider``
  callable so production deployments can swap in a real ``^VIX`` series
  (``STRATEGY_LAB_VIX_SOURCE=yahoo``) without changing callers.

Both return ``List[(regime_label, indices)]`` where ``indices`` are positions
into the return series for that subwindow. Indices are the 1-based positions
in the *daily-returns* array (i.e. aligned with ``equity[i+1]`` prices).
"""

from __future__ import annotations

import math
from datetime import date
from typing import Callable, List, Optional, Sequence, Tuple

REGIME_LABELS: Tuple[str, str, str, str] = (
    "vix_q1",
    "vix_q2",
    "vix_q3",
    "vix_q4",
)


def _std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _rolling_std(returns: Sequence[float], window: int) -> List[float]:
    """Trailing rolling std; fewer than ``window`` observations → 0."""
    out: List[float] = []
    for i in range(len(returns)):
        lo = max(0, i - window + 1)
        slice_ = returns[lo : i + 1]
        out.append(_std(slice_) if len(slice_) >= 2 else 0.0)
    return out


def _quartile_breakpoints(values: Sequence[float]) -> Tuple[float, float, float]:
    """Return ``(q1, q2, q3)`` breakpoints for the empirical distribution."""
    if not values:
        return (0.0, 0.0, 0.0)
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    q1 = sorted_vals[max(0, n // 4 - 1)]
    q2 = sorted_vals[max(0, n // 2 - 1)]
    q3 = sorted_vals[max(0, 3 * n // 4 - 1)]
    return (q1, q2, q3)


def realized_vol_quartile_subwindows(
    returns: Sequence[float],
    *,
    window: int = 21,
) -> List[Tuple[str, List[int]]]:
    """Classify each return index into a realized-vol quartile.

    Rolling std is computed with a trailing ``window`` (default 21 trading
    days ≈ 1 month). Breakpoints are the empirical quartiles of the populated
    rolling-std series. Returns an entry per regime label even when empty so
    downstream consumers can iterate a fixed schema.
    """
    n = len(returns)
    # Index ``window-1`` is the first position whose trailing slice is fully
    # populated, so we need ``n >= window`` — not ``n > window`` — before
    # classifying anything.
    if n < window:
        return [(label, []) for label in REGIME_LABELS]

    rolling = _rolling_std(returns, window=window)
    # Only index positions where the rolling window is fully populated are
    # eligible for regime assignment; earlier positions fall outside all bins.
    populated_slice = rolling[window - 1 :]
    q1, q2, q3 = _quartile_breakpoints(populated_slice)

    buckets: List[List[int]] = [[], [], [], []]
    for i in range(window - 1, n):
        s = rolling[i]
        if s <= q1:
            buckets[0].append(i)
        elif s <= q2:
            buckets[1].append(i)
        elif s <= q3:
            buckets[2].append(i)
        else:
            buckets[3].append(i)
    return list(zip(REGIME_LABELS, buckets))


VixProvider = Callable[[Sequence[date]], List[float]]


def vix_quartile_subwindows(
    dates: Sequence[date],
    benchmark_returns: Sequence[float],
    *,
    vix_provider: Optional[VixProvider] = None,
    window: int = 21,
) -> List[Tuple[str, List[int]]]:
    """Classify each return index into a VIX quartile.

    When ``vix_provider`` is provided, it is called with ``dates`` and must
    return a list of VIX levels aligned 1-to-1; regime assignment uses those
    levels directly. When ``vix_provider`` is ``None`` (the default), falls
    back to :func:`realized_vol_quartile_subwindows` on
    ``benchmark_returns`` — keeping tests network-free and production-
    deployments pluggable via the ``STRATEGY_LAB_VIX_SOURCE`` environment
    variable (wired in the orchestrator).
    """
    if vix_provider is None:
        return realized_vol_quartile_subwindows(benchmark_returns, window=window)

    vix_levels = list(vix_provider(list(dates)))
    if len(vix_levels) != len(dates):
        raise ValueError(f"vix_provider returned {len(vix_levels)} values, expected {len(dates)}")
    # Match realized-vol semantics: the first ``window-1`` indices are
    # unassigned (equivalent to "insufficient prior data" in the RV path),
    # but index ``window-1`` itself is classifiable when ``n >= window``.
    n = len(vix_levels)
    if n < window:
        return [(label, []) for label in REGIME_LABELS]

    populated_slice = vix_levels[window - 1 :]
    q1, q2, q3 = _quartile_breakpoints(populated_slice)
    buckets: List[List[int]] = [[], [], [], []]
    for i in range(window - 1, n):
        v = vix_levels[i]
        if v <= q1:
            buckets[0].append(i)
        elif v <= q2:
            buckets[1].append(i)
        elif v <= q3:
            buckets[2].append(i)
        else:
            buckets[3].append(i)
    return list(zip(REGIME_LABELS, buckets))


def regime_comparison(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    subwindows: Sequence[Tuple[str, Sequence[int]]],
) -> List[dict]:
    """Per-regime strategy-vs-benchmark comparison.

    Returns one dict per regime with ``{regime, trade_count,
    strategy_cumret, benchmark_cumret, beat_benchmark}``. ``beat_benchmark``
    is ``True`` when the strategy's cumulative return over the regime's
    return indices exceeds the benchmark's on the same indices.
    """
    out: List[dict] = []
    n = min(len(strategy_returns), len(benchmark_returns))
    for label, indices in subwindows:
        valid = [i for i in indices if 0 <= i < n]
        strat_cum = 1.0
        bench_cum = 1.0
        for i in valid:
            strat_cum *= 1.0 + strategy_returns[i]
            bench_cum *= 1.0 + benchmark_returns[i]
        out.append(
            {
                "regime": label,
                "n_obs": len(valid),
                "strategy_cumret": round(strat_cum - 1.0, 6),
                "benchmark_cumret": round(bench_cum - 1.0, 6),
                "beat_benchmark": strat_cum > bench_cum,
            }
        )
    return out
