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
    call_case,
    call_strategy_indicators,
)

# Minimal kwargs for the 3 executor/synthesis indicators whose DSL name
# differs from their executor spelling (see
# `_indicator_property_harness._STRATEGY_INDICATORS_NAME_OVERRIDES`) — just
# enough to exercise `call_strategy_indicators`'s name translation.
_NAME_TRANSLATION_CASES = {
    "bollinger_bands": {"period": 20, "num_std": 2.0},
    "donchian_channels": {"period": 20},
    "keltner_channels": {"period": 20, "atr_period": 10, "multiplier": 2.0},
}


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
    # NaN/None (warm-up) is a valid, tolerated result — the adapters must
    # simply not raise when fed the harness's generated bars. `call_case`
    # also resolves each case's per-adapter param/selector differences
    # (e.g. macd's signal-line projection), so a future equivalence test
    # can build directly on its returned values without repeating that
    # translation itself.
    call_case(SHARED_INDICATORS[name], bars)


@pytest.mark.parametrize("name", sorted(_NAME_TRANSLATION_CASES))
@given(bars=bar_sequences())
@settings(max_examples=5, deadline=None)
def test_strategy_indicators_translates_three_way_names(name, bars):
    # `call_strategy_indicators` accepts the executor/synthesis spelling of
    # these 3 indicators (e.g. "bollinger_bands") and must translate it to
    # `indicator_value`'s own DSL name ("bollinger") before dispatch,
    # rather than raising ValueError for an "unknown indicator".
    call_strategy_indicators(name, bars, **_NAME_TRANSLATION_CASES[name])
