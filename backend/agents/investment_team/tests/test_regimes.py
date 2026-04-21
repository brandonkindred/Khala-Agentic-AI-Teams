"""Regime subwindow + 60/40 benchmark synthesis tests (issue #247, step 6)."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from investment_team.execution.benchmarks import (
    build_60_40_equity,
    build_weighted_blend_equity,
)
from investment_team.execution.regimes import (
    REGIME_LABELS,
    realized_vol_quartile_subwindows,
    regime_comparison,
    vix_quartile_subwindows,
)

# ---------------------------------------------------------------------------
# Realized-vol quartile provider
# ---------------------------------------------------------------------------


def test_realized_vol_returns_four_labelled_buckets():
    rng = random.Random(1)
    returns = [rng.gauss(0, 0.01) for _ in range(252)]
    subs = realized_vol_quartile_subwindows(returns, window=21)
    assert [label for label, _ in subs] == list(REGIME_LABELS)


def test_realized_vol_buckets_partition_eligible_indices():
    rng = random.Random(2)
    returns = [rng.gauss(0, 0.01) for _ in range(252)]
    subs = realized_vol_quartile_subwindows(returns, window=21)
    all_idx = sorted(i for _, idx in subs for i in idx)
    # Every index from window-1 .. n-1 should be in exactly one bucket.
    assert all_idx == list(range(20, 252))
    # No duplicates.
    assert len(all_idx) == 252 - 20


def test_realized_vol_empty_bucket_list_when_series_shorter_than_window():
    subs = realized_vol_quartile_subwindows([0.01, 0.02, 0.03], window=21)
    assert [label for label, _ in subs] == list(REGIME_LABELS)
    assert all(len(idx) == 0 for _, idx in subs)


def test_realized_vol_classifies_minimum_valid_window():
    # A series of exactly ``window`` observations has one fully-populated
    # trailing-std value at index ``window-1``. The regime classifier must
    # assign that index rather than dropping it (PR #271 review: off-by-one).
    returns = [0.01 * i for i in range(21)]
    subs = dict(realized_vol_quartile_subwindows(returns, window=21))
    all_idx = sorted(i for idx in subs.values() for i in idx)
    assert all_idx == [20]


def test_realized_vol_calmer_regime_gets_lower_bucket():
    # Vol increases monotonically across the span, so the earliest eligible
    # indices should land in q1 and the latest in q4.
    rng = random.Random(9)
    returns: list[float] = []
    for i in range(252):
        scale = 0.001 + (i / 252.0) * 0.03
        returns.append(rng.gauss(0.0, scale))
    subs = dict(realized_vol_quartile_subwindows(returns, window=21))

    # All four regimes must contain indices for a median comparison.
    assert all(len(subs[label]) > 0 for label in REGIME_LABELS)

    q1_median = sorted(subs["vix_q1"])[len(subs["vix_q1"]) // 2]
    q4_median = sorted(subs["vix_q4"])[len(subs["vix_q4"]) // 2]
    assert q1_median < q4_median


# ---------------------------------------------------------------------------
# VIX provider (pluggable)
# ---------------------------------------------------------------------------


def test_vix_quartile_falls_back_to_realized_vol_when_no_provider():
    rng = random.Random(3)
    returns = [rng.gauss(0, 0.01) for _ in range(252)]
    dates = [date(2022, 1, 3) + timedelta(days=i) for i in range(252)]
    subs_vix = vix_quartile_subwindows(dates, returns, window=21)
    subs_rv = realized_vol_quartile_subwindows(returns, window=21)
    assert subs_vix == subs_rv


def test_vix_quartile_uses_provider_output():
    dates = [date(2022, 1, 3) + timedelta(days=i) for i in range(100)]

    # Monotonically increasing VIX means all late indices go to q4.
    def provider(ds):
        return [float(i) for i in range(len(ds))]

    subs = dict(vix_quartile_subwindows(dates, [0.0] * 100, vix_provider=provider, window=21))
    # Highest quartile should contain the tail of the series.
    assert max(subs["vix_q4"]) == 99
    # Lowest quartile should contain the earliest eligible indices.
    assert min(subs["vix_q1"]) == 20


def test_vix_quartile_classifies_minimum_valid_window():
    # Same off-by-one fix applies to the VIX path.
    dates = [date(2022, 1, 3) + timedelta(days=i) for i in range(21)]

    def provider(ds):
        return [float(i) for i in range(len(ds))]

    subs = dict(vix_quartile_subwindows(dates, [0.0] * 21, vix_provider=provider, window=21))
    all_idx = sorted(i for idx in subs.values() for i in idx)
    assert all_idx == [20]


def test_vix_provider_length_mismatch_raises():
    dates = [date(2022, 1, 3) + timedelta(days=i) for i in range(50)]
    with pytest.raises(ValueError, match="expected 50"):
        vix_quartile_subwindows(dates, [0.0] * 50, vix_provider=lambda ds: [1.0] * 25, window=21)


# ---------------------------------------------------------------------------
# Regime comparison
# ---------------------------------------------------------------------------


def test_regime_comparison_flags_strategy_outperforming_benchmark():
    # Strategy: +1% per day. Benchmark: 0% per day.
    strat = [0.01] * 100
    bench = [0.0] * 100
    subs = [("vix_q1", [10, 20, 30]), ("vix_q4", [50, 60, 70])]
    rows = regime_comparison(strat, bench, subs)
    assert all(r["beat_benchmark"] for r in rows)
    assert all(r["strategy_cumret"] > r["benchmark_cumret"] for r in rows)


def test_regime_comparison_flags_underperformance():
    strat = [-0.005] * 100
    bench = [0.005] * 100
    subs = [("vix_q1", [10, 20, 30])]
    rows = regime_comparison(strat, bench, subs)
    assert not rows[0]["beat_benchmark"]


def test_regime_comparison_skips_out_of_range_indices():
    strat = [0.01] * 10
    bench = [0.0] * 10
    subs = [("vix_q1", [0, 5, 999, -1])]
    rows = regime_comparison(strat, bench, subs)
    assert rows[0]["n_obs"] == 2


# ---------------------------------------------------------------------------
# 60/40 benchmark synthesis
# ---------------------------------------------------------------------------


def test_build_60_40_equity_compounds_returns_correctly():
    # SPY: +1% every day. AGG: 0% every day. 60/40 blend: 0.6% per day.
    spy = [100.0 * (1.01) ** i for i in range(5)]
    agg = [100.0] * 5
    out = build_60_40_equity(spy, agg, initial_capital=1000.0)
    expected = [1000.0 * (1.006) ** i for i in range(5)]
    for a, e in zip(out, expected):
        assert abs(a - e) < 1e-6


def test_build_weighted_blend_rejects_weights_not_summing_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        build_weighted_blend_equity([100.0, 101.0], [100.0, 100.0], weights=(0.7, 0.4))


def test_build_weighted_blend_handles_empty_inputs():
    assert build_weighted_blend_equity([], [], weights=(0.6, 0.4)) == []


def test_build_weighted_blend_aligns_to_shorter_input():
    a = [100.0] * 10
    b = [100.0] * 5
    out = build_weighted_blend_equity(a, b, weights=(0.5, 0.5), initial_capital=1.0)
    assert len(out) == 5


def test_build_60_40_equity_handles_nonpositive_prev_value():
    # Defensive: a degenerate input where the previous value is 0 contributes
    # 0 to the blend return rather than dividing by zero.
    spy = [100.0, 0.0, 50.0]
    agg = [100.0, 100.0, 100.0]
    out = build_60_40_equity(spy, agg, initial_capital=1000.0)
    assert all(math.isfinite(v) for v in out)
    assert len(out) == 3
