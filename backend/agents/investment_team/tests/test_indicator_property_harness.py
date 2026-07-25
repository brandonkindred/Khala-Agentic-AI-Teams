"""Smoke tests for the shared indicator property-test harness.

Proves the harness itself works — generated bars are valid OHLCV and each
of the four indicator-implementation adapters runs end-to-end without
raising. This is groundwork for the sibling cross-implementation numeric
equivalence tests; it does not assert the four implementations agree
numerically.
"""

from __future__ import annotations

import math

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402

from ._indicator_property_harness import (  # noqa: E402
    SHARED_INDICATORS,
    bar_sequences,
    call_executor_indicators,
    call_primitives,
    call_strategy_indicators,
    call_synthesis_helper,
)


@given(bars=bar_sequences())
@settings(max_examples=8, deadline=None)
def test_generated_bars_are_valid_ohlcv(bars):
    assert len(bars) >= 30
    for i, bar in enumerate(bars):
        assert bar.timestamp == i
        assert bar.open > 0 and bar.close > 0
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)
        assert bar.low > 0
        assert bar.volume >= 0
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume):
            assert not math.isnan(value)
            assert not math.isinf(value)


@pytest.mark.parametrize("name", sorted(SHARED_INDICATORS))
@given(bars=bar_sequences())
@settings(max_examples=5, deadline=None)
def test_each_adapter_runs_without_error(name, bars):
    case = SHARED_INDICATORS[name]

    # NaN/None (warm-up) is a valid, tolerated result — the adapters must
    # simply not raise when fed the harness's generated bars.
    call_primitives(case.primitives_name, bars, **case.primitives_params)
    call_executor_indicators(case.shared_name, bars, **case.shared_params)
    call_strategy_indicators(case.shared_name, bars, **case.shared_params)
    call_synthesis_helper(case.shared_name, bars, **case.shared_params)
