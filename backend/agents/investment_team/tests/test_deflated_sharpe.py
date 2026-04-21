"""DSR + stationary block bootstrap tests (issue #247, step 2)."""

from __future__ import annotations

import math
import random

import pytest

from investment_team.execution.metrics import (
    _annualized_sharpe_from_returns,
    _expected_max_sharpe,
    _sample_kurtosis,
    _sample_skewness,
    bootstrap_sharpe_ci,
    compute_deflated_sharpe,
    summarize_return_moments,
)

# ---------------------------------------------------------------------------
# Expected-max-Sharpe helper
# ---------------------------------------------------------------------------


def test_expected_max_sharpe_zero_for_n_le_1():
    assert _expected_max_sharpe(0) == 0.0
    assert _expected_max_sharpe(1) == 0.0


def test_expected_max_sharpe_monotone_in_trials():
    vals = [_expected_max_sharpe(n) for n in (2, 5, 50, 500, 5000)]
    for a, b in zip(vals, vals[1:]):
        assert b > a


# ---------------------------------------------------------------------------
# Moment estimators
# ---------------------------------------------------------------------------


def test_sample_moments_degrade_to_defaults_on_tiny_input():
    assert _sample_skewness([]) == 0.0
    assert _sample_skewness([0.01, 0.02]) == 0.0
    assert _sample_kurtosis([]) == 3.0
    assert _sample_kurtosis([0.01, 0.02, 0.03]) == 3.0


def test_sample_moments_on_symmetric_constant_std_are_near_normal():
    rng = random.Random(7)
    xs = [rng.gauss(0.0, 1.0) for _ in range(5000)]
    sk, ku = summarize_return_moments(xs)
    assert abs(sk) < 0.1
    assert abs(ku - 3.0) < 0.3


def test_sample_skewness_positive_for_right_skewed_series():
    # lognormal has positive skew
    rng = random.Random(11)
    xs = [math.exp(rng.gauss(0.0, 1.0)) - math.e**0.5 for _ in range(3000)]
    sk = _sample_skewness(xs)
    assert sk > 1.0


# ---------------------------------------------------------------------------
# DSR formula
# ---------------------------------------------------------------------------


def test_dsr_sr0_n1_is_half():
    dsr = compute_deflated_sharpe(sharpe=0.0, n_trials=1, n_obs=252)
    assert abs(dsr - 0.5) < 1e-9


def test_dsr_high_sr_low_trials_near_one():
    dsr = compute_deflated_sharpe(sharpe=2.0, n_trials=1, n_obs=252)
    assert dsr > 0.95


def test_dsr_deflates_monotonically_as_trials_grow():
    vals = [compute_deflated_sharpe(sharpe=1.5, n_trials=n, n_obs=252) for n in (1, 10, 100, 1000)]
    # DSR falls monotonically as the multiple-testing count grows.
    for a, b in zip(vals, vals[1:]):
        assert b < a


def test_dsr_negative_skew_and_fat_tails_lower_dsr():
    # For Sharpes comfortably above ``E[max SR]`` (discriminating regime),
    # heavier-tailed / left-skewed return series widen the PSR denominator,
    # pulling DSR down from near-1 towards the baseline Φ(0) = 0.5.
    baseline = compute_deflated_sharpe(sharpe=1.5, n_trials=5, n_obs=252, skew=0.0, kurtosis=3.0)
    heavy = compute_deflated_sharpe(sharpe=1.5, n_trials=5, n_obs=252, skew=-1.5, kurtosis=8.0)
    assert 0.5 < heavy < baseline


def test_dsr_zero_when_n_obs_too_small():
    assert compute_deflated_sharpe(sharpe=1.5, n_trials=10, n_obs=1) == 0.0


def test_dsr_zero_when_denominator_pathological():
    # Engineered input: large negative skew + large SR can make the denom
    # under the radical non-positive. Formula should return 0 (no claim).
    dsr = compute_deflated_sharpe(sharpe=5.0, n_trials=1, n_obs=252, skew=5.0, kurtosis=3.0)
    assert dsr == 0.0


# ---------------------------------------------------------------------------
# Stationary block bootstrap
# ---------------------------------------------------------------------------


def _iid_returns(mean: float, std: float, n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, std) for _ in range(n)]


def _ar1_returns(phi: float, std: float, n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    out = [rng.gauss(0.0, std)]
    for _ in range(n - 1):
        out.append(phi * out[-1] + rng.gauss(0.0, std))
    return out


def test_bootstrap_empty_and_tiny_series_return_zero_bounds():
    assert bootstrap_sharpe_ci([]) == (0.0, 0.0)
    assert bootstrap_sharpe_ci([0.01]) == (0.0, 0.0)


def test_bootstrap_invalid_confidence_raises():
    with pytest.raises(ValueError, match="confidence"):
        bootstrap_sharpe_ci([0.01, 0.02, 0.03], confidence=1.5)


def test_bootstrap_deterministic_with_seed(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.0")
    xs = _iid_returns(0.001, 0.01, 252, seed=42)
    a = bootstrap_sharpe_ci(xs, n_resamples=500, seed=13)
    b = bootstrap_sharpe_ci(xs, n_resamples=500, seed=13)
    assert a == b


def test_bootstrap_ci_contains_point_estimate_on_iid_series(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.0")
    xs = _iid_returns(0.001, 0.01, 504, seed=1)
    point = _annualized_sharpe_from_returns(xs, periods_per_year=252, rfr_annual=0.0)
    lo, hi = bootstrap_sharpe_ci(xs, n_resamples=1000, seed=1)
    assert lo <= point <= hi


def test_bootstrap_ci_width_grows_with_autocorrelation(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.0")
    # Match unconditional volatility so the two series have comparable scale.
    phi = 0.7
    innovation_std = 0.01 * math.sqrt(1 - phi**2)
    iid = _iid_returns(0.0, 0.01, 504, seed=7)
    ar1 = _ar1_returns(phi, innovation_std, 504, seed=7)
    block = 8
    iid_lo, iid_hi = bootstrap_sharpe_ci(iid, n_resamples=1000, block_size=block, seed=7)
    ar1_lo, ar1_hi = bootstrap_sharpe_ci(ar1, n_resamples=1000, block_size=block, seed=7)
    # Stationary block bootstrap preserves autocorrelation via long blocks,
    # which translates to wider Sharpe CIs under persistent returns.
    assert (ar1_hi - ar1_lo) > (iid_hi - iid_lo)


def test_bootstrap_block_size_one_collapses_to_iid(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAB_RISK_FREE_RATE", "0.0")
    xs = _iid_returns(0.0005, 0.01, 252, seed=3)
    lo1, hi1 = bootstrap_sharpe_ci(xs, n_resamples=500, block_size=1, seed=3)
    # Block size 1 is the Efron bootstrap; check CI bounds are finite and ordered.
    assert lo1 <= hi1
    assert math.isfinite(lo1) and math.isfinite(hi1)


def test_bootstrap_invalid_block_size_raises():
    with pytest.raises(ValueError, match="block_size"):
        bootstrap_sharpe_ci([0.01, 0.02, 0.03], block_size=0)
