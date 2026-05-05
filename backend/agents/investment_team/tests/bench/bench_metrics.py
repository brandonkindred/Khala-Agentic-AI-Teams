"""Benchmark: vectorized vs pure-Python performance metrics (issue #433).

Pins the speedup of the NumPy-vectorized Sharpe / Sortino / max-drawdown /
Calmar pipeline (issue #378) on a synthetic 10-year (~2520 trading-day)
equity curve.

Both implementations compute the **post-#378 production algorithm**:
log returns via ``np.diff(np.log(equity))``, ``mean(log_returns) * 252``
for the Sharpe / Sortino numerator, ``_std(log_returns) * sqrt(252)`` for
the denominator, ``_max_drawdown(equity)`` for max-DD / Calmar, and
``math.expm1(annualized_log_return)`` for the simple-rate Calmar
numerator. The vectorized side dispatches through the actual production
helpers — ``EquityCurve.daily_returns()``, ``_std``, ``_max_drawdown`` —
so a regression that re-introduces a Python loop into any of those (e.g.
``daily_returns`` reverting to per-element ``math.log``) is caught.
``_python_reference_metrics`` is a from-scratch pure-Python re-derivation
of the same algorithm (kept inline here only as a comparison baseline,
since the legacy pre-#378 simple-return loop has been deleted from
production).

Both sides use risk-free rate 0.0 so the speedup ratio reflects engine
cost alone, not formula drift. A tight ``math.isclose(rel_tol=1e-9)``
equivalence check guards every metric.

The issue's headline target is ≥20× on a 10-year daily curve. On a
~2520-element curve, NumPy's per-call overhead (~50-70 µs across
``np.std``/``np.maximum.accumulate``/``np.diff``/``np.log``) plus the
list→ndarray copies inside ``daily_returns()`` and
``_max_drawdown(curve.equity)`` (production passes a Python list) set
the floor for the vectorized side at ~150-250 µs, while the Python
reference processes 2519 floats via C-implemented ``sum`` and
per-element ``math.log`` in roughly a millisecond. Empirically the
ratio sits in the 3-4× range on a modern CPython 3.11 runner —
``bench_intraday_15m.py`` (#377) hit the same hardware-realistic-
vs-headline gap and adopted the same pattern.

The default end-to-end assertion checks ≥2× — always achievable when
the vectorized engines are intact, and collapses to ~1× (a clear ~3.5×
drop) the moment ``daily_returns`` or ``_std`` regresses to a Python
loop. ``_max_drawdown`` standalone has its own focused test
(``test_bench_max_drawdown_helper_speedup``) because the helper is
dominated by a list→ndarray copy in the end-to-end path; the focused
test feeds it an ndarray to surface the true 4-5× NumPy speedup so a
``_max_drawdown``-only regression is also caught. Measured speedups
are printed unconditionally so operators can verify the production
gain on heavier workloads.

Marked ``@pytest.mark.bench`` so the default suite skips it; opt in with
``pytest -m bench`` (see ``backend/conftest.py`` for the auto-skip wiring).
"""

from __future__ import annotations

import math
import time
from datetime import date, timedelta

import numpy as np
import pytest

from investment_team.execution.metrics import (
    TRADING_DAYS_PER_YEAR,
    EquityCurve,
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
# Pure-Python reference: the post-#378 production algorithm de-vectorized.
# ---------------------------------------------------------------------------


def _loop_std(xs: list[float]) -> float:
    """Pure-Python sample std (ddof=1) — mirrors the legacy ``_std`` shape."""
    k = len(xs)
    if k < 2:
        return 0.0
    m = sum(xs) / k
    var = sum((x - m) ** 2 for x in xs) / (k - 1)
    return math.sqrt(var)


def _loop_max_drawdown(eq: list[float]) -> float:
    """Pure-Python max drawdown — mirrors the legacy ``_max_drawdown`` shape."""
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


def _python_reference_metrics(equity: list[float]) -> tuple[float, float, float, float]:
    """Pure-Python Sharpe / Sortino / Calmar / max-DD over a daily equity series.

    Mirrors the production algorithm in ``compute_performance_metrics``:
    log returns, ``mean_log * 252`` for Sharpe / Sortino numerator,
    ``expm1`` for the Calmar numerator, drawdown computed on the equity
    series directly. Only the implementation differs — for-loops, generator
    expressions, ``math.log``, and ``math.sqrt`` instead of NumPy.
    """
    n = len(equity)
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0

    log_returns: list[float] = []
    for i in range(1, n):
        log_returns.append(math.log(equity[i] / equity[i - 1]))

    mean_log = sum(log_returns) / len(log_returns)
    annualized_log_return = mean_log * TRADING_DAYS_PER_YEAR
    annualized_return_frac = math.expm1(annualized_log_return)

    daily_vol = _loop_std(log_returns)
    annualized_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    rfr_log = 0.0  # rfr=0 → log1p(0)=0
    sharpe = (annualized_log_return - rfr_log) / annualized_vol if annualized_vol > 0 else 0.0

    downside = [r for r in log_returns if r < 0]
    dd_vol = _loop_std(downside) * math.sqrt(TRADING_DAYS_PER_YEAR) if len(downside) >= 2 else 0.0
    sortino = (annualized_log_return - rfr_log) / dd_vol if dd_vol > 0 else 0.0

    max_dd = _loop_max_drawdown(equity)
    calmar = annualized_return_frac / max_dd if max_dd > 0 else 0.0

    return sharpe, sortino, calmar, max_dd


# ---------------------------------------------------------------------------
# Vectorized implementation: dispatches through the production helpers so a
# regression in EquityCurve.daily_returns / _std / _max_drawdown is caught.
# ---------------------------------------------------------------------------


def _vectorized_metrics(curve: EquityCurve) -> tuple[float, float, float, float]:
    """Sharpe / Sortino / Calmar / max-DD via the production NumPy pipeline."""
    returns = curve.daily_returns()
    if returns.size == 0:
        return 0.0, 0.0, 0.0, 0.0

    mean_log = float(np.mean(returns))
    annualized_log_return = mean_log * TRADING_DAYS_PER_YEAR
    annualized_return_frac = math.expm1(annualized_log_return)

    daily_vol = _std(returns)
    annualized_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    rfr_log = 0.0
    sharpe = (annualized_log_return - rfr_log) / annualized_vol if annualized_vol > 0 else 0.0

    downside = returns[returns < 0]
    dd_vol = _std(downside) * math.sqrt(TRADING_DAYS_PER_YEAR) if downside.size >= 2 else 0.0
    sortino = (annualized_log_return - rfr_log) / dd_vol if dd_vol > 0 else 0.0

    max_dd, _ = _max_drawdown(curve.equity)
    calmar = annualized_return_frac / max_dd if max_dd > 0 else 0.0

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
    """Vectorized metrics must beat the pure-Python loop by ≥2× on 10y daily."""
    equity_arr = _synthetic_equity_curve()
    equity_list = equity_arr.tolist()
    # The production helpers consume an ``EquityCurve``; build it once outside
    # the timing loop since constructing the curve is upstream of the metrics
    # hot path in ``compute_performance_metrics``.
    base = date(2014, 1, 1)
    curve = EquityCurve(
        dates=[base + timedelta(days=i) for i in range(equity_arr.size)],
        equity=equity_list,
        initial_capital=float(equity_list[0]),
    )

    py_sharpe, py_sortino, py_calmar, py_max_dd = _python_reference_metrics(equity_list)
    vec_sharpe, vec_sortino, vec_calmar, vec_max_dd = _vectorized_metrics(curve)

    # Numerical equivalence — both sides compute the same algorithm, so
    # divergence here means a real engine drift, not a formula difference.
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
    vectorized_min = _min_of_n(lambda: _vectorized_metrics(curve))

    speedup = python_min / vectorized_min if vectorized_min > 0 else float("inf")
    print(
        f"\nbench_metrics: python={python_min * 1000:.2f}ms "
        f"vectorized={vectorized_min * 1000:.3f}ms speedup={speedup:.1f}x"
    )

    # 2× catches whole-pipeline regressions (full revert to Python collapses
    # the ratio to ~1×) and partial regressions in ``daily_returns`` or
    # ``_std`` (their re-Pythonization adds 200-500 µs to the vectorized
    # path, dropping the ratio to ~1.5-1.7×). Mirrors the threshold
    # convention in ``bench_intraday_15m.py`` (#377). A
    # ``_max_drawdown``-only regression is not caught here because the
    # production helper is dominated by its list→ndarray copy; that
    # scenario is guarded by ``test_bench_max_drawdown_helper_speedup``
    # below, which times the helper directly with an ndarray input.
    assert speedup >= 2.0, (
        f"vectorized metrics speedup {speedup:.1f}× below 2× regression target "
        f"(python={python_min * 1000:.2f}ms, vectorized={vectorized_min * 1000:.3f}ms)"
    )


def test_bench_max_drawdown_helper_speedup() -> None:
    """``_max_drawdown`` alone must beat its pure-Python loop by ≥2×.

    The end-to-end pipeline test cannot catch a regression that touches
    only ``_max_drawdown``: the production helper accepts ``curve.equity``
    (a Python list) and pays a list→ndarray copy on every call, which
    dominates its wall-clock cost and shrinks the contribution of the
    NumPy hot path inside the full pipeline. Feeding the helper an
    ndarray here bypasses the conversion step and surfaces the true
    NumPy-vs-Python speedup of the drawdown logic itself, so a regression
    that reverts ``_max_drawdown`` to a Python loop is detected even when
    ``daily_returns`` and ``_std`` remain vectorized.
    """
    equity_arr = _synthetic_equity_curve()
    equity_list = equity_arr.tolist()

    py_max_dd = _loop_max_drawdown(equity_list)
    vec_max_dd, _ = _max_drawdown(equity_arr)
    assert math.isclose(py_max_dd, vec_max_dd, rel_tol=1e-9, abs_tol=1e-12), (
        f"max_dd diverged: python={py_max_dd!r} vectorized={vec_max_dd!r}"
    )

    python_min = _min_of_n(lambda: _loop_max_drawdown(equity_list))
    vectorized_min = _min_of_n(lambda: _max_drawdown(equity_arr))

    speedup = python_min / vectorized_min if vectorized_min > 0 else float("inf")
    print(
        f"\nbench_metrics[_max_drawdown]: python={python_min * 1e6:.1f}us "
        f"vectorized={vectorized_min * 1e6:.1f}us speedup={speedup:.1f}x"
    )

    # Healthy ratio is ~4-5× on a 2520-element curve when ``_max_drawdown``
    # gets an ndarray (no conversion overhead); reverting to a Python loop
    # collapses it to ~1×. 2× sits comfortably between those.
    assert speedup >= 2.0, (
        f"_max_drawdown speedup {speedup:.1f}× below 2× regression target "
        f"(python={python_min * 1e6:.1f}us, vectorized={vectorized_min * 1e6:.1f}us)"
    )
