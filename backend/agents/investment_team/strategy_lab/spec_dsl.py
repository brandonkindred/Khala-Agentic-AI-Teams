"""Structured `StrategySpec` DSL — issue #537 literal schema.

- `IndicatorRef` is a single flat class — `name` selects the indicator,
  `params` carries the (typed) per-indicator arguments, `source`
  selects the bar field. Per-indicator required/optional/bounds
  validation lives in a registry-backed `model_validator`.
- `Predicate.op` uses the symbol literals (``"<"``, ``">"``, …).
- `Predicate.lhs` is either an `IndicatorRef` or a
  ``Literal["bar.close","bar.high","bar.low","bar.volume"]`` bar-field
  reference. `Predicate.rhs` additionally accepts a plain ``float``.
- `EntryRule`, `ExitRule`, and `SizingRule` are discriminated unions on
  `kind`. Prose strings are not accepted as rule values — Pydantic
  discriminator validation raises on prose input.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

# Issue #537: comparison ops are the literal symbols, not name aliases.
ComparisonOp = Literal["<", ">", "<=", ">=", "==", "cross_above", "cross_below"]

Source = Literal["close", "high", "low", "open", "volume", "hl2", "ohlc4"]

# Bar-field price references. `lhs` may name any of these; `rhs` cannot
# reference "bar.volume" (semantic comparison against a volume on the
# right side is rarely meaningful and unsupported here).
PriceRefLiteral = Literal["bar.close", "bar.high", "bar.low", "bar.volume"]
ExitPriceRefLiteral = Literal["bar.close", "bar.high", "bar.low"]

IndicatorName = Literal[
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "atr",
    "adx",
    "stochastic",
    "vwap",
]

_PRICE_REF_LITERALS: frozenset[str] = frozenset({"bar.close", "bar.high", "bar.low", "bar.volume"})


class _SpecNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _reject_non_finite_floats(self):
        """Reject NaN / +inf / -inf for every float field on this node.

        Non-finite floats round-trip badly: ``model_dump_json()`` serialises
        them as ``null`` / ``Infinity`` (neither of which downstream parsers
        accept), and ``_format_number`` refuses them outright.  Rejecting
        here keeps the DSL's validation/serialisation contract internally
        consistent regardless of which numeric field a caller supplies.
        """
        for name, value in self.__dict__.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{type(self).__name__}.{name} must be finite (got {value!r})")
        return self


# ---------------------------------------------------------------------------
# Per-indicator param registry. Each entry describes the params an indicator
# accepts; the registry is consulted by `IndicatorRef._validate_params` to
# enforce required keys, default fill-ins on optional keys, and per-param
# type/bounds. This is the per-indicator validation the issue-text shape
# (``params: dict[str, float | int | str]``) cannot enforce at the type level.
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
    allowed_set = set(allowed)

    def check(value: Any) -> None:
        if value not in allowed_set:
            raise ValueError(f"must be one of {sorted(allowed_set)} (got {value!r})")

    return check


# Each entry: required[key] = validator; optional[key] = (default, validator).
# ``allow_source`` mirrors the previous per-class behaviour: ATR/ADX/Stochastic
# do not accept a ``source`` override (they read OHLC directly).
_INDICATOR_PARAM_SPECS: dict[str, dict[str, Any]] = {
    "sma": {
        "required": {"period": _int_in(2, 400)},
        "optional": {},
        "allow_source": True,
    },
    "ema": {
        "required": {"period": _int_in(2, 400)},
        "optional": {},
        "allow_source": True,
    },
    "rsi": {
        "required": {},
        "optional": {"period": (14, _int_in(2, 200))},
        "allow_source": True,
    },
    "macd": {
        "required": {},
        "optional": {
            "fast": (12, _int_in(2, 200)),
            "slow": (26, _int_in(3, 400)),
            "signal": (9, _int_in(2, 100)),
            "output": ("macd", _one_of("macd", "signal", "histogram")),
        },
        "allow_source": True,
    },
    "bollinger": {
        "required": {},
        "optional": {
            "period": (20, _int_in(5, 200)),
            "num_std": (2.0, _float_gt(0)),
            "band": ("middle", _one_of("upper", "middle", "lower")),
        },
        "allow_source": True,
    },
    "atr": {
        "required": {},
        "optional": {"period": (14, _int_in(2, 200))},
        "allow_source": False,
    },
    "adx": {
        "required": {},
        "optional": {"period": (14, _int_in(2, 200))},
        "allow_source": False,
    },
    "stochastic": {
        "required": {},
        "optional": {
            "k_period": (14, _int_in(2, 200)),
            "d_period": (3, _int_in(1, 100)),
            "output": ("k", _one_of("k", "d")),
        },
        "allow_source": False,
    },
    "vwap": {
        "required": {},
        "optional": {},
        "allow_source": False,
    },
}


# ---------------------------------------------------------------------------
# IndicatorRef — flat shape per issue #537. The `name` field selects the
# indicator; `params` is a dict whose accepted keys/values are governed by
# `_INDICATOR_PARAM_SPECS`. `params` is widened to `float | int | str` (vs.
# the issue's literal `float | int`) so the existing `macd.output`,
# `bollinger.band`, `stochastic.output` selectors keep working — these are
# discrete-choice params, not free strings.
# ---------------------------------------------------------------------------


class IndicatorRef(_SpecNode):
    name: IndicatorName
    params: dict[str, Union[int, float, str]] = Field(default_factory=dict)
    source: Source = "close"

    @model_validator(mode="after")
    def _validate_params(self):
        spec = _INDICATOR_PARAM_SPECS[self.name]
        required: dict[str, Any] = spec["required"]
        optional: dict[str, Any] = spec["optional"]
        allow_source: bool = spec["allow_source"]

        for key, check in required.items():
            if key not in self.params:
                raise ValueError(f"indicator {self.name!r} requires param {key!r}")
            check(self.params[key])

        for key, value in self.params.items():
            if key in required:
                continue
            if key not in optional:
                allowed = sorted(set(required) | set(optional))
                raise ValueError(
                    f"indicator {self.name!r} got unexpected param {key!r}; allowed: {allowed}"
                )
            _default, check = optional[key]
            check(value)

        if not allow_source and self.source != "close":
            raise ValueError(f"indicator {self.name!r} does not accept a 'source' override")

        # Fill in defaults for any optional params that weren't supplied so
        # constructed nodes are self-describing — e.g. ``IndicatorRef(name="rsi")``
        # is equivalent post-construction to
        # ``IndicatorRef(name="rsi", params={"period": 14})``. Two IndicatorRefs
        # with the same effective configuration compare equal regardless of
        # whether the caller passed the default explicitly.
        for key, (default, _check) in optional.items():
            self.params.setdefault(key, default)

        return self

    def param(self, key: str, default: Any = None) -> Any:
        """Return ``params[key]`` if set, else the registry default, else ``default``."""
        if key in self.params:
            return self.params[key]
        spec = _INDICATOR_PARAM_SPECS[self.name]
        if key in spec["optional"]:
            return spec["optional"][key][0]
        return default


# ---------------------------------------------------------------------------
# Predicate, EntryRule, ExitRule.
# ---------------------------------------------------------------------------


PredicateLhs = Union[IndicatorRef, PriceRefLiteral]
PredicateRhs = Union[IndicatorRef, PriceRefLiteral, float]


class Predicate(_SpecNode):
    lhs: PredicateLhs
    op: ComparisonOp
    # ``rhs`` is intentionally widened beyond the issue's
    # ``ExitPriceRefLiteral`` to accept ``"bar.volume"`` symmetrically with
    # ``lhs``. Comparing volume against indicators is uncommon but supported
    # for symmetry. Floats are accepted only on the rhs.
    rhs: PredicateRhs

    @model_validator(mode="after")
    def _validate_sides(self):
        if isinstance(self.lhs, str) and self.lhs not in _PRICE_REF_LITERALS:
            raise ValueError(
                f"Predicate.lhs string must be one of {sorted(_PRICE_REF_LITERALS)}; "
                f"got {self.lhs!r}"
            )
        if isinstance(self.rhs, str) and self.rhs not in _PRICE_REF_LITERALS:
            raise ValueError(
                f"Predicate.rhs string must be one of {sorted(_PRICE_REF_LITERALS)} "
                f"or a float; got {self.rhs!r}"
            )
        return self


class EntryRule(_SpecNode):
    kind: Literal["entry"] = "entry"
    side: Literal["long", "short"] = "long"
    when: Predicate
    note: str = ""


# Back-compat alias — earlier the entry union included an Unparsable variant.
# Issue #537 lifts unparseable handling to the spec level; this alias keeps
# import sites unchanged.
EntryRuleUnion = EntryRule


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


# Exit rules: structured close conditions the engine enforces (stop-loss /
# take-profit) plus signal-based exits.  The union is intentionally limited
# to price-, P&L-, and signal-based exits.
ExitRule = Annotated[
    Union[StopLossRule, TakeProfitRule, SignalExitRule],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# SizingRule union. Kept as a typed union (vs. the issue's flat
# ``{kind, params}`` shape) so the existing typed call sites in the
# orchestrator/ideation continue to validate at the type level. The
# user-listed scope only required reshaping ``IndicatorRef`` and
# ``Predicate``; the sizing union was not included.
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


SizingRule = Annotated[
    Union[FixedFractionSizing, VolatilityTargetSizing, FixedNotionalSizing],
    Field(discriminator="kind"),
]


# Resolve forward refs so union members are usable from outside the module.
IndicatorRef.model_rebuild()
Predicate.model_rebuild()
EntryRule.model_rebuild()
StopLossRule.model_rebuild()
TakeProfitRule.model_rebuild()
SignalExitRule.model_rebuild()
FixedFractionSizing.model_rebuild()
VolatilityTargetSizing.model_rebuild()
FixedNotionalSizing.model_rebuild()


# TypeAdapters expose discriminator dispatch for callers that need to
# validate a raw dict without pre-selecting the concrete class.
IndicatorRefAdapter: TypeAdapter = TypeAdapter(IndicatorRef)
EntryRuleAdapter: TypeAdapter = TypeAdapter(EntryRule)
ExitRuleAdapter: TypeAdapter = TypeAdapter(ExitRule)
SizingRuleAdapter: TypeAdapter = TypeAdapter(SizingRule)


# Issue #551: default sizing payload used when a producer (API request,
# ideation LLM output, frontend default) does not supply one. Raw dict so
# callers can pass it straight to ``StrategySpec(sizing=...)`` and let
# Pydantic dispatch — equivalent to ``FixedFractionSizing(fraction=0.02)``.
DEFAULT_SIZING_PAYLOAD: dict = {"kind": "fixed_fraction", "fraction": 0.02}


# ---------------------------------------------------------------------------
# Human-readable formatters. Render structured rules to prose for LLM
# prompts. Not a parser — the DSL is the only accepted input shape.
# ---------------------------------------------------------------------------


_OP_SYMBOL: dict[str, str] = {
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
    "==": "==",
    "cross_above": "crosses above",
    "cross_below": "crosses below",
}


def _format_number(x: float) -> str:
    """Render a float as decimal text the adapter regex can parse back."""
    if not math.isfinite(x):
        raise ValueError(f"cannot format non-finite value: {x!r}")
    rounded = round(x)
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
    name = ref.name
    if name == "sma":
        base = f"sma({ref.param('period')})"
        return _with_source(base, ref.source)
    if name == "ema":
        base = f"ema({ref.param('period')})"
        return _with_source(base, ref.source)
    if name == "rsi":
        base = f"rsi({ref.param('period')})"
        return _with_source(base, ref.source)
    if name == "macd":
        output = ref.param("output")
        macd_name = "macd" if output == "macd" else f"macd_{output}"
        base = f"{macd_name}({ref.param('fast')},{ref.param('slow')},{ref.param('signal')})"
        return _with_source(base, ref.source)
    if name == "bollinger":
        period = ref.param("period")
        num_std = float(ref.param("num_std"))
        band = ref.param("band")
        base = f"bollinger_{band}({period},{_format_number(num_std)})"
        return _with_source(base, ref.source)
    if name == "atr":
        return f"atr({ref.param('period')})"
    if name == "adx":
        return f"adx({ref.param('period')})"
    if name == "stochastic":
        output = ref.param("output")
        return f"stochastic_{output}({ref.param('k_period')},{ref.param('d_period')})"
    if name == "vwap":
        return "vwap()"
    raise TypeError(f"unknown IndicatorRef name: {name!r}")


def _format_side(side: Union[IndicatorRef, str, int, float]) -> str:
    """Render one side of a `Predicate` for the prose formatter."""
    if isinstance(side, IndicatorRef):
        return _format_indicator_ref(side)
    if isinstance(side, str):
        if side in _PRICE_REF_LITERALS:
            return side.split(".", 1)[1]  # "bar.close" -> "close"
        raise ValueError(f"unexpected string side: {side!r}")
    if isinstance(side, bool):
        raise TypeError("boolean is not a valid predicate side")
    if isinstance(side, (int, float)):
        return _format_number(float(side))
    raise TypeError(f"unknown predicate side type: {type(side).__name__}")


def _format_predicate(p: Predicate) -> str:
    return f"{_format_side(p.lhs)} {_OP_SYMBOL[p.op]} {_format_side(p.rhs)}"


_STOP_LOSS_BASIS_PREFIX: dict[str, str] = {
    "trailing_high": "trailing-high ",
    "trailing_low": "trailing-low ",
}


def _format_rule(
    rule: Union[EntryRule, StopLossRule, TakeProfitRule, SignalExitRule],
) -> str:
    if isinstance(rule, EntryRule):
        return f"{rule.side} when {_format_predicate(rule.when)}"
    if isinstance(rule, StopLossRule):
        prefix = _STOP_LOSS_BASIS_PREFIX.get(rule.basis, "")
        return f"{prefix}stop loss {_format_number(rule.pct * 100)}%"
    if isinstance(rule, TakeProfitRule):
        return f"take profit {_format_number(rule.pct * 100)}%"
    if isinstance(rule, SignalExitRule):
        return f"exit when {_format_predicate(rule.when)}"
    raise TypeError(f"unknown rule variant: {type(rule).__name__}")


def format_rules_for_prompt(rules, separator: str = ", ") -> str:
    """Render a list of structured rules as a single human-readable string."""
    return separator.join(_format_rule(r) for r in rules)


def format_sizing_rule(sizing) -> str:
    """Render a structured sizing rule back into prose the adapter parses."""
    if isinstance(sizing, FixedFractionSizing):
        return f"risk {_format_number(sizing.fraction * 100)}% per trade"
    if isinstance(sizing, VolatilityTargetSizing):
        return f"vol-target {_format_number(sizing.target_annual_vol * 100)}%"
    if isinstance(sizing, FixedNotionalSizing):
        return f"${_format_number(sizing.notional_usd)} per trade"
    raise TypeError(f"unknown SizingRule variant: {type(sizing).__name__}")
