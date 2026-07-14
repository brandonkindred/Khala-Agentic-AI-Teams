"""Single source of truth for per-indicator param constraints, emit args, and
lookback formulas — consumed by both DSL compilers and by ``spec_dsl``.

Host-side only (imported by ``spec_dsl.py``, ``factors/compiler.py``,
``synthesis/compiler.py``): never flattened into the strategy sandbox, so it
has no constraint from the sandbox import whitelist (``quality_gates.code_safety
.ALLOWED_IMPORTS``) — that whitelist only governs what *emitted/compiled*
strategy source may import, not what the compilers importing this module may
do at compile time.

Before this module, four independent tables carried overlapping slices of
this same information: ``spec_dsl._INDICATOR_PARAM_SPECS`` (param bounds),
``spec_dsl.INDICATOR_HELPER_NAME`` (DSL name -> emitted method name),
``synthesis.compiler._EMIT_ARGS`` (kwarg emission), and two independently
hand-written lookback functions (``factors.compiler._lookback``,
``synthesis.compiler._lookback_for``) that had to be kept in sync by hand — a
lookback bug (MACD signal: ``slow + signal - 1``, not ``slow + signal``) was
once fixed in one place and had to be manually re-applied to the others.
``IndicatorDescriptor``/``INDICATOR_METADATA`` below fold all four into one
table; ``spec_dsl.py`` and ``synthesis/compiler.py`` re-export the derived
views under their original names so no downstream import changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Validator factories (moved from spec_dsl.py — identical behavior, including
# the ``.allowed`` attribute ``_one_of`` sets, which
# ``quality_gates.code_conformance`` reads directly off the spec table).
# ---------------------------------------------------------------------------


def _int_in(lo: int, hi: int):
    def check(value: Any) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"must be int (got {type(value).__name__})")
        if not (lo <= value <= hi):
            raise ValueError(f"must be in [{lo}, {hi}] (got {value})")

    return check


def _float_gt(threshold: float):
    def check(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"must be numeric (got {type(value).__name__})")
        if not (math.isfinite(float(value)) and float(value) > threshold):
            raise ValueError(f"must be > {threshold} (got {value})")

    return check


def _one_of(*allowed: str):
    allowed_set = frozenset(allowed)

    def check(value: Any) -> None:
        if value not in allowed_set:
            raise ValueError(f"must be one of {sorted(allowed_set)} (got {value!r})")

    # Exposed so downstream code can derive from the DSL rather than
    # hardcoding a second copy (e.g. the conformance gate's Bollinger
    # derived-band set); keep this the single source of truth.
    check.allowed = allowed_set
    return check


# Floor applied to the cumulative-style indicators (vwap pre-rolling-window,
# obv) whose warm-up has no strict formula. Exposed so ``synthesis.compiler``
# can derive its ``_MIN_WINDOW`` from the same constant instead of a private
# literal.
MIN_WINDOW: int = 20


@dataclass(frozen=True)
class IndicatorDescriptor:
    """Everything a DSL compiler or the param-validation layer needs for one indicator.

    Preconditions: ``lookback`` accepts a fully-defaulted params mapping
    (every optional key present, e.g. as produced by
    ``IndicatorRef._validate_params``'s ``setdefault`` pass) and returns the
    minimum bar count before the indicator yields a non-``None``/non-``NaN``
    value.
    """

    name: str
    # The emitted/DSL helper method name — i.e. what ``INDICATOR_HELPER_NAME``
    # maps to and what the compiled strategy class exposes (e.g.
    # ``donchian_channels``). This is NOT always a live ``IndicatorRegistry``
    # method name: for donchian/keltner the registry methods are ``reg.donchian``
    # / ``reg.keltner``, so do not ``getattr(reg, helper_name)`` — the runtime
    # name → registry-method dispatch lives in ``streaming.resolve_indicator``.
    helper_name: str
    required: Mapping[str, Callable[[Any], None]]
    optional: Mapping[str, Tuple[Any, Callable[[Any], None]]]
    allow_source: bool
    output_range: Optional[Tuple[float, float]]
    lookback: Callable[[Mapping[str, Any]], int]
    emit_args: Tuple[Tuple[str, str, Optional[str]], ...] = field(default_factory=tuple)


_DESCRIPTORS: Tuple[IndicatorDescriptor, ...] = (
    IndicatorDescriptor(
        name="sma",
        helper_name="sma",
        required={"period": _int_in(2, 400)},
        optional={},
        allow_source=True,
        output_range=None,
        lookback=lambda p: int(p["period"]),
        emit_args=(("period", "int", "period"), ("source", "source", None)),
    ),
    IndicatorDescriptor(
        name="ema",
        helper_name="ema",
        required={"period": _int_in(2, 400)},
        optional={},
        allow_source=True,
        output_range=None,
        lookback=lambda p: int(p["period"]),
        emit_args=(("period", "int", "period"), ("source", "source", None)),
    ),
    IndicatorDescriptor(
        name="rsi",
        helper_name="rsi",
        required={},
        optional={"period": (14, _int_in(2, 200))},
        allow_source=True,
        output_range=(0.0, 100.0),
        # Each RSI gain/loss term compares against the prior bar, so the
        # first ratio needs one extra bar beyond ``period``.
        lookback=lambda p: int(p.get("period", 14)) + 1,
        emit_args=(("period", "int", "period"), ("source", "source", None)),
    ),
    IndicatorDescriptor(
        name="macd",
        helper_name="macd",
        required={},
        optional={
            "fast": (12, _int_in(2, 200)),
            "slow": (26, _int_in(3, 400)),
            "signal": (9, _int_in(2, 100)),
            "output": ("macd", _one_of("macd", "signal", "histogram")),
        },
        allow_source=True,
        output_range=None,
        # The macd line is computable at ``slow`` bars; the signal/histogram
        # outputs additionally need ``signal - 1`` macd-line samples, i.e.
        # ``slow + signal - 1`` bars total. This is the exact formula whose
        # duplication once caused a real bug (see module docstring).
        lookback=lambda p: (
            int(p.get("slow", 26))
            if str(p.get("output", "macd")) == "macd"
            else int(p.get("slow", 26)) + int(p.get("signal", 9)) - 1
        ),
        emit_args=(
            ("fast", "int", "fast"),
            ("slow", "int", "slow"),
            ("signal", "int", "signal"),
            ("source", "source", None),
            ("select", "select", "output"),
        ),
    ),
    IndicatorDescriptor(
        name="bollinger",
        helper_name="bollinger_bands",
        required={},
        optional={
            "period": (20, _int_in(5, 200)),
            "num_std": (2.0, _float_gt(0)),
            "band": (
                "middle",
                _one_of("upper", "middle", "lower", "percent_b", "bandwidth"),
            ),
        },
        allow_source=True,
        output_range=None,
        lookback=lambda p: int(p.get("period", 20)),
        emit_args=(
            ("period", "int", "period"),
            ("num_std", "float", "num_std"),
            ("source", "source", None),
            ("select", "select", "band"),
        ),
    ),
    IndicatorDescriptor(
        name="atr",
        helper_name="atr",
        required={},
        optional={"period": (14, _int_in(2, 200))},
        allow_source=False,
        output_range=None,
        # Each true-range term reads the prior bar's close.
        lookback=lambda p: int(p.get("period", 14)) + 1,
        emit_args=(("period", "int", "period"),),
    ),
    IndicatorDescriptor(
        name="adx",
        helper_name="adx",
        required={},
        optional={"period": (14, _int_in(2, 200))},
        allow_source=False,
        output_range=(0.0, 100.0),
        # Wilder smoothing requires two DX windows: ``2 * period + 1``.
        lookback=lambda p: 2 * int(p.get("period", 14)) + 1,
        emit_args=(("period", "int", "period"),),
    ),
    IndicatorDescriptor(
        name="stochastic",
        helper_name="stochastic",
        required={},
        optional={
            "k_period": (14, _int_in(2, 200)),
            "d_period": (3, _int_in(1, 100)),
            "output": ("k", _one_of("k", "d")),
        },
        allow_source=False,
        output_range=(0.0, 100.0),
        # %K available at k_period; %D smoothing needs d_period - 1
        # additional bars of %K history.
        lookback=lambda p: int(p.get("k_period", 14)) + int(p.get("d_period", 3)) - 1,
        emit_args=(
            ("k_period", "int", "k_period"),
            ("d_period", "int", "d_period"),
            ("select", "select", "output"),
        ),
    ),
    IndicatorDescriptor(
        name="vwap",
        helper_name="vwap",
        # Rolling window, unified with the factors DSL's VWAP node (which has
        # always taken a ``period``). Synthesis's VWAP was cumulative-over-
        # all-history before this — an explicit, intentional behavior change
        # for synthesis-compiled strategies referencing VWAP.
        required={},
        optional={"period": (20, _int_in(2, 400))},
        allow_source=False,
        output_range=None,
        lookback=lambda p: int(p.get("period", 20)),
        emit_args=(("period", "int", "period"),),
    ),
    IndicatorDescriptor(
        name="donchian",
        helper_name="donchian_channels",
        required={},
        optional={
            "period": (20, _int_in(2, 400)),
            "band": ("middle", _one_of("upper", "middle", "lower")),
        },
        allow_source=False,
        output_range=None,
        lookback=lambda p: int(p.get("period", 20)),
        emit_args=(("period", "int", "period"), ("select", "select", "band")),
    ),
    IndicatorDescriptor(
        name="keltner",
        helper_name="keltner_channels",
        required={},
        optional={
            "period": (20, _int_in(2, 400)),
            "atr_period": (10, _int_in(2, 200)),
            "multiplier": (2.0, _float_gt(0)),
            "band": ("middle", _one_of("upper", "middle", "lower")),
        },
        allow_source=False,
        output_range=None,
        # EMA basis needs ``period`` bars; the ATR leg needs ``atr_period + 1``.
        lookback=lambda p: max(int(p.get("period", 20)), int(p.get("atr_period", 10)) + 1),
        emit_args=(
            ("period", "int", "period"),
            ("atr_period", "int", "atr_period"),
            ("multiplier", "float", "multiplier"),
            ("select", "select", "band"),
        ),
    ),
    IndicatorDescriptor(
        name="obv",
        helper_name="obv",
        required={},
        optional={},
        allow_source=False,
        output_range=None,
        # Cumulative over all history (unlike VWAP, which is now a rolling
        # window) — no strict warm-up; floored to MIN_WINDOW.
        lookback=lambda p: MIN_WINDOW,
        emit_args=(),
    ),
    IndicatorDescriptor(
        name="mfi",
        helper_name="mfi",
        required={},
        optional={"period": (14, _int_in(2, 200))},
        allow_source=False,
        output_range=(0.0, 100.0),
        # Each money-flow term compares typical price against the prior bar.
        lookback=lambda p: int(p.get("period", 14)) + 1,
        emit_args=(("period", "int", "period"),),
    ),
    IndicatorDescriptor(
        name="roc",
        helper_name="roc",
        required={},
        optional={"period": (12, _int_in(2, 400))},
        allow_source=True,
        output_range=None,
        lookback=lambda p: int(p.get("period", 12)) + 1,
        emit_args=(("period", "int", "period"), ("source", "source", None)),
    ),
    IndicatorDescriptor(
        name="cci",
        helper_name="cci",
        required={},
        optional={"period": (20, _int_in(2, 400))},
        allow_source=False,
        output_range=None,
        lookback=lambda p: int(p.get("period", 20)),
        emit_args=(("period", "int", "period"),),
    ),
    IndicatorDescriptor(
        name="williams_r",
        helper_name="williams_r",
        required={},
        optional={"period": (14, _int_in(2, 200))},
        allow_source=False,
        output_range=(-100.0, 0.0),
        lookback=lambda p: int(p.get("period", 14)),
        emit_args=(("period", "int", "period"),),
    ),
)

INDICATOR_METADATA: Mapping[str, IndicatorDescriptor] = MappingProxyType(
    {d.name: d for d in _DESCRIPTORS}
)

_EXPECTED_NAMES: frozenset[str] = frozenset(
    {
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger",
        "atr",
        "adx",
        "stochastic",
        "vwap",
        "donchian",
        "keltner",
        "obv",
        "mfi",
        "roc",
        "cci",
        "williams_r",
    }
)
if set(INDICATOR_METADATA) != _EXPECTED_NAMES:
    raise RuntimeError(
        "INDICATOR_METADATA must cover exactly the DSL indicator names; "
        f"mismatch: {_EXPECTED_NAMES ^ set(INDICATOR_METADATA)}"
    )


# ---------------------------------------------------------------------------
# Backward-compatible derived views — re-exported by ``spec_dsl.py`` and
# ``synthesis/compiler.py`` under their original names so no downstream
# import site changes.
# ---------------------------------------------------------------------------

INDICATOR_PARAM_SPECS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        name: MappingProxyType(
            {
                "required": d.required,
                "optional": d.optional,
                "allow_source": d.allow_source,
                **({"output_range": d.output_range} if d.output_range is not None else {}),
            }
        )
        for name, d in INDICATOR_METADATA.items()
    }
)

INDICATOR_HELPER_NAME: Mapping[str, str] = MappingProxyType(
    {name: d.helper_name for name, d in INDICATOR_METADATA.items()}
)

INDICATOR_OUTPUT_RANGES: Mapping[str, Tuple[float, float]] = MappingProxyType(
    {name: d.output_range for name, d in INDICATOR_METADATA.items() if d.output_range is not None}
)

EMIT_ARGS: Mapping[str, Tuple[Tuple[str, str, Optional[str]], ...]] = MappingProxyType(
    {name: d.emit_args for name, d in INDICATOR_METADATA.items()}
)


def lookback_for(name: str, params: Mapping[str, Any]) -> int:
    """Minimum bar count before indicator ``name`` yields a real value.

    Preconditions: ``name`` is a key of :data:`INDICATOR_METADATA`; ``params``
    carries (at least) the keys the indicator's ``lookback`` formula reads —
    missing optional keys fall back to the indicator's own documented default.
    Postconditions: matches the first ``len(history)`` at which the
    corresponding compiled helper stops returning ``None``/``NaN``.
    """
    return INDICATOR_METADATA[name].lookback(params)
