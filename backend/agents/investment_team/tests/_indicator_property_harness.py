"""Property-based bar-sequence harness shared by the indicator-equivalence
test suite.

Not a test module — the leading underscore keeps pytest from collecting it
(see ``backend/pytest.ini``'s ``python_files = test_*.py *_test.py
bench_*.py``).

Generates a single canonical OHLCV bar sequence (:func:`bar_sequences`) and
bridges it into each of the four places the strategy_lab indicator math is
duplicated:

* ``factors.primitives`` — pure functions of a bars list.
* ``executor.indicators`` — pandas-``Series``-returning vectorized functions.
* ``executor.strategy_indicators`` — the scalar ``indicator_value`` accessor.
* ``synthesis.compiler``'s ``_HELPER_BODIES`` — per-indicator method source
  strings normally spliced into a compiled ``Strategy`` subclass.

This module only builds the bridge; it does not assert the four
implementations agree numerically (that belongs to the sibling equivalence
test suite building on top of this harness).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from hypothesis import strategies as st

from investment_team.strategy_lab.executor.indicators import INDICATORS
from investment_team.strategy_lab.executor.strategy_indicators import indicator_value
from investment_team.strategy_lab.factors import primitives
from investment_team.strategy_lab.synthesis.compiler import (
    _HELPER_BODIES,
    _emit_source_helper,
    _indent_method,
)


@dataclass
class Bar:
    """Duck-type-compatible with ``contract.Bar`` for the attributes every
    implementation reads (``.open/.high/.low/.close/.volume``), plus
    ``.timestamp``/``.symbol`` for the pieces that want them (e.g.
    synthesis's MACD cache key)."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = "AAPL"


@st.composite
def bar_sequences(draw, *, min_size: int = 30, max_size: int = 80) -> List[Bar]:
    """Generate a representative OHLCV bar sequence.

    Preconditions: ``1 <= min_size <= max_size``.
    Postconditions: returns ``n`` bars (``min_size <= n <= max_size``) with
    ``high >= max(open, close)``, ``low <= min(open, close)``, all prices
    and volume strictly positive, strictly increasing integer timestamps,
    and no NaN/inf values. The per-bar close step is drawn from one of
    three regimes (flat / trending / volatile) chosen once for the whole
    sequence, so a single generated example is internally consistent while
    examples across runs cover varied market behavior.
    """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    regime = draw(st.sampled_from(("flat", "trending", "volatile")))
    step_ranges = {
        "flat": (-0.5, 0.5),
        "trending": (0.1, 2.0),
        "volatile": (-5.0, 5.0),
    }
    lo, hi = step_ranges[regime]
    sign = draw(st.sampled_from((1.0, -1.0))) if regime == "trending" else 1.0

    close = draw(st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False))
    bars: List[Bar] = []
    for i in range(n):
        open_ = close
        step = draw(st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False))
        close = max(0.01, open_ + step * sign)
        wick_cap = max(open_, close) * 0.02 + 0.01
        wick_hi = draw(
            st.floats(min_value=0.0, max_value=wick_cap, allow_nan=False, allow_infinity=False)
        )
        wick_lo = draw(
            st.floats(min_value=0.0, max_value=wick_cap, allow_nan=False, allow_infinity=False)
        )
        volume = draw(
            st.floats(min_value=1.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
        )
        bars.append(
            Bar(
                timestamp=i,
                open=open_,
                high=max(open_, close) + wick_hi,
                low=max(0.01, min(open_, close) - wick_lo),
                close=close,
                volume=volume,
                symbol="AAPL",
            )
        )
    return bars


@dataclass(frozen=True)
class IndicatorCase:
    """One entry in :data:`SHARED_INDICATORS`.

    ``primitives_name``/``primitives_params`` address
    ``factors.primitives`` (which uses different names/kwargs for some
    indicators); ``shared_name``/``shared_params`` address the three
    implementations that already agree on naming
    (``executor.indicators``, ``executor.strategy_indicators``,
    ``synthesis.compiler``'s ``_HELPER_BODIES``).
    """

    primitives_name: str
    primitives_params: Dict[str, Any]
    shared_name: str
    shared_params: Dict[str, Any]


# The 4-way intersection of indicator math duplicated across all four
# implementations. `factors.primitives` implements a smaller, partly
# differently-named subset than the other three (no donchian/keltner/obv/
# mfi/roc/cci/williams_r, and `macd_signal`/`bollinger_z`/`stochastic_k`
# instead of `macd`/`bollinger_bands`/`stochastic`), so this table is
# hand-curated rather than derived from a single manifest — mirroring
# `test_indicator_dsl_parity.py::_PARITY_CASES`, the existing prior art for
# this exact intersection.
SHARED_INDICATORS: Dict[str, IndicatorCase] = {
    "sma": IndicatorCase("sma", {"period": 10}, "sma", {"period": 10}),
    "ema": IndicatorCase("ema", {"period": 10}, "ema", {"period": 10}),
    "rsi": IndicatorCase("rsi", {"period": 14}, "rsi", {"period": 14}),
    "atr": IndicatorCase("atr", {"period": 14}, "atr", {"period": 14}),
    "adx": IndicatorCase("adx", {"period": 14}, "adx", {"period": 14}),
    "vwap": IndicatorCase("vwap", {"period": 20}, "vwap", {"period": 20}),
    "macd": IndicatorCase(
        "macd_signal",
        {"fast": 12, "slow": 26, "signal": 9},
        "macd",
        {"fast": 12, "slow": 26, "signal": 9},
    ),
}

# The full 16-indicator surface shared by executor.indicators,
# executor.strategy_indicators, and synthesis.compiler's _HELPER_BODIES —
# reused as-is from executor.indicators.INDICATORS rather than hand-listed
# again, for callers that only need those three (not factors.primitives).
THREE_WAY_INDICATORS = tuple(INDICATORS)


def call_primitives(name: str, bars: List[Bar], **params: Any) -> float:
    """Call a ``factors.primitives`` indicator function on ``bars``."""
    fn = getattr(primitives, name)
    return fn(bars, **params)


def call_executor_indicators(name: str, bars: List[Bar], **params: Any):
    """Call an ``executor.indicators`` helper via the ``INDICATORS`` manifest.

    Each positional slot in ``spec.data_inputs`` receives the same ``bars``
    list; the helper's own ``_coerce_series(series, field=...)`` pulls the
    field that slot represents (``high``/``low``/``close``/``volume``) off
    each bar object, so passing the same list positionally is correct even
    for multi-input indicators like ``atr``/``vwap``.
    """
    spec = INDICATORS[name]
    args = [bars for _ in spec.data_inputs]
    return spec.helper(*args, **params)


def call_strategy_indicators(name: str, bars: List[Bar], *, source: str = "close", **params: Any):
    """Call ``executor.strategy_indicators.indicator_value`` for ``name``."""
    return indicator_value(name, bars, source=source, **params)


# synthesis.compiler's _HELPER_BODIES occasionally reference `math` (e.g.
# ADX's warm-up guard) and `deque` (MACD's cached macd_line) as bare globals
# — the real compiled module always imports both unconditionally (see
# `_emit_imports`), so the harness's throwaway exec namespace must too.
# MACD also reads/writes `self._ind_state`, a persistent per-indicator cache
# the compiled class's own `__init__` sets to `{}` (see `_emit_class`) — the
# throwaway class mirrors that same `__init__`.
_SYNTHESIS_EXEC_PRELUDE = "import math\nfrom collections import deque\n\n\n"


def call_synthesis_helper(name: str, bars: List[Bar], **params: Any):
    """Exec a single ``_HELPER_BODIES`` entry onto a throwaway object and
    call it directly — no ``compile_strategy``/``contract`` stub needed,
    since the raw helper bodies never reference ``contract``."""
    src = (
        _SYNTHESIS_EXEC_PRELUDE
        + "class _Harness:\n"
        + "    def __init__(self):\n"
        + "        self._ind_state = {}\n"
        + _indent_method(_emit_source_helper())
        + "\n"
        + _indent_method(_HELPER_BODIES[name])
    )
    namespace: Dict[str, Any] = {}
    exec(compile(src, "<indicator_property_harness>", "exec"), namespace)  # noqa: S102
    obj = namespace["_Harness"]()
    return getattr(obj, name)(bars, **params)
