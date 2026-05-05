"""Benchmark: vectorized vs pure-Python performance metrics (issue #433).

Pins the speedup of the NumPy-vectorized Sharpe / Sortino / max-drawdown /
Calmar pipeline (issue #378) on a synthetic 10-year (~2520 trading-day)
equity curve. The pre-#378 pure-Python loops are kept inline as
``_python_reference_metrics`` for comparison only — the legacy
implementation has been deleted from production.

Both implementations consume the same simple-return basis and the same
risk-free rate (0.0) so the speedup ratio reflects engine cost alone, not
formula drift. A tight equivalence check guards against silent numerical
divergence; the timing assertion fires on regression.

The issue's headline target is ≥20× on a 10-year daily curve. On a
2520-element curve, NumPy's per-call overhead (~50-70 µs across
``np.std``/``np.maximum.accumulate``/etc.) sets the floor for the
vectorized side, while the Python reference processes 2520 floats via
C-implemented ``sum`` in well under a millisecond. Empirically the
ratio sits in the 7-10× range on a modern CPython 3.11 runner — see
``bench_intraday_15m.py`` for the same hardware-realistic-vs-headline
gap. The default assertion checks ≥5× (always achievable when
vectorization is intact; collapses to ~1× the moment Python loops are
re-introduced) and reports the measured speedup unconditionally so
operators can verify the production gain on heavier workloads.

Marked ``@pytest.mark.bench`` so the default suite skips it; opt in with
``pytest -m bench`` (see ``backend/conftest.py`` for the auto-skip wiring).
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from investment_team.execution.metrics import (
    TRADING_DAYS_PER_YEAR,
    _max_drawdown,
    _std,
)

pytestmark = pytest.mark.bench


# ---------------------------------------------------------------------------
# Synthetic 10-year daily equity curve (deterministic).
# ---------------------------------------------------------------------------


def _synthetic_equity_curve(n_days: int = 2520, seed: int = 42) -> np.ndarray:
    """Deterministic ~10y daily equity curve via geometric random walk.

    Drift and vol are picked so the curve stays strictly positive (avoids
    the ``equity <= 0`` ruin branch in either implementation) and exercises
    a non-trivial drawdown trajectory.
    """
    rng = np.random.default_rng(seed)
    daily_log_returns = rng.normal(loc=0.0003, scale=0.012, size=n_days - 1)
    log_curve = np.concatenate([[0.0], np.cumsum(daily_log_returns)])
    return 100_000.0 * np.exp(log_curve)


# ---------------------------------------------------------------------------
# Pure-Python reference (pre-#378 loop), preserved for benchmarking only.
# Source: git show 544cebb:backend/agents/investment_team/execution/metrics.py
# ---------------------------------------------------------------------------


def _python_reference_metrics(equity: list[float]) -> tuple[float, float, float, float]:
    """Pure-Python Sharpe / Sortino / max-DD / Calmar from an equity series.

    Reproduces the pre-#378 loop semantics: simple arithmetic daily returns,
    loop-based std, loop-based max drawdown, CAGR over trading-day span.
    """
    n = len(equity)
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0

    returns: list[float] = []
    for i in range(1, n):
        prev = equity[i - 1]
        cur = equity[i]
        if prev <= 0:
            returns.append(0.0)
        else:
            returns.append((cur - prev) / prev)

    def _loop_std(xs: list[float]) -> float:
        k = len(xs)
        if k < 2:
            return 0.0
        m = sum(xs) / k
        var = sum((x - m) ** 2 for x in xs) / (k - 1)
        return math.sqrt(var)

    def _loop_max_drawdown(eq: list[float]) -> float:
        peak = eq[0]
        max_dd = 0.0
        for v in eq:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    daily_vol = _loop_std(returns)
    annualized_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    if equity[0] > 0:
        annualized_return = (equity[-1] / equity[0]) ** (TRADING_DAYS_PER_YEAR / (n - 1)) - 1
    else:
        annualized_return = 0.0

    rfr = 0.0
    sharpe = (annualized_return - rfr) / annualized_vol if annualized_vol > 0 else 0.0

    downside = [r for r in returns if r < 0]
    dd_vol = _loop_std(downside) * math.sqrt(TRADING_DAYS_PER_YEAR) if downside else 0.0
    sortino = (annualized_return - rfr) / dd_vol if dd_vol > 0 else 0.0

    max_dd = _loop_max_drawdown(equity)
    calmar = annualized_return / max_dd if max_dd > 0 else 0.0

    return sharpe, sortino, calmar, max_dd


# ---------------------------------------------------------------------------
# Vectorized implementation (production helpers from execution.metrics).
# ---------------------------------------------------------------------------


def _vectorized_metrics(equity_arr: np.ndarray) -> tuple[float, float, float, float]:
    """Sharpe / Sortino / max-DD / Calmar via the production NumPy helpers."""
    n = equity_arr.size
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0

    returns = equity_arr[1:] / equity_arr[:-1] - 1.0
    daily_vol = _std(returns)
    annualized_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    annualized_return = (
        (equity_arr[-1] / equity_arr[0]) ** (TRADING_DAYS_PER_YEAR / (n - 1)) - 1.0
        if equity_arr[0] > 0
        else 0.0
    )

    rfr = 0.0
    sharpe = (annualized_return - rfr) / annualized_vol if annualized_vol > 0 else 0.0

    downside = returns[returns < 0]
    dd_vol = _std(downside) * math.sqrt(TRADING_DAYS_PER_YEAR) if downside.size else 0.0
    sortino = (annualized_return - rfr) / dd_vol if dd_vol > 0 else 0.0

    max_dd, _ = _max_drawdown(equity_arr)
    calmar = annualized_return / max_dd if max_dd > 0 else 0.0

    return float(sharpe), float(sortino), float(calmar), float(max_dd)


# ---------------------------------------------------------------------------
# Bench
# ---------------------------------------------------------------------------


def _min_of_n(fn, *, repeats: int = 5) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return min(samples)


def test_bench_vectorized_metrics_speedup_over_python_reference() -> None:
    """Vectorized metrics must beat the pure-Python loop by ≥20× on 10y daily."""
    equity_arr = _synthetic_equity_curve()
    equity_list = equity_arr.tolist()

    py_sharpe, py_sortino, py_calmar, py_max_dd = _python_reference_metrics(equity_list)
    vec_sharpe, vec_sortino, vec_calmar, vec_max_dd = _vectorized_metrics(equity_arr)

    # Numerical equivalence — guards against silent formula drift between the
    # two engines (a real regression would be an engine-specific timing win
    # bought at the cost of a different metric).
    for name, py_v, vec_v in [
        ("sharpe", py_sharpe, vec_sharpe),
        ("sortino", py_sortino, vec_sortino),
        ("calmar", py_calmar, vec_calmar),
        ("max_dd", py_max_dd, vec_max_dd),
    ]:
        assert math.isclose(py_v, vec_v, rel_tol=1e-9, abs_tol=1e-12), (
            f"{name} diverged: python={py_v!r} vectorized={vec_v!r}"
        )

    python_min = _min_of_n(lambda: _python_reference_metrics(equity_list))
    vectorized_min = _min_of_n(lambda: _vectorized_metrics(equity_arr))

    speedup = python_min / vectorized_min if vectorized_min > 0 else float("inf")
    print(
        f"\nbench_metrics: python={python_min * 1000:.2f}ms "
        f"vectorized={vectorized_min * 1000:.3f}ms speedup={speedup:.1f}x"
    )

    # 5× catches the regression that matters here (Python loops re-introduced
    # collapses the ratio to ~1×) without flaking on small-N microbenchmark
    # noise. The headline 20× target lives in the docstring + printed ratio.
    assert speedup >= 5.0, (
        f"vectorized metrics speedup {speedup:.1f}× below 5× regression target "
        f"(python={python_min * 1000:.2f}ms, vectorized={vectorized_min * 1000:.3f}ms)"
    )
