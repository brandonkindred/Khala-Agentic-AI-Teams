"""Tests for shared.hitl.progress.coerce_progress — clamp to [0, 100]."""

from __future__ import annotations

import pytest

from shared.hitl.progress import coerce_progress


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (47, 47),
        (100, 100),
        (250, 100),  # clamp high
        (-5, 0),  # clamp low
        (99.9, 99),  # float truncates toward zero, then in-range
        (150.0, 100),  # float clamps
        ("42", 42),  # numeric string
        ("0", 0),
        (True, 1),  # bool is an int subtype -> 1, in range
    ],
)
def test_coerce_progress_numeric(value, expected):
    assert coerce_progress(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", "", "12.5", [], {}, object()],
)
def test_coerce_progress_garbage_returns_none(value):
    assert coerce_progress(value) is None
