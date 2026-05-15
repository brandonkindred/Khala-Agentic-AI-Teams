"""Structured `StrategySpec` DSL (issue #550, step 1 of 8 from #537).

This module defines a typed, machine-readable replacement for today's
`StrategySpec.entry_rules: list[str]` / `exit_rules: list[str]` /
`sizing_rules: list[str]` (`backend/agents/investment_team/models.py:194-210`).
Nothing wires `StrategySpec` to these types yet — that is step 2 of #537 and
intentionally out of scope here. This module is a pure addition.

House pattern mirrored from
`backend/agents/investment_team/strategy_lab/factors/models.py`:

- `_SpecNode(BaseModel)` base with `extra="forbid"`.
- Every concrete node carries a `Literal[...]` `kind` discriminator.
- Top-level unions written as
  ``Annotated[Union[...], Field(discriminator="kind")]``.
- Trailing `Model.model_rebuild()` for forward-ref resolution.

Indicator **defaults** mirror
`backend/agents/investment_team/strategy_lab/executor/indicators.py` exactly
(RSI period=14, MACD 12/26/9, Bollinger 20/2.0, etc.).

Indicator **bound style** (e.g. `ge=2, le=400`) mirrors the existing house
factor DSL at `strategy_lab/factors/models.py`. The runtime helpers in
`executor/indicators.py` themselves accept any positive integer — the
coverage-probe registry there only enforces positive-int /
positive-float-for-``num_std``. We apply the same sanity caps the factor
DSL applies so degenerate spec payloads (`period=0`, `period=10000`) are
rejected at validation time rather than silently producing all-NaN
columns downstream.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

ComparisonOp = Literal["gt", "lt", "ge", "le", "eq", "cross_above", "cross_below"]

Source = Literal["close", "high", "low", "open", "volume", "hl2", "ohlc4"]


class _SpecNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _reject_non_finite_floats(self):
        """Reject NaN / +inf / -inf for every float field on this node.

        Non-finite floats round-trip badly: ``model_dump_json()`` serialises
        them as ``null`` / ``Infinity`` (neither of which the adapter parses
        back), and ``_format_number`` refuses them outright.  Rejecting here
        keeps the DSL's validation/serialisation contract internally
        consistent regardless of which numeric field a caller supplies.
        """
        for name, value in self.__dict__.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{type(self).__name__}.{name} must be finite (got {value!r})")
        return self


# ---------------------------------------------------------------------------
# IndicatorRef union — discriminator "kind".  Bounds mirror
# strategy_lab/executor/indicators.py.
# ---------------------------------------------------------------------------


class PriceRef(_SpecNode):
    kind: Literal["price"] = "price"
    field: Source = "close"


class ConstRef(_SpecNode):
    kind: Literal["const"] = "const"
    # Non-finite values are rejected by ``_SpecNode._reject_non_finite_floats``.
    value: float


class SMARef(_SpecNode):
    kind: Literal["sma"] = "sma"
    period: int = Field(ge=2, le=400)
    source: Source = "close"


class EMARef(_SpecNode):
    kind: Literal["ema"] = "ema"
    period: int = Field(ge=2, le=400)
    source: Source = "close"


class RSIRef(_SpecNode):
    kind: Literal["rsi"] = "rsi"
    period: int = Field(default=14, ge=2, le=200)
    source: Source = "close"


class MACDRef(_SpecNode):
    kind: Literal["macd"] = "macd"
    fast: int = Field(default=12, ge=2, le=200)
    slow: int = Field(default=26, ge=3, le=400)
    signal: int = Field(default=9, ge=2, le=100)
    output: Literal["macd", "signal", "histogram"] = "macd"
    source: Source = "close"


class BollingerRef(_SpecNode):
    kind: Literal["bollinger"] = "bollinger"
    period: int = Field(default=20, ge=5, le=200)
    num_std: float = Field(default=2.0, gt=0)
    band: Literal["upper", "middle", "lower"] = "middle"
    source: Source = "close"


class ATRRef(_SpecNode):
    kind: Literal["atr"] = "atr"
    period: int = Field(default=14, ge=2, le=200)


class ADXRef(_SpecNode):
    kind: Literal["adx"] = "adx"
    period: int = Field(default=14, ge=2, le=200)


class StochasticRef(_SpecNode):
    kind: Literal["stochastic"] = "stochastic"
    k_period: int = Field(default=14, ge=2, le=200)
    d_period: int = Field(default=3, ge=1, le=100)
    output: Literal["k", "d"] = "k"


class VWAPRef(_SpecNode):
    kind: Literal["vwap"] = "vwap"


IndicatorRef = Annotated[
    Union[
        PriceRef,
        ConstRef,
        SMARef,
        EMARef,
        RSIRef,
        MACDRef,
        BollingerRef,
        ATRRef,
        ADXRef,
        StochasticRef,
        VWAPRef,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Predicate, EntryRule, ExitRule.
# ---------------------------------------------------------------------------


class Predicate(_SpecNode):
    lhs: IndicatorRef
    op: ComparisonOp
    rhs: IndicatorRef


class EntryRule(_SpecNode):
    kind: Literal["entry"] = "entry"
    side: Literal["long", "short"] = "long"
    when: Predicate
    note: str = ""


class UnparsableRule(_SpecNode):
    """Shared variant used in entry or exit slots when prose can't be parsed."""

    kind: Literal["unparsable"] = "unparsable"
    prose: str
    reason: str = ""


EntryRuleUnion = Annotated[
    Union[EntryRule, UnparsableRule],
    Field(discriminator="kind"),
]


class TimeStopRule(_SpecNode):
    kind: Literal["time_stop"] = "time_stop"
    n_bars: int = Field(gt=0)
    note: str = ""


class StopLossRule(_SpecNode):
    kind: Literal["stop_loss"] = "stop_loss"
    pct: float = Field(gt=0, le=1.0)
    basis: Literal["entry_price", "trailing_high", "trailing_low"] = "entry_price"
    note: str = ""


class TakeProfitRule(_SpecNode):
    kind: Literal["take_profit"] = "take_profit"
    pct: float = Field(gt=0)
    note: str = ""


class SignalExitRule(_SpecNode):
    kind: Literal["signal_exit"] = "signal_exit"
    when: Predicate
    note: str = ""


ExitRule = Annotated[
    Union[TimeStopRule, StopLossRule, TakeProfitRule, SignalExitRule, UnparsableRule],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# SizingRule union.
# ---------------------------------------------------------------------------


class FixedFractionSizing(_SpecNode):
    kind: Literal["fixed_fraction"] = "fixed_fraction"
    fraction: float = Field(gt=0, le=1.0)
    note: str = ""


class VolatilityTargetSizing(_SpecNode):
    kind: Literal["volatility_target"] = "volatility_target"
    target_annual_vol: float = Field(gt=0)
    note: str = ""


class FixedNotionalSizing(_SpecNode):
    kind: Literal["fixed_notional"] = "fixed_notional"
    notional_usd: float = Field(gt=0)
    note: str = ""


class UnparsableSizing(_SpecNode):
    kind: Literal["unparsable_sizing"] = "unparsable_sizing"
    prose: str
    reason: str = ""


SizingRule = Annotated[
    Union[
        FixedFractionSizing,
        VolatilityTargetSizing,
        FixedNotionalSizing,
        UnparsableSizing,
    ],
    Field(discriminator="kind"),
]


# Resolve forward refs so union members are usable from outside the module.
PriceRef.model_rebuild()
ConstRef.model_rebuild()
SMARef.model_rebuild()
EMARef.model_rebuild()
RSIRef.model_rebuild()
MACDRef.model_rebuild()
BollingerRef.model_rebuild()
ATRRef.model_rebuild()
ADXRef.model_rebuild()
StochasticRef.model_rebuild()
VWAPRef.model_rebuild()
Predicate.model_rebuild()
EntryRule.model_rebuild()
UnparsableRule.model_rebuild()
TimeStopRule.model_rebuild()
StopLossRule.model_rebuild()
TakeProfitRule.model_rebuild()
SignalExitRule.model_rebuild()
FixedFractionSizing.model_rebuild()
VolatilityTargetSizing.model_rebuild()
FixedNotionalSizing.model_rebuild()
UnparsableSizing.model_rebuild()


# TypeAdapters expose discriminator dispatch for callers that need to
# validate a raw dict without pre-selecting the concrete class.
IndicatorRefAdapter: TypeAdapter = TypeAdapter(IndicatorRef)
EntryRuleAdapter: TypeAdapter = TypeAdapter(EntryRuleUnion)
ExitRuleAdapter: TypeAdapter = TypeAdapter(ExitRule)
SizingRuleAdapter: TypeAdapter = TypeAdapter(SizingRule)


# Issue #551: default sizing payload used when a producer (API request,
# ideation LLM output, frontend default) does not supply one. Raw dict so
# callers can pass it straight to ``StrategySpec(sizing=...)`` and let
# Pydantic dispatch — equivalent to ``FixedFractionSizing(fraction=0.02)``.
DEFAULT_SIZING_PAYLOAD: dict = {"kind": "fixed_fraction", "fraction": 0.02}


# ---------------------------------------------------------------------------
# Human-readable formatters.  Outputs are chosen to round-trip through
# spec_dsl_adapter.parse_* (step 1's adapter), so feeding format_rule's
# output back into the adapter returns an equal structured rule.
# ---------------------------------------------------------------------------


_OP_SYMBOL: dict[str, str] = {
    "gt": ">",
    "lt": "<",
    "ge": ">=",
    "le": "<=",
    "eq": "==",
    "cross_above": "crosses above",
    "cross_below": "crosses below",
}


def _format_number(x: float) -> str:
    """Render a float as decimal text the adapter regex can parse back.

    Integer-valued floats (including those that fall on an integer after the
    usual ``0.02 * 100 == 2.0000000000000004`` float-arithmetic jitter) render
    as bare integers.  Everything else uses ``repr(x)``, which gives Python's
    shortest unambiguous representation.  ``repr`` may emit scientific
    notation for very small or very large values (e.g. ``1e-13``); the adapter
    accepts that form via ``_NUMBER_PAT``.  Non-finite values raise — they're
    rejected at construction time by ``_SpecNode`` but we double-check here.
    """
    if not math.isfinite(x):
        raise ValueError(f"cannot format non-finite value: {x!r}")
    rounded = round(x)
    # Relative-tolerance check absorbs float jitter like 0.10 * 100 →
    # 10.000000000000002 without collapsing genuinely tiny values:
    # math.isclose(5e-10, 0, rel_tol=1e-12, abs_tol=0) is False because
    # rel_tol*max(|5e-10|, 0) = 5e-22 < 5e-10.  The -1e16..1e16 bound keeps
    # very large floats out of the integer fast path (their decimal form
    # would lose precision).
    if math.isclose(x, rounded, rel_tol=1e-12, abs_tol=0) and -1e16 < x < 1e16:
        return str(rounded)
    return repr(x)


def _with_source(base: str, source: str) -> str:
    """Append ``, source=X`` (or ``source=X`` for arg-less calls) when non-default."""
    if source == "close":
        return base
    assert base.endswith(")")
    inner = base[:-1]
    if inner.endswith("("):
        return f"{inner}source={source})"
    return f"{inner}, source={source})"


def _format_indicator_ref(ref: IndicatorRef) -> str:
    if isinstance(ref, PriceRef):
        return ref.field
    if isinstance(ref, ConstRef):
        return _format_number(ref.value)
    if isinstance(ref, SMARef):
        return _with_source(f"sma({ref.period})", ref.source)
    if isinstance(ref, EMARef):
        return _with_source(f"ema({ref.period})", ref.source)
    if isinstance(ref, RSIRef):
        return _with_source(f"rsi({ref.period})", ref.source)
    if isinstance(ref, MACDRef):
        # Default `output="macd"` formats as bare `macd(…)`; otherwise emit
        # `macd_signal(…)` / `macd_histogram(…)` so the token matches one of
        # the adapter's recognised indicator names.
        macd_name = "macd" if ref.output == "macd" else f"macd_{ref.output}"
        return _with_source(f"{macd_name}({ref.fast},{ref.slow},{ref.signal})", ref.source)
    if isinstance(ref, BollingerRef):
        return _with_source(
            f"bollinger_{ref.band}({ref.period},{_format_number(ref.num_std)})",
            ref.source,
        )
    if isinstance(ref, ATRRef):
        return f"atr({ref.period})"
    if isinstance(ref, ADXRef):
        return f"adx({ref.period})"
    if isinstance(ref, StochasticRef):
        return f"stochastic_{ref.output}({ref.k_period},{ref.d_period})"
    if isinstance(ref, VWAPRef):
        return "vwap()"
    raise TypeError(f"unknown IndicatorRef variant: {type(ref).__name__}")


def _format_predicate(p: Predicate) -> str:
    return f"{_format_indicator_ref(p.lhs)} {_OP_SYMBOL[p.op]} {_format_indicator_ref(p.rhs)}"


_STOP_LOSS_BASIS_PREFIX: dict[str, str] = {
    "trailing_high": "trailing-high ",
    "trailing_low": "trailing-low ",
}


def _format_rule(
    rule: EntryRule
    | UnparsableRule
    | TimeStopRule
    | StopLossRule
    | TakeProfitRule
    | SignalExitRule,
) -> str:
    if isinstance(rule, EntryRule):
        return f"{rule.side} when {_format_predicate(rule.when)}"
    if isinstance(rule, TimeStopRule):
        return f"exit after {rule.n_bars} bars"
    if isinstance(rule, StopLossRule):
        prefix = _STOP_LOSS_BASIS_PREFIX.get(rule.basis, "")
        return f"{prefix}stop loss {_format_number(rule.pct * 100)}%"
    if isinstance(rule, TakeProfitRule):
        return f"take profit {_format_number(rule.pct * 100)}%"
    if isinstance(rule, SignalExitRule):
        return f"exit when {_format_predicate(rule.when)}"
    if isinstance(rule, UnparsableRule):
        return rule.prose
    raise TypeError(f"unknown rule variant: {type(rule).__name__}")


def format_rules_for_prompt(rules, separator: str = ", ") -> str:
    """Render a list of structured rules as a single human-readable string."""
    return separator.join(_format_rule(r) for r in rules)


def format_sizing_rule(sizing) -> str:
    """Render a structured sizing rule back into the prose forms the adapter parses."""
    if isinstance(sizing, FixedFractionSizing):
        return f"risk {_format_number(sizing.fraction * 100)}% per trade"
    if isinstance(sizing, VolatilityTargetSizing):
        return f"vol-target {_format_number(sizing.target_annual_vol * 100)}%"
    if isinstance(sizing, FixedNotionalSizing):
        return f"${_format_number(sizing.notional_usd)} per trade"
    if isinstance(sizing, UnparsableSizing):
        return sizing.prose
    raise TypeError(f"unknown SizingRule variant: {type(sizing).__name__}")
