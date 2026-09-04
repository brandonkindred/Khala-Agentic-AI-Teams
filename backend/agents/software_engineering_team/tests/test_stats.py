"""Unit tests for the shared statistics helpers (metrics._stats)."""

from __future__ import annotations

import pytest

from software_engineering_team.metrics._stats import median, p95


def test_median_of_empty_list_is_none() -> None:
    """median([]) is None, not an error."""
    assert median([]) is None


def test_median_of_odd_length_is_middle_element() -> None:
    """median of an odd-length sample is its sorted midpoint."""
    assert median([3.0, 1.0, 2.0]) == pytest.approx(2.0)


def test_median_of_even_length_averages_middle_two() -> None:
    """median of an even-length sample averages the two sorted middle elements."""
    assert median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)


def test_p95_of_empty_list_is_none() -> None:
    """p95([]) is None, not an error."""
    assert p95([]) is None


def test_p95_of_single_sample_is_that_sample() -> None:
    """p95 of one sample is the sample itself, matching median's n==1 behavior."""
    assert p95([42.0]) == pytest.approx(42.0)


def test_p95_of_two_samples_is_the_larger() -> None:
    """Nearest-rank p95 of two samples returns the worse (larger) one."""
    assert p95([10.0, 20.0]) == pytest.approx(20.0)


def test_p95_nearest_rank_no_interpolation() -> None:
    """p95 of four samples matches hand computation: rank = ceil(0.95*4) = 4."""
    assert p95([10.0, 20.0, 30.0, 40.0]) == pytest.approx(40.0)
