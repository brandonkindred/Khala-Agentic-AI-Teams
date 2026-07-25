"""Cross-implementation numeric equivalence tests for the shared indicator
math duplicated across `factors.primitives`, `executor.indicators`,
`executor.strategy_indicators`, and `synthesis.compiler`'s `_HELPER_BODIES`.

Builds on the harness in `_indicator_property_harness.py` (which only wires
each implementation up to the same generated bar sequence, without
asserting the results agree). This module is the assertion layer: a
formula fix applied to one copy but not mirrored in the others should fail
here rather than silently diverging compiled-strategy behavior from
custom-code strategy behavior in production.
"""

from __future__ import annotations

import math
from typing import Any, Dict

import pandas as pd
import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402

from ._indicator_property_harness import (  # noqa: E402
    SHARED_INDICATORS,
    bar_sequences,
    call_case,
)

# MACD's slow(26)+signal(9) EMA chain needs ~35 bars to clear warm-up;
# min_size=45 gives every generated sequence enough headroom that all four
# adapters are always past warm-up, so a divergence in this test is always
# a genuine numeric disagreement rather than the already-known difference
# in each adapter's warm-up return convention (`primitives` returns `nan`,
# `strategy_indicators`/`synthesis` return `None`).
_EQUIVALENCE_BARS = bar_sequences(min_size=45, max_size=80)

_REL_TOL = 1e-6
_ABS_TOL = 1e-9


def _to_scalar(value: Any) -> Any:
    """Reduce a `call_case` result to its last-bar scalar.

    Preconditions: none.
    Postconditions: a `pd.Series` (`executor.indicators` always returns
    one, even after `executor_select` projects a multi-line tuple down to
    a single line) is reduced to its final element; any other value
    (already scalar) is returned unchanged.
    """
    if isinstance(value, pd.Series):
        return value.iloc[-1]
    return value


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _assert_all_close(name: str, values: Dict[str, Any]) -> None:
    """Assert every adapter in `values` agrees on the same scalar.

    Preconditions: `values` maps adapter label -> the adapter's raw
    `call_case` result for indicator `name`.
    Postconditions: raises `AssertionError` with all four labeled values
    if any is still in warm-up (missing) or if any pair disagrees beyond
    `_REL_TOL`/`_ABS_TOL`; otherwise returns silently.
    """
    scalars = {label: _to_scalar(value) for label, value in values.items()}
    missing = {label: value for label, value in scalars.items() if _is_missing(value)}
    assert not missing, (
        f"{name}: adapter(s) still in warm-up despite the harness's warm-up "
        f"margin -- {missing}; full results: {scalars}"
    )
    reference_label, reference_value = next(iter(scalars.items()))
    mismatches = {
        label: value
        for label, value in scalars.items()
        if not math.isclose(value, reference_value, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    }
    assert not mismatches, f"{name}: implementations disagree -- {scalars}"


@pytest.mark.parametrize("name", sorted(SHARED_INDICATORS))
@given(bars=_EQUIVALENCE_BARS)
@settings(max_examples=25, deadline=None)
def test_indicator_implementations_are_numerically_equivalent(name, bars):
    case = SHARED_INDICATORS[name]
    results = call_case(case, bars)
    _assert_all_close(name, results)
