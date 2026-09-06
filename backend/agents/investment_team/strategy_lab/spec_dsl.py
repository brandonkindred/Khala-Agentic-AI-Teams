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
from decimal import Decimal
from typing import Annotated, Any, Callable, Iterator, Literal, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .indicators.registry_metadata import (
    INDICATOR_HELPER_NAME,
    INDICATOR_OUTPUT_RANGES,  # noqa: F401 (re-exported for downstream imports)
    _float_gt,  # noqa: F401 (re-exported for downstream imports)
    _int_in,  # noqa: F401 (re-exported for downstream imports)
    _one_of,  # noqa: F401 (re-exported for downstream imports)
)
from .indicators.registry_metadata import INDICATOR_PARAM_SPECS as _INDICATOR_PARAM_SPECS

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
    "donchian",
    "keltner",
    "obv",
    "mfi",
    "roc",
    "cci",
    "williams_r",
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
# Per-indicator param registry. ``_INDICATOR_PARAM_SPECS`` (consulted by
# ``IndicatorRef._validate_params`` to enforce required keys, default
# fill-ins on optional keys, and per-param type/bounds) and
# ``INDICATOR_HELPER_NAME`` (DSL name -> emitted method name) are imported
# above from ``indicators.registry_metadata`` — the single source of truth
# shared with ``synthesis.compiler``'s emit-args table and both compilers'
# lookback formulas. Re-exported here under their original names so no
# downstream import site (``quality_gates.code_conformance``,
# ``synthesis.compiler``, etc.) needs to change.
# ---------------------------------------------------------------------------

# Explicit raise (survives ``python -O``): a DSL indicator missing from this map would
# make the compiler ``KeyError`` at emit time and the conformance gate miss the call.
if set(INDICATOR_HELPER_NAME) != set(IndicatorName.__args__):
    raise RuntimeError(
        "indicator helper map (INDICATOR_HELPER_NAME) must cover every DSL "
        f"IndicatorName literal; mismatch: {set(IndicatorName.__args__) ^ set(INDICATOR_HELPER_NAME)}"
    )


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

    @property
    def sig_id(self) -> str:
        """Cheap, current-configuration cache key for the indicator cache.

        Replaces ``model_dump_json()`` on the indicator-cache hot path: the
        JSON encoder walked the whole model on every lookup, whereas this is a
        single ``str`` built from the live fields. ``repr`` on each value keeps
        int 14, float 14.0 and str "14" distinct keys — matching the type
        fidelity the previous key provided.

        Derived from the **current** ``name`` / ``source`` / ``params`` on
        every access (these models are mutable), so a ref whose ``params`` or
        ``source`` is changed after construction keys a different cache entry —
        matching the old per-call ``model_dump_json()`` semantics. Not cached
        on the instance for that reason.

        Postconditions: returns a non-empty string; equal current
        configurations return equal ``sig_id``.
        """
        return "|".join(
            [self.name, self.source, *(f"{k}={v!r}" for k, v in sorted(self.params.items()))]
        )

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


# ---------------------------------------------------------------------------
# Boolean predicate combinators. A rule's ``when`` may be a single ``Predicate``
# (leaf) or a nested ``all_of`` / ``any_of`` tree, so a confirmation-stacked
# entry (trend ∧ pullback ∧ volume) is expressible as ONE rule and compiles
# deterministically — the engine evaluates the tree (see
# ``executor.predicate_evaluator.evaluate_tree``); the compiled code emits no
# predicate logic. Combinators carry no operators of their own, only children.
# ---------------------------------------------------------------------------


class AllOf(_SpecNode):
    """Conjunction: satisfied only when EVERY child predicate/tree is satisfied.

    Invariants: ``of`` carries ≥2 children — a 1-child ``all_of`` is just the
    child, so it is rejected to keep the tree in a single canonical shape.
    """

    kind: Literal["all_of"] = "all_of"
    of: list["PredicateTree"] = Field(min_length=2)


class AnyOf(_SpecNode):
    """Disjunction: satisfied when ANY child predicate/tree is satisfied.

    Invariants: ``of`` carries ≥2 children (see :class:`AllOf`).
    """

    kind: Literal["any_of"] = "any_of"
    of: list["PredicateTree"] = Field(min_length=2)


# A predicate position in a rule's ``when``: a single comparison or a nested
# boolean tree. ``Predicate`` (no ``kind``, ``extra="forbid"``) and the
# combinators (``kind`` + ``of``, ``extra="forbid"``) are mutually exclusive
# shapes, so this plain union dispatches unambiguously without an explicit
# discriminator — a leaf dict never validates as a combinator and vice versa.
PredicateTree = Union[Predicate, AllOf, AnyOf]


def iter_leaf_predicates(node: "PredicateTree") -> Iterator[Predicate]:
    """Yield every leaf ``Predicate`` in ``node`` (depth-first, left-to-right).

    Preconditions: ``node`` is a ``Predicate`` / ``AllOf`` / ``AnyOf``.
    Postconditions: yields each leaf once; a bare ``Predicate`` yields itself.
    Single source of the tree recursion, reused by the compiler, the readiness
    gate, the engine's indicator collection, and the conformance fixtures so
    "what predicates does this ``when`` contain" is answered in exactly one place.
    """
    if isinstance(node, Predicate):
        yield node
        return
    for child in node.of:
        yield from iter_leaf_predicates(child)


def iter_tree_indicator_refs(node: "PredicateTree") -> Iterator[IndicatorRef]:
    """Yield every ``IndicatorRef`` on either side of any leaf predicate in ``node``.

    Preconditions: ``node`` is a ``PredicateTree``.
    Postconditions: yields refs in leaf order; the same ref may appear more than
    once (callers dedupe by ``sig_id`` where needed).
    """
    for pred in iter_leaf_predicates(node):
        for side in (pred.lhs, pred.rhs):
            if isinstance(side, IndicatorRef):
                yield side


class EntryRule(_SpecNode):
    kind: Literal["entry"] = "entry"
    side: Literal["long", "short"] = "long"
    when: "PredicateTree"
    note: str = ""


# Back-compat alias — earlier the entry union included an Unparsable variant.
# Issue #537 lifts unparseable handling to the spec level; this alias keeps
# import sites unchanged.
EntryRuleUnion = EntryRule


class StopLossRule(_SpecNode):
    kind: Literal["stop_loss"] = "stop_loss"
    pct: float = Field(gt=0, le=1.0)
    basis: Literal["entry_price", "trailing_high", "trailing_low"] = "entry_price"
    # Execution style for the structured close the engine emits when this rule
    # fires. ``"market"`` (default) emits a guaranteed next-bar-open market close
    # via the bar-by-bar evaluator. For an engine-dispatched entry (not the
    # custom-code path) with a resting-eligible level (``style="market"``,
    # ``basis="entry_price"``, ``0 < pct < 1.0`` — the exact predicate is
    # ``trading_service.service._is_resting_stop_loss``; the stable entry
    # point a caller resolves it through is
    # ``trading_service.service.resolve_resting_stop_loss_attachment``),
    # ``_EngineEntryDispatcher`` can INSTEAD attach a resting STOP order at
    # entry-fill that closes the position intrabar, when the run's feature
    # check (``trading_service.service._resting_stop_loss_enabled``, off by
    # default) selects that mechanism. That resting order's level is
    # re-anchored to the entry's actual fill price at materialization (not
    # the signal-bar-close preview it was first resolved from — see
    # ``StopAttachment.entry_price_pct``), so it always agrees with this
    # bar-by-bar evaluator's own (also fill-anchored) level. The two
    # mechanisms are mutually exclusive by construction, not merely in
    # agreement: when the resting order is selected for this rule, the
    # bar-by-bar evaluator excludes that same rule from its own evaluation
    # (``rule_compiler._filtered_intent_for_rule``'s ``exclude_rule_index``),
    # so exactly one of the two ever acts on a given trigger — never both.
    # ``"limit"`` emits a *resting* STOP_LIMIT at the rule's price floor/ceiling
    # with the limit placed ``limit_offset_pct`` away on the protective side; it
    # may **not** fill on a gap-through (the position stays open), which is the
    # defining, intended trade-off of a stop-limit. ``"market"`` keeps the
    # structured-exit "guaranteed close" invariant; ``"limit"`` relaxes it.
    style: Literal["market", "limit"] = "market"
    # Limit offset as a fraction of the stop level, consulted only when
    # ``style == "limit"``. The limit sits below the stop for a long position
    # (sell-stop-limit) and above it for a short (buy-stop-limit). Required when
    # ``style == "limit"``; forbidden otherwise. Bounded strictly below 1.0: at
    # exactly 1.0 a long-side limit would collapse to ``stop*(1-1)=0`` (a
    # never-filling protective order), so the open interval keeps the limit
    # strictly positive.
    limit_offset_pct: Optional[float] = Field(default=None, gt=0, lt=1.0)
    note: str = ""

    @model_validator(mode="after")
    def _validate_limit_style(self):
        """Tie ``limit_offset_pct`` to ``style`` and restrict limit-style stops to
        a static (``entry_price``) stop level.

        Preconditions: ``style`` and ``basis`` are valid Literals (Pydantic
        enforces this before this validator runs).
        Postconditions: returns ``self`` unchanged when consistent; raises
        ``ValueError`` when ``style == "limit"`` lacks ``limit_offset_pct``, uses
        ``pct >= 1.0``, or uses a trailing basis, or when ``limit_offset_pct`` is
        set without ``style == "limit"``.

        A trailing basis moves the stop level every bar, so a *resting* limit
        order against it would need continuous re-pricing — the same reason the
        bracket path forbids combining ``trail_offset`` with ``limit_offset``. A
        limit-style structured stop therefore requires the static
        ``entry_price`` basis. ``pct`` must be ``< 1.0``: the rule is
        side-agnostic, so it must resolve to a strictly-positive level for either
        side it might apply to, and a long's level ``entry * (1 - pct)`` is
        positive only when ``pct < 1.0`` (a short's ``entry * (1 + pct)`` always
        is). ``pct == 1.0`` resolves a long to price 0, which has no valid
        protective limit.
        """
        if self.style == "limit":
            if self.limit_offset_pct is None:
                raise ValueError(
                    "StopLossRule.style='limit' requires limit_offset_pct "
                    "(the limit's distance from the stop level, as a fraction)"
                )
            # ``StopLossRule`` is side-agnostic — it applies to whichever side
            # the strategy opens — so a limit-style stop must resolve to a
            # strictly-positive level for BOTH sides. A short's level is
            # ``entry * (1 + pct)`` (always > 0), but a long's is
            # ``entry * (1 - pct)``, which is > 0 only when ``pct < 1.0``. So
            # ``pct < 1.0`` is the necessary, sufficient, side-agnostic bound;
            # ``>= 1.0`` (rather than ``== 1.0``) also stays correct if the
            # shared ``pct`` field cap is ever loosened past 1.0.
            if self.pct >= 1.0:
                raise ValueError(
                    "StopLossRule.style='limit' requires pct < 1.0: the rule is "
                    "side-agnostic and a long's resolved level entry*(1-pct) is "
                    "non-positive at pct>=1.0 (price 0), which has no valid "
                    "protective limit"
                )
            if self.basis != "entry_price":
                raise ValueError(
                    "StopLossRule.style='limit' is only supported with "
                    "basis='entry_price'; a resting stop-limit needs a static "
                    "stop level, and a trailing basis re-prices the stop each bar"
                )
        elif self.limit_offset_pct is not None:
            raise ValueError("StopLossRule.limit_offset_pct is only valid when style='limit'")
        return self


class TakeProfitRule(_SpecNode):
    kind: Literal["take_profit"] = "take_profit"
    pct: float = Field(gt=0)
    note: str = ""


# Relative slack absorbing float-summation noise when comparing a ladder's rung
# ``qty_fraction`` values against 1.0 (e.g. ``0.5 + 0.3 + 0.2`` need not be exactly
# 1.0). Single source of the tolerance shared by the DSL validator and the
# downstream readiness gate so the "sums to a full close" boundary never drifts.
LADDER_SUM_TOL = 1e-9


class TakeProfitLevel(_SpecNode):
    """One rung of a laddered (scaled) take-profit.

    ``pct`` is the profit-target MAGNITUDE as a positive fraction off entry (the
    field is ``gt=0``); the direction is implied by the position side — ``0.05``
    means a 5% favourable move, i.e. +5% for a long and −5% for a short. It is
    never a negative value. ``qty_fraction`` is the fraction of the position's
    *original entry quantity* to close when this rung's target is reached.

    Invariants (enforced by the owning :class:`ScaledTakeProfitRule`): within a
    rule the ``pct`` values are strictly increasing (successively higher targets)
    and the ``qty_fraction`` values sum to ``<= 1.0`` (the remainder rides the
    other exits).
    """

    pct: float = Field(gt=0)
    qty_fraction: float = Field(gt=0, le=1.0)
    note: str = ""


class ScaledTakeProfitRule(_SpecNode):
    """Laddered take-profit: close a *fraction* of the position at each of several
    successively higher targets, letting the remainder run.

    Each :class:`TakeProfitLevel` fires at most once per position; a level's close
    is sized as ``qty_fraction * original_entry_qty``. The engine emits one tranche
    per bar (the lowest un-fired rung whose target the bar crosses), so a gap that
    crosses several rungs at once scales out across consecutive bars. The remainder
    ``(1 - sum(qty_fraction))`` is left open for the spec's stop-loss / trailing-stop
    / signal exits to close.
    """

    kind: Literal["scaled_take_profit"] = "scaled_take_profit"
    levels: list[TakeProfitLevel] = Field(min_length=1)
    note: str = ""

    @model_validator(mode="after")
    def _validate_levels(self):
        """Enforce a well-ordered ladder whose tranches do not over-close.

        Preconditions: ``levels`` is non-empty and each level passed its own field
        validation (``pct > 0``, ``0 < qty_fraction <= 1.0``).
        Postconditions: returns ``self`` when ``pct`` is strictly increasing across
        levels and ``sum(qty_fraction) <= 1.0`` (within a tiny tolerance); raises
        ``ValueError`` otherwise. Strictly-increasing ``pct`` guarantees the engine
        fires rungs in target order; the sum bound guarantees a non-negative
        remainder so the ladder never closes more than the position.
        """
        prev_pct: Optional[float] = None
        for level in self.levels:
            if prev_pct is not None and level.pct <= prev_pct:
                raise ValueError(
                    "ScaledTakeProfitRule.levels must have strictly increasing pct "
                    f"(each rung a higher target); got {level.pct} after {prev_pct}"
                )
            prev_pct = level.pct
        total = math.fsum(level.qty_fraction for level in self.levels)
        if total > 1.0 + LADDER_SUM_TOL:
            raise ValueError(
                "ScaledTakeProfitRule level qty_fraction values must sum to <= 1.0 "
                f"(got {total}); the remainder rides the other exit rules"
            )
        return self


class SignalExitRule(_SpecNode):
    kind: Literal["signal_exit"] = "signal_exit"
    when: "PredicateTree"
    note: str = ""


class BracketStopLeg(_SpecNode):
    """Stop-loss leg of an :class:`OcoBracketRule`.

    ``pct`` is the protective distance off the entry reference price as a
    positive fraction (``0.03`` = 3%), bounded ``< 1.0`` so a long's resolved
    level ``ref * (1 - pct)`` stays strictly positive (the leg is side-agnostic,
    the same reasoning as :class:`StopLossRule`). ``style`` mirrors
    :class:`StopLossRule`: ``"market"`` materializes a plain STOP child;
    ``"limit"`` materializes a STOP_LIMIT whose limit sits ``limit_offset_pct``
    (of the stop level) on the protective side. The bracket stop is always
    entry-anchored (static) — a trailing basis would re-price the resting child
    every bar — so there is no ``basis`` field.

    Preconditions: ``pct`` in ``(0, 1)``; ``limit_offset_pct`` in ``(0, 1)`` and
    set iff ``style == "limit"``.
    Postconditions: a validated leg whose ``style`` / ``limit_offset_pct`` are
    mutually consistent.
    """

    pct: float = Field(gt=0, lt=1.0)
    style: Literal["market", "limit"] = "market"
    limit_offset_pct: Optional[float] = Field(default=None, gt=0, lt=1.0)
    note: str = ""

    @model_validator(mode="after")
    def _validate_limit_style(self):
        """Tie ``limit_offset_pct`` to ``style`` (same coupling as
        :meth:`StopLossRule._validate_limit_style`, minus the basis restriction —
        a bracket stop has no trailing basis to forbid).

        Preconditions: ``style`` is a valid Literal (Pydantic-enforced).
        Postconditions: returns ``self`` when consistent; raises ``ValueError``
        when ``style == "limit"`` lacks ``limit_offset_pct`` or when
        ``limit_offset_pct`` is set without ``style == "limit"``.
        """
        if self.style == "limit":
            if self.limit_offset_pct is None:
                raise ValueError(
                    "BracketStopLeg.style='limit' requires limit_offset_pct "
                    "(the limit's distance from the stop level, as a fraction)"
                )
        elif self.limit_offset_pct is not None:
            raise ValueError("BracketStopLeg.limit_offset_pct is only valid when style='limit'")
        return self


class BracketTakeProfitLeg(_SpecNode):
    """Take-profit leg of an :class:`OcoBracketRule`.

    ``pct`` is the favourable-move target off the entry reference price as a
    positive fraction in ``(0, 1)``; the direction is implied by the position
    side (``+pct`` for a long, ``−pct`` for a short). Materializes a resting LIMIT
    child that fills at its exact limit price (the OCO bracket's defining
    advantage over the bar-by-bar ``take_profit`` market close).

    ``pct`` is bounded ``< 1.0`` for the same side-agnostic reason as
    :class:`BracketStopLeg` / :class:`StopLossRule`: a short's resolved target is
    ``ref * (1 - pct)``, which is strictly positive only when ``pct < 1.0`` (at
    ``pct >= 1.0`` it collapses to a non-positive, never-filling limit). A
    long-only strategy wanting a >100% target can still author it via the
    standalone ``take_profit`` rule, which is not side-agnostic.
    """

    pct: float = Field(gt=0, lt=1.0)
    note: str = ""


class OcoBracketRule(_SpecNode):
    """One-cancels-other bracket: a protective stop leg and a profit-target leg
    attached to the entry order as a single OCO group.

    On entry-fill the engine materializes the two legs into resting opposite-side
    child orders (a STOP / STOP_LIMIT and a LIMIT) sharing one ``oco_group_id``;
    when either fills the engine cancels its sibling
    (``OrderBook.oco_cancel_siblings``). Unlike the independent ``stop_loss`` /
    ``take_profit`` rules — which the engine evaluates bar-by-bar and closes at
    market — a bracket's take-profit rests as a LIMIT and fills at its exact
    price. The bracket is a *full-position* OCO: whichever leg fills closes the
    whole position.

    Invariants (enforced at the :class:`StrategySpec` level): a spec carries at
    most one bracket, and a bracket is the sole engine-handled *price* exit (no
    ``stop_loss`` / ``take_profit`` / ``scaled_take_profit`` alongside it). A
    ``signal_exit`` may coexist as a secondary discretionary trigger.
    """

    kind: Literal["oco_bracket"] = "oco_bracket"
    stop_loss: "BracketStopLeg"
    take_profit: "BracketTakeProfitLeg"
    note: str = ""


def ladder_closes_full_position(rule: "ScaledTakeProfitRule") -> bool:
    """Whether a laddered take-profit's rungs together close the WHOLE position.

    Preconditions: ``rule`` is a ``ScaledTakeProfitRule`` (its ``qty_fraction``
    values sum to ``<= 1.0`` by construction).
    Postconditions: ``True`` iff the rung fractions sum to ``1.0`` within
    ``LADDER_SUM_TOL`` — i.e. the ladder leaves no residual for another exit to
    close. Single source of the "ladder is a full close" test shared by the
    readiness gate.
    """
    return math.fsum(level.qty_fraction for level in rule.levels) >= 1.0 - LADDER_SUM_TOL


def is_partial_exit(rule: Any) -> bool:
    """Whether ``rule`` only PARTIALLY closes the position.

    A scaled ladder is a partial exit ONLY when its rung fractions sum to < 1.0,
    leaving a residual for another exit to close; a ladder that sums to 1.0 closes
    the full position over its rungs and is a full-position exit (see
    :func:`is_full_position_exit`). Canonical single-rule classifier.
    Preconditions: ``rule`` is an ``ExitRule`` member.
    Postconditions: ``True`` iff ``rule`` is a ``ScaledTakeProfitRule`` whose rungs
    sum to < 1.0.
    """
    return isinstance(rule, ScaledTakeProfitRule) and not ladder_closes_full_position(rule)


def is_full_position_exit(rule: Any) -> bool:
    """Whether ``rule`` fully closes the position.

    A stop-loss / take-profit / signal-exit each close the full position in a
    single firing; a scaled ladder closes it across its rungs when (and only when)
    those rungs sum to 1.0 (``ladder_closes_full_position``). A ladder summing to
    < 1.0 is partial (see :func:`is_partial_exit`). Canonical single source of the
    full-position membership so a new exit kind is classified in one place.
    Preconditions: ``rule`` is an ``ExitRule`` member.
    Postconditions: ``True`` iff ``rule`` is a stop-loss, take-profit, signal exit,
    OCO bracket, or a ``ScaledTakeProfitRule`` whose rungs sum to 1.0.
    """
    if isinstance(rule, (StopLossRule, TakeProfitRule, SignalExitRule, OcoBracketRule)):
        return True
    if isinstance(rule, ScaledTakeProfitRule):
        return ladder_closes_full_position(rule)
    return False


def is_bracket_exit(rule: Any) -> bool:
    """Whether ``rule`` is an engine-native OCO bracket (vs. a bar-by-bar exit).

    A bracket is attached to the entry order and materialized by the engine into
    resting OCO children; the bar-by-bar exit dispatcher must NOT also evaluate
    it (dual emission). Canonical single source of the "is this the attach-to-
    entry bracket kind" test shared by the entry dispatcher and the gates.
    Preconditions: ``rule`` is an ``ExitRule`` member.
    Postconditions: ``True`` iff ``rule`` is an ``OcoBracketRule``.
    """
    return isinstance(rule, OcoBracketRule)


def is_engine_handled_exit(rule: Any) -> bool:
    """Whether the engine enforces ``rule`` (vs. it being strategy-code-owned).

    Every structured exit rule is engine-enforced — it is either a full-position
    close or a partial scale-out — so this is exactly the union of
    :func:`is_full_position_exit` and :func:`is_partial_exit`. Defining it in terms
    of those two keeps the partition explicit and removes a separate membership
    list to maintain. Preconditions: ``rule`` is an ``ExitRule`` member.
    Postconditions: ``True`` for any ``ExitRule`` member.
    """
    return is_full_position_exit(rule) or is_partial_exit(rule)


def is_entry_anchored_exit(rule: Any) -> bool:
    """Whether the engine enforces ``rule`` against ``position.entry_price``.

    Stop-loss / take-profit / scaled-take-profit all trigger on price relative to
    the entry, so a compiled custom strategy must expose that binding; a signal
    exit compares indicators, not the entry price, so it is excluded. Canonical
    source of the "needs entry_price" membership used by the synthesis compiler.
    Preconditions: ``rule`` is an ``ExitRule`` member. Postconditions: ``True`` iff
    ``rule`` is a stop-loss, take-profit, or scaled take-profit.
    """
    return isinstance(rule, (StopLossRule, TakeProfitRule, ScaledTakeProfitRule))


def stop_caps_side(basis: str, side: str) -> bool:
    """Whether a ``StopLossRule`` ``basis`` can cap loss for an entry ``side``.

    Mirrors the executor's stop-trigger semantics (``rule_compiler.stop_loss_triggers``):
    a ``trailing_high`` stop only fires for a LONG (it tracks the running high
    and floors below it), ``trailing_low`` only fires for a SHORT, and
    ``entry_price`` fires for both. A stop whose basis cannot fire for the
    position's side never caps that side's loss — the executor no-ops it.

    Preconditions: ``side`` is ``"long"`` or ``"short"``.
    Postconditions: returns ``True`` iff a stop with ``basis`` can trigger for a
    position on ``side``.
    """
    assert side in ("long", "short"), "side must be 'long' or 'short'"
    if basis == "entry_price":
        return True
    if basis == "trailing_high":
        return side == "long"
    if basis == "trailing_low":
        return side == "short"
    return False  # pragma: no cover - DSL Literal forbids other bases


def protective_limit_price(stop_price: float, offset: float, *, closing_long: bool) -> float:
    """Place a stop-limit's limit on the protective side of its stop.

    Closing a long is a sell, so the limit sits *below* the stop
    (``stop - offset``); closing a short is a buy, so it sits *above*
    (``stop + offset``). This is the single source of the sign convention shared
    by the DSL structured-exit path (``rule_compiler``) and the bracket-child
    materializer (``fill_simulator``), and it matches ``OrderRequest.validate_prices``
    (SHORT close requires ``limit <= stop``; LONG close requires ``limit >= stop``).

    This helper owns only the *sign* convention; keeping the limit strictly
    positive is the caller's responsibility. The DSL path bounds
    ``limit_offset_pct < 1.0`` so ``offset < stop_price`` and the long-side limit
    stays positive (re-asserted in ``_build_stop_limit_close``); the bracket path
    accepts absolute offsets and owns its own bounds.

    Preconditions: ``stop_price > 0`` and ``offset >= 0``.
    Postconditions: returns ``stop_price - offset`` when ``closing_long`` (limit
    on/below the stop), else ``stop_price + offset`` (limit on/above the stop).
    """
    assert stop_price > 0, "stop_price must be positive"
    assert offset >= 0, "offset must be non-negative"
    return stop_price - offset if closing_long else stop_price + offset


def protective_stop_price(ref_price: float, pct: float, *, is_long: bool) -> float:
    """Floor/cap price for a stop anchored at ``pct`` off ``ref_price``.

    A long's protective level sits *below* the reference (``ref * (1 - pct)``);
    a short's sits *above* it (``ref * (1 + pct)``). This is the single source
    of that geometry, shared by every consumer that must derive the same level
    from the same reference and never drift apart: the bar-close evaluator
    (``rule_compiler.stop_loss_level``, for both the static ``entry_price``
    basis and the trailing bases — trailing only changes which price is passed
    as ``ref_price``, not the formula), the generalized exit-leg resolver
    (``trading_service.service.resolve_exit_leg_attachments``, whose preview
    anchors ``ref_price`` at the signal bar's close), and the resting-child
    materializer (``trading_service.engine.fill_simulator._materialize_stop_child``,
    which re-anchors ``ref_price`` at the entry's actual fill price for a leg
    carrying ``StopAttachment.entry_price_pct``). Before this helper existed,
    each of those three re-implemented the same two-line formula independently
    — durable only as long as no one edited one copy without the others, which
    is exactly the class of bug the resting-child re-anchor was added to fix
    (see that materializer's own comment).

    Preconditions: ``ref_price`` and ``pct`` are plain floats (no
    finiteness/sign/bound constraint here — callers validate the result
    against their own contract; ``ExitLegSpec``/``_is_resting_stop_loss``
    bound ``pct`` to ``(0, 1)`` before it reaches here, ``rule_compiler.
    stop_loss_level`` passes ``StopLossRule.pct`` bounded to ``(0, 1.0]``
    (a long at ``pct == 1.0`` resolves to level 0, which never triggers
    against positive bar prices), and the STOP-family branch of
    ``resolve_exit_leg_attachments`` validates the resolved price
    afterward).
    Postconditions: returns ``ref_price * (1 - pct)`` when ``is_long``, else
    ``ref_price * (1 + pct)``.
    """
    return ref_price * (1.0 - pct) if is_long else ref_price * (1.0 + pct)


def first_side_stop_factor(exit_rules: Sequence[Any], side: str) -> Optional[float]:
    """Worst-case stop fraction for ``side``: the FIRST side-compatible
    ``StopLossRule.pct`` in spec order, or ``None`` when no stop can fire for it.

    The engine's ``evaluate_exit_rules`` breaks on the first triggered rule in
    spec order, so on a gap that crosses several stops at once the earliest
    (possibly loosest) side-compatible stop wins — its pct bounds the modeled
    realised loss. A later stop only wins when no earlier one triggered, which
    for a monotonic move means it is tighter, so the first side-compatible stop
    is the true worst case. ``TradingService`` uses the ``None`` result to detect
    a side with no effective stop (the short-safety auto-stop injection).

    A ``style="limit"`` stop is still counted as an effective stop here: it
    bounds the *intended* loss at the same ``pct`` and so still suppresses the
    short-safety auto-stop injection. A gap-through that leaves a limit-style
    stop unfilled can let *realised* loss exceed ``pct`` — that residual risk is
    surfaced via ``stop_limit_unfilled_triggers`` telemetry, not by treating the
    rule as "no stop" (which would inject a redundant second stop).

    An ``OcoBracketRule``'s stop leg also counts: it is a static (entry-anchored)
    stop that caps loss for either side at its ``pct``, so a short carrying a
    bracket already has an effective stop and must not get the redundant 100%
    auto-stop injection.

    Preconditions: ``side`` is ``"long"`` or ``"short"``.
    Postconditions: returns the first matching stop fraction as a float — a
    side-compatible ``StopLossRule.pct`` or an ``OcoBracketRule`` stop-leg
    ``pct`` — else ``None``.
    """
    for r in exit_rules:
        if isinstance(r, StopLossRule) and stop_caps_side(r.basis, side):
            return float(r.pct)
        if isinstance(r, OcoBracketRule):
            # The bracket stop is entry-anchored (static), so it caps both sides.
            return float(r.stop_loss.pct)
    return None


# Exit rules: structured close conditions the engine enforces (stop-loss /
# take-profit) plus signal-based exits.  The union is intentionally limited
# to price-, P&L-, and signal-based exits.
ExitRule = Annotated[
    Union[StopLossRule, TakeProfitRule, ScaledTakeProfitRule, SignalExitRule, OcoBracketRule],
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
AllOf.model_rebuild()
AnyOf.model_rebuild()
EntryRule.model_rebuild()
StopLossRule.model_rebuild()
TakeProfitRule.model_rebuild()
TakeProfitLevel.model_rebuild()
ScaledTakeProfitRule.model_rebuild()
SignalExitRule.model_rebuild()
BracketStopLeg.model_rebuild()
BracketTakeProfitLeg.model_rebuild()
OcoBracketRule.model_rebuild()
FixedFractionSizing.model_rebuild()
VolatilityTargetSizing.model_rebuild()
FixedNotionalSizing.model_rebuild()


# TypeAdapters expose discriminator dispatch for callers that need to
# validate a raw dict without pre-selecting the concrete class.
IndicatorRefAdapter: TypeAdapter = TypeAdapter(IndicatorRef)
PredicateTreeAdapter: TypeAdapter = TypeAdapter(PredicateTree)
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
    # `repr(x)` is the shortest string that round-trips, but for small magnitudes
    # (e.g. 1e-05) it uses scientific notation the adapter regex can't parse.
    # Decimal(repr(x)) keeps those exact digits; the "f" format forces plain decimal.
    return format(Decimal(repr(x)), "f")


def _with_source(base: str, source: str) -> str:
    """Append ``, source=X`` (or ``source=X`` for arg-less calls) when non-default."""
    if source == "close":
        return base
    assert base.endswith(")")
    inner = base[:-1]
    if inner.endswith("("):
        return f"{inner}source={source})"
    return f"{inner}, source={source})"


def _format_period_indicator(ref: IndicatorRef) -> str:
    """Format the `name(period)` shape shared by single-period indicators.

    Covers both source-aware indicators (sma/ema/rsi/roc) and
    source-forbidding ones (atr/adx/vwap/mfi/cci/williams_r) — for the
    latter ``ref.source`` is always ``"close"`` (enforced by
    ``IndicatorRef._validate_params``), so ``_with_source`` is a no-op.
    """
    return _with_source(f"{ref.name}({ref.param('period')})", ref.source)


def _format_macd(ref: IndicatorRef) -> str:
    output = ref.param("output")
    macd_name = "macd" if output == "macd" else f"macd_{output}"
    base = f"{macd_name}({ref.param('fast')},{ref.param('slow')},{ref.param('signal')})"
    return _with_source(base, ref.source)


def _format_bollinger(ref: IndicatorRef) -> str:
    period = ref.param("period")
    num_std = float(ref.param("num_std"))
    band = ref.param("band")
    base = f"bollinger_{band}({period},{_format_number(num_std)})"
    return _with_source(base, ref.source)


def _format_stochastic(ref: IndicatorRef) -> str:
    output = ref.param("output")
    return f"stochastic_{output}({ref.param('k_period')},{ref.param('d_period')})"


def _format_donchian(ref: IndicatorRef) -> str:
    band = ref.param("band")
    return f"donchian_{band}({ref.param('period')})"


def _format_keltner(ref: IndicatorRef) -> str:
    band = ref.param("band")
    multiplier = float(ref.param("multiplier"))
    return (
        f"keltner_{band}({ref.param('period')},{ref.param('atr_period')},"
        f"{_format_number(multiplier)})"
    )


def _format_obv(ref: IndicatorRef) -> str:
    return "obv()"


# Dict dispatch replaces a 16-branch if/elif chain: adding an indicator's
# formatter is a single entry here instead of a new branch, mirroring the
# dict-keyed convention already used for this indicator list elsewhere
# (``INDICATOR_HELPER_NAME`` / ``INDICATOR_PARAM_SPECS`` in
# ``indicators.registry_metadata``).
_INDICATOR_FORMATTERS: dict[IndicatorName, Callable[[IndicatorRef], str]] = {
    "sma": _format_period_indicator,
    "ema": _format_period_indicator,
    "rsi": _format_period_indicator,
    "macd": _format_macd,
    "bollinger": _format_bollinger,
    "atr": _format_period_indicator,
    "adx": _format_period_indicator,
    "stochastic": _format_stochastic,
    "vwap": _format_period_indicator,
    "donchian": _format_donchian,
    "keltner": _format_keltner,
    "obv": _format_obv,
    "mfi": _format_period_indicator,
    "roc": _format_period_indicator,
    "cci": _format_period_indicator,
    "williams_r": _format_period_indicator,
}

# Explicit raise (mirrors the ``INDICATOR_HELPER_NAME`` guard above): a DSL
# indicator missing a formatter here would otherwise only surface as a
# ``TypeError`` the first time an LLM prompt renders that indicator.
if set(_INDICATOR_FORMATTERS) != set(IndicatorName.__args__):
    raise RuntimeError(
        "indicator formatter map (_INDICATOR_FORMATTERS) must cover every DSL "
        f"IndicatorName literal; mismatch: {set(IndicatorName.__args__) ^ set(_INDICATOR_FORMATTERS)}"
    )


def _format_indicator_ref(ref: IndicatorRef) -> str:
    """Render an `IndicatorRef` to prose via `_INDICATOR_FORMATTERS`."""
    formatter = _INDICATOR_FORMATTERS.get(ref.name)
    if formatter is None:
        raise TypeError(f"unknown IndicatorRef name: {ref.name!r}")
    return formatter(ref)


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


def format_predicate_tree(node: "PredicateTree", *, leaf_formatter=_format_predicate) -> str:
    """Render a predicate tree, parameterised by the per-leaf renderer.

    A leaf ``Predicate`` renders via ``leaf_formatter`` (default: the prose
    ``_format_predicate``); an ``all_of`` / ``any_of`` renders as a parenthesised
    ``(c1 and c2 …)`` / ``(c1 or c2 …)`` so the boolean structure is unambiguous.

    The single source of the tree-walk: prompt rendering uses the default prose
    leaf, while the alignment gate passes its own ``repr``-style leaf renderer —
    so neither re-implements the recursion.

    Preconditions: ``node`` is a ``Predicate`` / ``AllOf`` / ``AnyOf``;
    ``leaf_formatter`` maps a ``Predicate`` to ``str``.
    Postconditions: returns a non-empty string.
    """
    if isinstance(node, Predicate):
        return leaf_formatter(node)
    joiner = " and " if isinstance(node, AllOf) else " or "
    return (
        "("
        + joiner.join(
            format_predicate_tree(child, leaf_formatter=leaf_formatter) for child in node.of
        )
        + ")"
    )


_STOP_LOSS_BASIS_PREFIX: dict[str, str] = {
    "trailing_high": "trailing-high ",
    "trailing_low": "trailing-low ",
}


def _format_rule(
    rule: Union[
        EntryRule,
        StopLossRule,
        TakeProfitRule,
        ScaledTakeProfitRule,
        SignalExitRule,
        OcoBracketRule,
    ],
) -> str:
    if isinstance(rule, EntryRule):
        return f"{rule.side} when {format_predicate_tree(rule.when)}"
    if isinstance(rule, StopLossRule):
        prefix = _STOP_LOSS_BASIS_PREFIX.get(rule.basis, "")
        base = f"{prefix}stop loss {_format_number(rule.pct * 100)}%"
        if rule.style == "limit":
            return f"{base} (limit, {_format_number(rule.limit_offset_pct * 100)}% offset)"
        return base
    if isinstance(rule, TakeProfitRule):
        return f"take profit {_format_number(rule.pct * 100)}%"
    if isinstance(rule, ScaledTakeProfitRule):
        rungs = ", ".join(
            f"{_format_number(level.qty_fraction * 100)}% at {_format_number(level.pct * 100)}%"
            for level in rule.levels
        )
        return f"scaled take profit ({rungs})"
    if isinstance(rule, SignalExitRule):
        return f"exit when {format_predicate_tree(rule.when)}"
    if isinstance(rule, OcoBracketRule):
        sl = rule.stop_loss
        stop = f"stop {_format_number(sl.pct * 100)}%"
        if sl.style == "limit":
            stop = f"{stop} (limit, {_format_number(sl.limit_offset_pct * 100)}% offset)"
        return f"OCO bracket: {stop} / target {_format_number(rule.take_profit.pct * 100)}%"
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
