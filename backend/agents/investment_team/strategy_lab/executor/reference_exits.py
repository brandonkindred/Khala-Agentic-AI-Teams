"""Exit-side replay for the reference-ledger simulator.

Second half of the reference-ledger simulator designed in
``system_design/reference_ledger_trade_model.md``, attaching exit modelling to
the entry-side replay in :mod:`reference_entries`. All four exit rule kinds
are covered: ``StopLossRule`` across all four variants (``entry_price`` /
trailing bases x ``market`` / ``limit`` styles), the take-profit family —
standalone ``TakeProfitRule`` and laddered ``ScaledTakeProfitRule`` — and
``SignalExitRule``. The combined simulator that lets every kind compete on the
same bar is a later step; each kind is still replayed here as if the others'
rules did not exist.

Three of the four are RESTING-ORDER kinds; ``SignalExitRule`` is not
------------------------------------------------------------------
The stop-loss and take-profit families are modeled as orders resting on the
book: they fill on their own trigger bar, at a level fixed in advance, and are
not eligible on the bar their order materializes. ``SignalExitRule`` is
deliberately NOT modeled that way — see :func:`resolve_signal_exit` for the
full argument and the two concrete behavioral differences (entry-bar trigger
eligibility, and a fill deferred to the next bar's open).

Trigger decisions and per-position rule-priority resolution are NOT
re-derived here, for any exit kind this module covers: stop-loss comes
from the shared, pure ``rule_compiler.stop_loss_triggers`` (and its
``stop_loss_level`` / ``stop_limit_prices`` geometry), and the take-profit
family's trigger decision AND its per-bar winner resolution both come from
``rule_compiler.evaluate_exit_rules_for_position`` — the same functions the
live evaluator and the post-hoc conformance gate call, per the design doc's
Reuse mandate. A ``PositionState`` whose ``high_since_entry``/
``low_since_entry`` are held FIXED at the post-slippage anchor (never
extended bar-over-bar) makes that shared evaluator's watermark-based
ladder-rung test provably equivalent to a current-bar-only test for these
fixed targets — see :class:`_RestingTakeProfitFamily`'s docstring for the
proof — so reusing it does not reintroduce the fabricated-fill risk a naive
pass-through of the live evaluator's own extended watermark would. What this
module adds — and what the shared evaluator does not cover — is resting-order
FILL mechanics: which bar an order fills on, at what price, gap handling, the
stop's trailing watermark ratchet, the stop-limit arm/latch, and the
take-profit ladder's rung cursor / materialization lifecycle and quantity
bookkeeping.

Target behavior, not shipped behavior
-------------------------------------
Today a ``style="market"`` stop is detected at bar close by the live exit
dispatcher and closed at the NEXT bar's open. The resting-order migration that
replaces that is still in flight. This module models the post-migration
semantics the design doc specifies (fill on the trigger bar, at the stop level
or the worse open on a gap) precisely because modelling the current
approximation would make every stop-loss trade diverge trivially the moment
that migration lands. The design doc's own section 1 mandates this and tells
the later trade-matching module to read the interim fill-mechanics gap as
expected noise rather than a spec/engine mismatch.

Exclusions
----------
Per the design doc's module boundary, nothing here imports — directly or
transitively — ``trading_service/service.py`` or
``trading_service/engine/{fill_simulator,order_book,execution_model,portfolio}.py``.
The fill semantics those modules implement are mirrored at the semantic level,
as new pure code.

Scope limits deliberately left to later steps
---------------------------------------------
* ``replay_entry_rules`` opens at most one position per symbol and never
  re-enters, so this returns at most one exit per symbol. Re-entry after a
  stop or take-profit closes a position needs the combined driver that
  resolves exits before entries on each bar.
* No quantity/sizing, capital ledger, or risk-limit admission gates; no
  cross-symbol merged ``(timestamp, symbol)`` timeline; no competition between
  the two families this module DOES cover and the ones it does not
  (``StopLossRule`` vs. the take-profit family vs. ``SignalExitRule``) — each
  of the three replays over the same spec is modeled as if the other kinds'
  rules did not exist, so any of them may fire on a bar where the full
  simulator would not have had a position left to close. In particular the
  design doc's FIFO precedence rule — a resting order materialized at entry
  beats a ``signal_exit`` close queued on the same bar — has no expression
  here, because no single walk in this module ever sees both.
* The take-profit family models no true multi-bar entry continuation: this
  simulator's entries always fill instantaneously in one shot
  (:func:`~.reference_entries.replay_entry_rules`), so the live engine's
  ``entry_continuation_in_flight`` deferral is vacuously always satisfied
  here. Both it and the engine's per-rung ``scaled_partial_in_flight``
  deferral collapse to the same structural fact in this module: the bar walk
  starts at ``entry_bar + 1`` and calls ``_RestingTakeProfitFamily.step``
  exactly once per bar, strictly in order, so no candidate — first or
  ladder-advanced — is ever examined before the bar after it can legally
  fire. No separate "eligible bar" state is needed to enforce that; see
  ``_LadderCursor``'s and ``_RestingTakeProfitFamily``'s own docstrings.
* ``ReferenceStopLossExit``/``ReferenceTakeProfitExit``/``ReferenceSignalExit``
  are correspondingly narrower than the design doc's ``ReferenceTrade``,
  exactly as ``ReferenceEntryFill`` is on the entry side: their fields match
  ``ReferenceTrade``'s exit-side fields 1:1 in name/type/semantics so a later
  step can join an entry fill and an exit into a full ``ReferenceTrade``
  without renaming anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Literal, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from ...models import StrategySpec
from ..spec_dsl import (
    ExitRule,
    IndicatorRef,
    ScaledTakeProfitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    first_side_stop_factor,
    stop_caps_side,
)
from .predicate_evaluator import HistoryView, PandasHistoryView
from .reference_entries import ReferenceEntryFill, bars_to_frame, replay_entry_rules
from .rule_compiler import (
    BarSnapshot,
    PositionState,
    evaluate_exit_rules_for_position,
    stop_limit_prices,
    stop_loss_level,
    stop_loss_triggers,
)

if TYPE_CHECKING:
    # Deferred for the same reason ``reference_entries`` defers it: importing
    # any ``trading_service`` submodule at runtime executes
    # ``trading_service/__init__.py`` -> ``service.py``, which top-level-imports
    # all four forbidden ``engine/`` modules. ``from __future__ import
    # annotations`` makes every annotation here a string, so the name below is
    # never resolved at runtime.
    from ...trading_service.strategy.contract import Bar

# The engine appends this synthetic stop to any spec that can go short but
# carries no effective short-side stop, so an unbounded short cannot run away.
# Mirrors the live service's own injection; kept as module constants so the
# reference model and a future reader can see the exact shape being reproduced.
_SHORT_SAFETY_STOP_PCT = 1.0
_SHORT_SAFETY_STOP_BASIS = "entry_price"

# Mirrors trading_service/engine/order_book.py's FILL_QTY_REL_TOL — the
# relative tolerance production uses to decide a position is "closed enough"
# despite floating-point summation noise in a ladder's rung quantities. Not
# imported: importing anything from trading_service/engine/* is forbidden
# (see the module docstring's Exclusions section and
# ``test_module_imports_no_forbidden_engine_module``). Kept as a local
# constant, the same treatment ``_SHORT_SAFETY_STOP_PCT`` already gets for its
# own production mirror. NOT interchangeable with ``spec_dsl.LADDER_SUM_TOL``
# (1e-9): that is the DSL validator's bound on a ladder's rung-fraction sum,
# unrelated to this runtime closure test.
_FILL_QTY_REL_TOL = 1e-12


@dataclass(frozen=True)
class ReferenceStopLossExit:
    """One reference position closed by a modeled ``StopLossRule``.

    Narrower than the design doc's ``ReferenceTrade`` — that record additionally
    needs a resolved ``qty`` and the entry-side fields, neither of which this
    module owns. Every field below matches ``ReferenceTrade``'s same-named field
    in type and semantics, so joining one of these with its
    :class:`~.reference_entries.ReferenceEntryFill` yields a ``ReferenceTrade``
    with no renaming.

    ``level_index`` is absent by design rather than present-and-``None``: the
    design doc makes it meaningful only for a ``scaled_take_profit`` close, and
    a field that can only ever hold one value carries no information here.

    Invariants (all enforced in ``__post_init__``): ``0 <= entry_bar <
    exit_bar``; ``exit_price`` is finite and positive; ``exit_rule_index >= 0``;
    ``exit_rule_kind == "stop_loss"``.
    """

    symbol: str
    entry_bar: int
    exit_bar: int
    exit_date: str
    exit_price: float
    exit_rule_kind: Literal["stop_loss"]
    exit_rule_index: int

    def __post_init__(self) -> None:
        """Enforce this record's structural contract at construction.

        Fail-fast at construction rather than trusting the one producer below,
        matching ``ExitIntent`` and ``ReferenceEntryFill``: a record built
        directly by a test or a later matching module's adapter cannot exist in
        an invalid state either.

        Preconditions: none beyond typing.
        Postconditions: raises ``ValueError`` when ``entry_bar < 0``,
        ``exit_bar <= entry_bar``, ``exit_rule_index < 0``, ``exit_price`` is
        not a positive finite number, or ``exit_rule_kind`` is anything but
        ``"stop_loss"``; otherwise the instance is structurally valid.
        ``symbol`` and ``exit_date`` are recorded as given, not validated —
        this module has no basis to reject a symbol or date string beyond
        typing, the same stance ``ReferenceEntryFill`` takes.
        """
        if self.entry_bar < 0:
            raise ValueError(f"entry_bar must be >= 0, got {self.entry_bar!r}")
        if self.exit_bar <= self.entry_bar:
            # Strict: a resting stop is not eligible until ``entry_bar + 1``, so
            # a same-bar close is unrepresentable by construction, mirroring the
            # design doc's ``entry_bar < exit_bar`` ReferenceTrade invariant.
            raise ValueError(
                f"exit_bar must be > entry_bar ({self.entry_bar!r}), got {self.exit_bar!r}"
            )
        if self.exit_rule_index < 0:
            raise ValueError(f"exit_rule_index must be >= 0, got {self.exit_rule_index!r}")
        if not (self.exit_price > 0 and math.isfinite(self.exit_price)):
            raise ValueError(
                f"exit_price must be a positive finite number, got {self.exit_price!r}"
            )
        if self.exit_rule_kind != "stop_loss":
            raise ValueError(f"exit_rule_kind must be 'stop_loss', got {self.exit_rule_kind!r}")


@dataclass(frozen=True)
class ReferenceTakeProfitExit:
    """One reference position closed by a modeled take-profit-family rule.

    A single record type serves BOTH ``TakeProfitRule`` and
    ``ScaledTakeProfitRule`` closes, unlike the separate ``ReferenceStopLossExit``:
    a combined resolver (see :class:`_RestingTakeProfitFamily`) races a spec's
    standalone targets and ladder rungs against each other bar by bar, so it
    must already return one unified outcome type — two record classes would
    just need an artificial union wrapping them for no benefit. Every field
    matches ``ReferenceTrade``'s same-named field in type and semantics, same
    as ``ReferenceStopLossExit``.

    For a ``scaled_take_profit`` close, ``level_index`` identifies the RUNG
    THAT FINALLY CLOSED the position — not every rung that contributed to
    ``exit_price`` along the way (a ladder whose first two rungs fired before a
    third rung emptied the position still records only the third rung's
    index). A ``take_profit`` close never carries a rung, so ``level_index`` is
    ``None`` there — the same "absent rather than present-and-meaningless"
    stance ``ReferenceStopLossExit`` takes by omitting the field outright,
    adapted here to an ``Optional`` field instead because this record type
    must represent both shapes.

    Invariants (all enforced in ``__post_init__``): ``0 <= entry_bar <
    exit_bar``; ``exit_price`` is finite and positive; ``exit_rule_index >= 0``;
    ``exit_rule_kind`` is ``"take_profit"`` or ``"scaled_take_profit"``;
    ``level_index`` is populated (and ``>= 0``) if and only if
    ``exit_rule_kind == "scaled_take_profit"``.
    """

    symbol: str
    entry_bar: int
    exit_bar: int
    exit_date: str
    exit_price: float
    exit_rule_kind: Literal["take_profit", "scaled_take_profit"]
    exit_rule_index: int
    level_index: Optional[int] = None

    def __post_init__(self) -> None:
        """Enforce this record's structural contract at construction.

        Fail-fast at construction, matching ``ReferenceStopLossExit``: a
        record built directly by a test or a later matching module's adapter
        cannot exist in an invalid state either.

        Preconditions: none beyond typing.
        Postconditions: raises ``ValueError`` when ``entry_bar < 0``,
        ``exit_bar <= entry_bar``, ``exit_rule_index < 0``, ``exit_price`` is
        not a positive finite number, ``exit_rule_kind`` is anything but
        ``"take_profit"``/``"scaled_take_profit"``, or ``level_index``'s
        presence disagrees with ``exit_rule_kind`` (populated for
        ``"take_profit"``, absent or negative for ``"scaled_take_profit"``);
        otherwise the instance is structurally valid. ``symbol`` and
        ``exit_date`` are recorded as given, not validated, the same stance
        ``ReferenceStopLossExit`` takes.
        """
        if self.entry_bar < 0:
            raise ValueError(f"entry_bar must be >= 0, got {self.entry_bar!r}")
        if self.exit_bar <= self.entry_bar:
            raise ValueError(
                f"exit_bar must be > entry_bar ({self.entry_bar!r}), got {self.exit_bar!r}"
            )
        if self.exit_rule_index < 0:
            raise ValueError(f"exit_rule_index must be >= 0, got {self.exit_rule_index!r}")
        if not (self.exit_price > 0 and math.isfinite(self.exit_price)):
            raise ValueError(
                f"exit_price must be a positive finite number, got {self.exit_price!r}"
            )
        if self.exit_rule_kind not in ("take_profit", "scaled_take_profit"):
            raise ValueError(
                "exit_rule_kind must be 'take_profit' or 'scaled_take_profit', "
                f"got {self.exit_rule_kind!r}"
            )
        if self.exit_rule_kind == "scaled_take_profit":
            if self.level_index is None:
                raise ValueError(
                    "level_index is required when exit_rule_kind is 'scaled_take_profit'"
                )
            if self.level_index < 0:
                raise ValueError(f"level_index must be >= 0, got {self.level_index!r}")
        elif self.level_index is not None:
            raise ValueError(
                f"level_index must be None when exit_rule_kind is 'take_profit', got {self.level_index!r}"
            )


@dataclass(frozen=True)
class ReferenceSignalExit:
    """One reference position closed by a modeled ``SignalExitRule``.

    Shaped exactly like :class:`ReferenceStopLossExit` — same fields, same
    validation, ``level_index`` absent by design for the same reason (a
    ``signal_exit`` close never carries a ladder rung, so a field that could
    only ever hold ``None`` carries no information). Every field matches
    ``ReferenceTrade``'s same-named field in type and semantics, so joining one
    of these with its :class:`~.reference_entries.ReferenceEntryFill` yields a
    ``ReferenceTrade`` with no renaming.

    ``exit_bar`` is the FILL bar, one past the bar whose predicate fired — not
    the trigger bar. That distinction is unique to this record type: for both
    resting kinds the trigger bar and the fill bar are the same bar. It is also
    what keeps the shared ``entry_bar < exit_bar`` invariant true even though a
    ``signal_exit``'s predicate, unlike a resting order, IS eligible on
    ``entry_bar`` itself: the earliest possible trigger is ``entry_bar``, so
    the earliest possible fill is ``entry_bar + 1``. A future change that ever
    filled a signal exit on its own trigger bar would break that invariant, and
    should — the invariant is load-bearing, not incidental.

    Invariants (all enforced in ``__post_init__``): ``0 <= entry_bar <
    exit_bar``; ``exit_price`` is finite and positive; ``exit_rule_index >= 0``;
    ``exit_rule_kind == "signal_exit"``.
    """

    symbol: str
    entry_bar: int
    exit_bar: int
    exit_date: str
    exit_price: float
    exit_rule_kind: Literal["signal_exit"]
    exit_rule_index: int

    def __post_init__(self) -> None:
        """Enforce this record's structural contract at construction.

        Fail-fast at construction rather than trusting the one producer below,
        matching ``ReferenceStopLossExit``/``ReferenceTakeProfitExit``: a record
        built directly by a test or a later matching module's adapter cannot
        exist in an invalid state either.

        Preconditions: none beyond typing.
        Postconditions: raises ``ValueError`` when ``entry_bar < 0``,
        ``exit_bar <= entry_bar``, ``exit_rule_index < 0``, ``exit_price`` is
        not a positive finite number, or ``exit_rule_kind`` is anything but
        ``"signal_exit"``; otherwise the instance is structurally valid.
        ``symbol`` and ``exit_date`` are recorded as given, not validated — the
        same stance the sibling records take.
        """
        if self.entry_bar < 0:
            raise ValueError(f"entry_bar must be >= 0, got {self.entry_bar!r}")
        if self.exit_bar <= self.entry_bar:
            raise ValueError(
                f"exit_bar must be > entry_bar ({self.entry_bar!r}), got {self.exit_bar!r}"
            )
        if self.exit_rule_index < 0:
            raise ValueError(f"exit_rule_index must be >= 0, got {self.exit_rule_index!r}")
        if not (self.exit_price > 0 and math.isfinite(self.exit_price)):
            raise ValueError(
                f"exit_price must be a positive finite number, got {self.exit_price!r}"
            )
        if self.exit_rule_kind != "signal_exit":
            raise ValueError(f"exit_rule_kind must be 'signal_exit', got {self.exit_rule_kind!r}")


def _decimals_for(reference_price: float) -> int:
    """Production's price-rounding bucket: 4 decimals below $10, else 2.

    Preconditions: ``reference_price`` is finite.
    Postconditions: returns ``4`` when ``reference_price < 10``, else ``2``.
    """
    return 4 if reference_price < 10 else 2


def _round_price(price: float) -> float:
    """Round a reference price the way production rounds its bid fields.

    Production stores ``entry_bid_price``/``exit_bid_price`` rounded to 4
    decimals below $10 and 2 decimals at or above it. A percentage-derived stop
    level routinely carries more places than either bucket allows, so skipping
    this would show every single trade as a spurious mismatch against
    production's own rounded field.

    Preconditions: ``price`` is finite (callers apply the finite-and-positive
    guard first).
    Postconditions: returns ``price`` rounded to the bucket ``price`` itself
    selects. Callers whose bucket is set by a DIFFERENT price than the one
    being rounded — the post-slippage anchor, whose bucket comes from the raw
    pre-slippage open — must not use this helper; see
    :func:`entry_price_basis`.
    """
    return round(price, _decimals_for(price))


def entry_price_basis(
    raw_open: float, side: Literal["long", "short"], entry_slippage_bps: float
) -> float:
    """The post-slippage entry anchor every modeled stop level hangs off.

    Production resolves ``basis="entry_price"`` levels and seeds trailing
    watermarks against ``Position.entry_price``, which is the POST-slippage
    fill — not the pre-slippage bid that ``ReferenceEntryFill.entry_price``
    reports. Anchoring on the pre-slippage value instead would shift every stop
    level (and possibly which bar crosses it) away from where the real engine
    rests its orders, for no reason but this module's choice of comparison
    field.

    The order of operations is load-bearing: multiply the RAW, unrounded open
    by the slippage multiplier and round ONCE, mirroring production's
    ``round(ref_price * slip, dp)``. Rounding first and scaling second can
    differ in the last decimal place, which is enough to move a level across a
    bar's extreme.

    Preconditions: ``raw_open`` is finite and ``> 0`` (the entry replay's own
    fill-bar guard has already established this); ``side`` is ``"long"`` or
    ``"short"``; ``entry_slippage_bps`` is finite and
    ``0 <= entry_slippage_bps < 10_000`` — at or above 10_000 the short-side
    multiplier reaches zero or goes negative, producing non-positive levels.
    ``raw_open`` must additionally be large enough that the ROUNDED anchor
    stays positive: ``raw_open > 0`` alone does not guarantee it, since a price
    below the bucket's own resolution rounds to zero (``round(0.00004, 4)`` is
    ``0.0``). Such a price is an input this function cannot model, not a value
    to coerce.
    Postconditions: returns ``round(raw_open * (1 + bps/10_000), dp)`` for a
    long and ``round(raw_open * (1 - bps/10_000), dp)`` for a short, with ``dp``
    taken from ``raw_open``'s own bucket. The result is strictly positive: an
    anchor that rounds to zero or below raises ``ValueError`` instead of being
    returned.
    """
    if not (raw_open > 0 and math.isfinite(raw_open)):
        raise ValueError(f"raw_open must be a positive finite number, got {raw_open!r}")
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if not (math.isfinite(entry_slippage_bps) and 0 <= entry_slippage_bps < 10_000):
        raise ValueError(
            f"entry_slippage_bps must be finite and in [0, 10_000), got {entry_slippage_bps!r}"
        )
    multiplier = (
        1.0 + entry_slippage_bps / 10_000.0
        if side == "long"
        else 1.0 - entry_slippage_bps / 10_000.0
    )
    # The bucket comes from the RAW open, not from the slipped product: a raw
    # price just under $10 that slippage pushes just over it still rounds to 4
    # places, exactly as production's own ``dp`` (derived from the reference
    # price it is about to slip) does. Deriving the bucket from the product
    # instead would round 9.99995 @ 2bps to 10.00 rather than 10.0019 — enough
    # to shift every level hanging off this anchor.
    anchor = round(raw_open * multiplier, _decimals_for(raw_open))
    # A sub-bucket price rounds to zero even though it passed every guard
    # above. Returning it would be silent and expensive: every stop level hangs
    # off this anchor, so a zero anchor drives each of them to zero, the
    # nonpositive-fill guard then suppresses every candidate fill, and the
    # position simply never closes — the reference ledger emits NO trade and
    # the later matching module reads that missing trade as a spec/engine
    # divergence. Fail here, where the cause is still visible.
    if not anchor > 0:
        raise ValueError(
            f"post-slippage anchor rounded to a non-positive value ({anchor!r}); "
            f"raw_open={raw_open!r} is below the resolution of its price bucket "
            f"({_decimals_for(raw_open)} decimals)"
        )
    return anchor


def working_exit_rules(spec: StrategySpec) -> List[ExitRule]:
    """``spec.exit_rules`` plus the engine's injected short safety stop.

    Before evaluating any exit, the live engine appends a synthetic
    ``StopLossRule(pct=1.0, basis="entry_price")`` to specs that permit short
    exposure but carry no effective short-side stop. That injected rule is real
    and indexable — a short that doubles against its entry closes through it,
    attributed like any other stop, at ``rule_index == len(spec.exit_rules)``.
    A reference ledger that skipped it would silently lack an exit rule the
    production ledger actually fires.

    "Permits short exposure" reduces here to ``any(rule.side == "short" ...)``
    over the entry rules: production's own condition also admits a spec whose
    entry rules are ``None``, but that is its custom-code signal, and
    custom-code specs are out of this simulator's scope entirely.

    Returned as a list so every later step derives rule INDICES from one place —
    the injected rule's index must be identical across the stop, take-profit,
    and combined replays or their ``exit_rule_index`` attributions disagree.

    Preconditions: ``spec`` is a validated ``StrategySpec`` with
    ``requires_custom_code`` False — enforced below rather than trusted, since
    a custom-code spec's real entries and quantities come from LLM-authored
    strategy code and not from ``spec.entry_rules``. Replaying its rules would
    produce a ledger with no relationship to what production traded: not an
    error anywhere, just a confidently wrong oracle. This is the boundary where
    that is still cheap to say.
    Postconditions: returns a NEW list — ``spec.exit_rules`` is never mutated —
    equal to ``spec.exit_rules`` when the spec has no short entry rule or
    already carries an effective short-side stop, else that list with one
    synthetic ``StopLossRule`` appended at index ``len(spec.exit_rules)``.
    Raises ``ValueError`` when the precondition above is violated.
    """
    if spec.requires_custom_code:
        raise ValueError(
            "reference-ledger replay is out of scope for a requires_custom_code "
            "spec: its entries come from strategy_code, not spec.entry_rules"
        )
    rules: List[ExitRule] = list(spec.exit_rules)
    shorts_possible = any(rule.side == "short" for rule in spec.entry_rules)
    if shorts_possible and first_side_stop_factor(rules, "short") is None:
        rules.append(StopLossRule(pct=_SHORT_SAFETY_STOP_PCT, basis=_SHORT_SAFETY_STOP_BASIS))
    return rules


def stop_loss_rules_for_side(
    rules: Sequence[ExitRule], side: Literal["long", "short"]
) -> List[Tuple[int, StopLossRule]]:
    """The ``StopLossRule``\\ s that can actually fire for ``side``, spec order.

    Filtering by basis/side compatibility here is not just an optimization:
    ``stop_loss_triggers`` already no-ops a ``trailing_low`` stop on a long (and
    vice versa), so an incompatible rule would never fire anyway — dropping it
    up front keeps the per-bar walk honest about which rules are genuinely
    competing for the position.

    Preconditions: ``rules`` are ``ExitRule`` members (non-stop members are
    valid input and are skipped); ``side`` is ``"long"`` or ``"short"``.
    Postconditions: returns ``(spec_index, rule)`` pairs in ascending spec
    index, containing exactly the ``StopLossRule``\\ s whose basis can trigger
    for ``side``. The indices are indices into ``rules`` as given, so they are
    the ``exit_rule_index`` values a fired rule records.
    """
    return [
        (idx, rule)
        for idx, rule in enumerate(rules)
        if isinstance(rule, StopLossRule) and stop_caps_side(rule.basis, side)
    ]


def take_profit_rules(rules: Sequence[ExitRule]) -> List[Tuple[int, TakeProfitRule]]:
    """The standalone ``TakeProfitRule`` members of ``rules``, in spec order.

    Unlike :func:`stop_loss_rules_for_side`, no side-compatibility filter
    applies: a ``TakeProfitRule``'s target formula is symmetric across both
    sides (``entry * (1 + pct)`` for a long, ``entry * (1 - pct)`` for a
    short) — there is no basis analog that can mismatch the position's side
    the way ``trailing_low``/``trailing_high`` can for a stop. Every
    ``TakeProfitRule`` in ``rules`` is therefore always a live candidate,
    regardless of which side the caller resolves against.

    Preconditions: ``rules`` are ``ExitRule`` members (non-take-profit members
    are valid input and are skipped).
    Postconditions: returns ``(spec_index, rule)`` pairs in ascending spec
    index, containing exactly the ``TakeProfitRule``\\ s in ``rules``. The
    indices are indices into ``rules`` as given, so they are the
    ``exit_rule_index`` values a fired rule records.
    """
    return [(idx, rule) for idx, rule in enumerate(rules) if isinstance(rule, TakeProfitRule)]


def scaled_take_profit_rules(
    rules: Sequence[ExitRule],
) -> List[Tuple[int, ScaledTakeProfitRule]]:
    """The ``ScaledTakeProfitRule`` ladders in ``rules``, in spec order.

    Same "no side filtering" stance as :func:`take_profit_rules` — see its
    docstring; a ladder rung's target formula is equally symmetric across
    sides.

    Preconditions: ``rules`` are ``ExitRule`` members (non-ladder members are
    valid input and are skipped).
    Postconditions: returns ``(spec_index, rule)`` pairs in ascending spec
    index, containing exactly the ``ScaledTakeProfitRule``\\ s in ``rules``.
    The indices are indices into ``rules`` as given, so they are the
    ``exit_rule_index`` values a fired rung's closing record attributes to.
    """
    return [(idx, rule) for idx, rule in enumerate(rules) if isinstance(rule, ScaledTakeProfitRule)]


class _RestingStopLoss:
    """One modeled resting stop order for one open position.

    Owns the two pieces of per-position state the shared, stateless
    ``rule_compiler`` decision functions cannot carry across bars: the trailing
    watermark and the PER-RULE stop-limit arm/latch state (``_armed`` latches
    each limit-style rule's index independently, since ``_rules`` may hold
    several limit-style stops competing on one position). Kept as an object
    rather than
    loop locals because the later combined simulator must interleave this with
    take-profit and signal-exit candidates bar by bar, asking each "would you
    fill on this bar?" — which is exactly :meth:`step`.

    Invariants:
      * ``_high_water >= entry_price_basis`` and ``_low_water <=
        entry_price_basis`` — both seeded there and only ever ratcheted
        favorably.
      * ``_armed`` is monotonic: once a limit-style stop's level is breached it
        never disarms, so a gap-through that left it unfilled still fills on a
        later bar whose range reaches the limit, without re-crossing the stop.
    """

    def __init__(
        self,
        *,
        side: Literal["long", "short"],
        symbol: str,
        anchor: float,
        rules: Sequence[Tuple[int, StopLossRule]],
    ) -> None:
        """Arm a resting stop model for a freshly filled position.

        Preconditions: ``anchor`` is the position's post-slippage
        :func:`entry_price_basis` (finite, ``> 0``); ``rules`` are the
        side-compatible ``(spec_index, rule)`` pairs from
        :func:`stop_loss_rules_for_side`, in ascending index.
        Postconditions: watermarks are seeded at ``anchor`` and no rule is
        armed — the object is positioned to evaluate its first bar.
        """
        self._side = side
        self._symbol = symbol
        self._anchor = anchor
        self._rules = list(rules)
        # Seeded at the post-slippage anchor, NOT at the entry bar's own
        # high/low: under the target resting-order model the order materializes
        # at entry fill with its watermark seeded from that fill price, and is
        # not eligible until the following bar, so the entry bar's range never
        # enters the watermark.
        self._high_water = anchor
        self._low_water = anchor
        self._armed: Set[int] = set()

    def _position(self) -> PositionState:
        """Snapshot the position as the shared evaluator expects to see it.

        ``qty`` is a nominal ``1.0``: this step models no sizing, and the
        rule-decision functions only require it be positive.

        Preconditions: none. Postconditions: returns a ``PositionState``
        carrying the watermarks AS OF THE PRIOR BAR (see :meth:`step`).
        """
        return PositionState(
            symbol=self._symbol,
            side=self._side,
            qty=1.0,
            entry_price=self._anchor,
            high_since_entry=self._high_water,
            low_since_entry=self._low_water,
        )

    def _extend_watermarks(self, bar: "Bar") -> None:
        """Fold ``bar``'s extremes into the watermark, AFTER its trigger check.

        Preconditions: ``bar``'s check has already run (:meth:`step` enforces
        the ordering).
        Postconditions: ``_high_water``/``_low_water`` include ``bar``'s
        high/low; both move only favorably.
        """
        if bar.high > self._high_water:
            self._high_water = bar.high
        if bar.low < self._low_water:
            self._low_water = bar.low

    def _market_fill_price(self, level: float, bar: "Bar") -> float:
        """Worse-of-open-and-level, the shipped resting-STOP fill geometry.

        A bar that trades through the level without gapping past it fills AT the
        level; a bar that opens already beyond it fills at that (worse) open.
        Mirrors the execution model's own ``min``/``max`` of open and stop.

        Preconditions: the rule has triggered against ``bar``; ``level`` is the
        resolved stop level.
        Postconditions: returns ``min(bar.open, level)`` when closing a long (a
        sell) and ``max(bar.open, level)`` when closing a short (a buy).
        """
        return min(bar.open, level) if self._side == "long" else max(bar.open, level)

    def _limit_reachable(self, limit_price: float, bar: "Bar") -> bool:
        """Whether ``bar``'s RANGE reaches an armed stop-limit's limit price.

        Reachability is judged on the full range, not the open: a bar that opens
        beyond the limit but trades back to it still fills. Only a bar whose
        entire range stays past the limit leaves the order resting — the
        gap-through non-fill that is a stop-limit's defining trade-off.

        Preconditions: ``limit_price`` is the resolved protective-side limit.
        Postconditions: ``True`` iff ``bar.high >= limit_price`` closing a long
        (a sell) or ``bar.low <= limit_price`` closing a short (a buy).
        """
        if self._side == "long":
            return bar.high >= limit_price
        return bar.low <= limit_price

    def _candidate_price(self, idx: int, rule: StopLossRule, bar: "Bar") -> Optional[float]:
        """The price ``rule`` would fill at on ``bar``, or ``None`` if it does not.

        Preconditions: ``rule`` is side-compatible with the position; ``idx`` is
        its index in the working rule list.
        Postconditions: returns the unrounded reference fill price, or ``None``
        when the rule does not fire on this bar. May latch ``idx`` into
        ``_armed`` as a
        side effect for a limit-style rule whose stop level is breached — that
        latch is deliberate engine state that must survive a non-filling bar.
        """
        position = self._position()
        if rule.style == "limit":
            limit_price = stop_limit_prices(rule, position).limit_price
            if idx not in self._armed:
                if not stop_loss_triggers(rule, position, _snapshot(bar)):
                    return None
                self._armed.add(idx)
            # Armed (this bar or an earlier one): only the limit stage decides
            # from here, so the stop level is never re-tested.
            if not self._limit_reachable(limit_price, bar):
                return None
            # A stop-limit fills AT its limit, never gap-adjusted worse.
            return limit_price
        if not stop_loss_triggers(rule, position, _snapshot(bar)):
            return None
        return self._market_fill_price(stop_loss_level(rule, position), bar)

    def step(self, bar: "Bar") -> Optional[Tuple[int, float]]:
        """Evaluate ``bar``, then ratchet the watermark.

        The ordering is the whole point and is not interchangeable: the trigger
        check runs against the watermark AS OF THE PRIOR BAR, and only
        afterwards does this bar's high/low extend it. Folding first would let a
        bar's favorable extreme raise the floor and that same bar's opposite
        extreme trigger against the raised floor — reading an ordinary wide bar
        as a stop-out. (The engine's own resting-order ratchet folds the current
        bar in first; the bar-close evaluator does not. This module follows the
        latter, per the design doc, which is also what keeps the shared
        ``stop_loss_triggers`` geometry usable unmodified. Do not "fix" this
        toward the fill simulator without re-reading that section.)

        Preconditions: ``bar`` is strictly later than the position's entry bar —
        a resting order is not eligible on its own materialization bar. Callers
        enforce this by starting the walk at ``entry_bar + 1``.
        Postconditions: returns ``(exit_rule_index, unrounded_fill_price)`` for
        the winning rule, or ``None`` when no rule fills on this bar. Ties among
        rules reachable on the same bar break by ascending spec index, matching
        ``first_exit_intent_for_position``'s spec-order walk. The watermark is
        extended with ``bar`` either way, so a caller that stops on a fill and
        one that continues see the same state evolution.
        """
        winner: Optional[Tuple[int, float]] = None
        for idx, rule in self._rules:
            price = self._candidate_price(idx, rule, bar)
            if price is None:
                continue
            if not (price > 0 and math.isfinite(price)):
                # A degenerate bar suppresses this one candidate fill rather
                # than aborting the run or emitting an invalid record; a
                # lower-priority rule may still fill on this bar, and this rule
                # may fill on a later one.
                continue
            winner = (idx, price)
            break
        self._extend_watermarks(bar)
        return winner


def _snapshot(bar: "Bar") -> BarSnapshot:
    """Adapt a ``Bar`` to the evaluator's minimal ``BarSnapshot``.

    Preconditions: ``bar`` exposes ``high``/``low``/``close``.
    Postconditions: returns an equivalent ``BarSnapshot``.
    """
    return BarSnapshot(high=bar.high, low=bar.low, close=bar.close)


def resolve_stop_loss_exit(
    rules: Sequence[ExitRule],
    entry: ReferenceEntryFill,
    symbol_bars: "Sequence[Bar]",
    *,
    entry_slippage_bps: float = 0.0,
) -> Optional[ReferenceStopLossExit]:
    """Model the ``StopLossRule`` close of one already-opened reference position.

    The per-position core of :func:`replay_stop_loss_exits`, exposed separately
    so a later step can drive it against a position whose entry came from
    somewhere other than a whole-spec replay.

    Preconditions:
        - ``rules`` is the WORKING exit-rule list from
          :func:`working_exit_rules` (not raw ``spec.exit_rules``), so an
          injected short safety stop is present and every index is the one a
          fired rule should record.
        - ``entry`` was produced against ``symbol_bars``: ``0 <= entry.entry_bar
          < len(symbol_bars)``.
        - ``entry_slippage_bps`` is finite and in ``[0, 10_000)``.

    Postconditions:
        - Returns the first modeled stop fill at or after ``entry.entry_bar +
          1`` — a resting order is not eligible on its materialization bar — or
          ``None`` when no stop fills before ``symbol_bars`` runs out. A
          position still open at the last bar produces no record at all,
          mirroring production reporting it as an open position rather than a
          synthetic force-close.
        - The returned record's ``exit_price`` is rounded to production's own
          bid-price buckets and its ``exit_rule_index`` indexes ``rules``.

    Invariants: does not mutate ``rules``, ``entry``, or ``symbol_bars``, and is
    deterministic in its inputs.
    """
    if not 0 <= entry.entry_bar < len(symbol_bars):
        raise ValueError(
            f"entry.entry_bar {entry.entry_bar!r} is out of range for {len(symbol_bars)} bars"
        )
    candidates = stop_loss_rules_for_side(rules, entry.side)
    if not candidates:
        return None
    anchor = entry_price_basis(symbol_bars[entry.entry_bar].open, entry.side, entry_slippage_bps)
    order = _RestingStopLoss(side=entry.side, symbol=entry.symbol, anchor=anchor, rules=candidates)
    for exit_bar in range(entry.entry_bar + 1, len(symbol_bars)):
        bar = symbol_bars[exit_bar]
        fired = order.step(bar)
        if fired is None:
            continue
        rule_index, raw_price = fired
        return ReferenceStopLossExit(
            symbol=entry.symbol,
            entry_bar=entry.entry_bar,
            exit_bar=exit_bar,
            # ``Bar.timestamp`` is ISO-8601, so its first 10 characters are the
            # date — production truncates ``bar.timestamp[:10]`` identically.
            exit_date=bar.timestamp[:10],
            exit_price=_round_price(raw_price),
            exit_rule_kind="stop_loss",
            exit_rule_index=rule_index,
        )
    return None


def replay_stop_loss_exits(
    spec: StrategySpec,
    bars: "Mapping[str, Sequence[Bar]]",
    *,
    entry_slippage_bps: float = 0.0,
) -> List[ReferenceStopLossExit]:
    """Replay ``spec``'s ``StopLossRule`` exits over ``bars``.

    Opens reference positions with the shared entry-side replay, then models
    each one's stop-loss close with resting-order fill semantics.

    Preconditions:
        - ``spec`` is a validated ``StrategySpec`` with ``requires_custom_code``
          False.
        - ``bars`` maps symbol to a chronological ``Bar`` sequence (an empty
          sequence is skipped, not an error — this is a narrower slice of the
          design doc's eventual ``simulate()``, and does not enforce that
          function's full precondition set).
        - ``entry_slippage_bps`` is finite and in ``[0, 10_000)``, mirroring the
          backtest config's own slippage input. It shifts the post-slippage
          anchor every stop level hangs off, so it can change both the recorded
          exit price and which bar the stop fires on.

    Postconditions:
        - Returns at most one ``ReferenceStopLossExit`` per symbol, in the order
          the entry replay yields positions. At most one because the entry
          replay opens at most one position per symbol; fewer whenever a
          position is still open when its bars run out, or the spec has no
          stop rule able to fire for that position's side.
        - Every returned record's ``exit_rule_index`` indexes
          :func:`working_exit_rules`'s list, so an injected short safety stop
          reports ``len(spec.exit_rules)``.

    Invariants:
        - No side effects: does not mutate ``spec`` or ``bars``, and performs
          no I/O.
        - Deterministic: identical inputs always produce an identical list.
        - Imports no module reaching ``trading_service/service.py`` or the four
          forbidden ``trading_service/engine/`` modules (see this module's
          docstring).
    """
    rules = working_exit_rules(spec)
    out: List[ReferenceStopLossExit] = []
    for entry in replay_entry_rules(spec, bars):
        found = resolve_stop_loss_exit(
            rules,
            entry,
            bars[entry.symbol],
            entry_slippage_bps=entry_slippage_bps,
        )
        if found is not None:
            out.append(found)
    return out


# ---------------------------------------------------------------------------
# Take-profit family: standalone ``TakeProfitRule`` and laddered
# ``ScaledTakeProfitRule``
# ---------------------------------------------------------------------------


def _take_profit_target(anchor: float, pct: float, side: Literal["long", "short"]) -> float:
    """The exact resting-limit target for one standalone rule or ladder rung.

    The shared evaluator's ``ExitIntent`` (below) confirms THAT a rule/rung
    triggered but does not resolve WHAT price it fills at — that fill-price
    mechanic is this module's own to add, per the design doc's Reuse/division
    of labor. This is that one remaining piece: a two-line, self-contained
    formula with no drift risk against the shared evaluator, since it uses the
    exact same ``entry_price`` field the evaluator's own trigger check reads
    (see :class:`_RestingTakeProfitFamily`'s docstring).

    Preconditions: ``anchor`` is the position's post-slippage
    :func:`entry_price_basis`; ``pct`` is the rule's/rung's positive profit
    magnitude; ``side`` is ``"long"`` or ``"short"``.
    Postconditions: returns ``anchor * (1 + pct)`` for a long and
    ``anchor * (1 - pct)`` for a short — the exact price the resting limit
    fills at, never gap-adjusted (see the design doc's ``take_profit``
    subsection for why this is asymmetric with stop-loss's worse-of-open
    rule).
    """
    return anchor * (1.0 + pct) if side == "long" else anchor * (1.0 - pct)


@dataclass
class _LadderCursor:
    """Per-ladder cursor state for one ``ScaledTakeProfitRule`` on one position.

    Mutable, unlike the frozen records elsewhere in this module: this is pure
    internal bookkeeping advanced bar-by-bar by
    :class:`_RestingTakeProfitFamily` and never exposed to a caller. Mirrors
    the live engine's ``_ScaledLadderCursor`` (keyed the same way — per
    ``rule_index`` — but scoped to one position, since this module walks one
    position at a time rather than a whole per-strategy dispatcher).

    Carries no explicit "next eligible bar" field for the engine's
    ``scaled_partial_in_flight`` materialization-bar deferral — a newly-armed
    rung is not eligible on the same bar an earlier rung just fired, but
    :meth:`_RestingTakeProfitFamily.step` already guarantees that structurally
    without extra state: it builds one bar's candidate list from the cursor
    BEFORE advancing it, and its only caller (:func:`resolve_take_profit_family_exit`)
    walks bars strictly sequentially, one ``step`` call per bar. So the
    newly-advanced rung is never even examined until the NEXT ``step`` call —
    which is, by construction, a later bar. Tracking a redundant eligibility
    bar would duplicate an invariant the call structure already enforces.

    Invariants: ``next_rung`` only ever increases.
    """

    rule_index: int
    rule: ScaledTakeProfitRule
    next_rung: int


class _TakeProfitFireResult(NamedTuple):
    """The winning candidate's outcome once its fill fully closes the position.

    ``raw_price`` is the qty-weighted average across every partial fill plus
    this closing fill — UNROUNDED. ``terminal_price`` is the FINAL closing
    slice's own unrounded price (identical to ``raw_price`` for a single-slice
    close, since there is nothing to blend). The two are deliberately kept
    separate: production's ``FillSimulator._fill_exit`` derives the rounding
    bucket (4dp below $10, else 2dp) from the terminal slice's own
    ``reference_price``, THEN rounds the blended ``weighted_avg_exit_price``
    with that bucket (``fill_simulator.py``'s terminal-close branch:
    ``dp = 4 if ref_price < 10 else 2`` from the terminal slice, then
    ``round(pos.weighted_avg_exit_bid_price, dp)``) — never re-deriving the
    bucket from the blended value itself. A ladder whose rung prices straddle
    the $10 bucket boundary would otherwise round at the wrong precision (the
    same "bucket comes from a DIFFERENT price than the one being rounded"
    pattern :func:`entry_price_basis` already documents for its own anchor).
    The caller applies the rounding in that same two-step order — bucket from
    ``terminal_price``, round ``raw_price`` — exactly once, matching the
    one-rounding-pass discipline :func:`entry_price_basis`/
    :func:`resolve_stop_loss_exit` already use.
    """

    exit_rule_index: int
    exit_rule_kind: Literal["take_profit", "scaled_take_profit"]
    raw_price: float
    terminal_price: float
    level_index: Optional[int]


class _RestingTakeProfitFamily:
    """One modeled take-profit-family resting-order book for one open position.

    Owns every standalone ``TakeProfitRule`` and every ``ScaledTakeProfitRule``
    ladder attached to one position, racing them together bar by bar via the
    shared, pure ``rule_compiler.evaluate_exit_rules_for_position`` — the same
    per-position rule-priority resolution the live evaluator and this design's
    own Reuse mandate specify, rather than a second, locally re-implemented
    tie-break. ``first_only=False`` asks it for every triggered intent in spec
    order (not just the winner), because a ``StopLossRule``/``SignalExitRule``
    intent could otherwise "win" the walk and mask a lower-priority take-profit
    candidate this module must still see — this class filters the returned
    list down to ``take_profit``/``scaled_take_profit`` kinds and takes the
    first (i.e. lowest ``exit_rule_index``) survivor, which is exactly the
    SAME per-position (not per-rule, not per-ladder) firing budget as before:
    a bar reachable by two different ladders' cursor rungs, or by a standalone
    target and a rung, still only fires the lower-``exit_rule_index`` one.

    Unlike :class:`_RestingStopLoss`, this object carries NO cross-bar price
    watermark — it passes a ``PositionState`` whose ``high_since_entry``/
    ``low_since_entry`` are HELD FIXED at the post-slippage anchor on every
    call, never extended bar-over-bar. This is not an approximation: a
    take-profit-family target is a FIXED price, so the shared evaluator's
    watermark-based ladder-rung test — ``max(high_since_entry, bar.high) >=
    entry * (1 + pct)`` for a long — is PROVABLY EQUIVALENT to a current-bar
    -only test when ``high_since_entry`` never moves off the anchor: since the
    anchor is always strictly below every rung's target (a positive ``pct``
    guarantees it), ``max(anchor, bar.high) >= target`` reduces exactly to
    ``bar.high >= target``. So freezing the watermark at construction is what
    makes reuse here behavior-preserving rather than reintroducing the
    fabricated-fill risk the design doc warns against (a rung that stays
    "eligible" on a later bar purely because an EARLIER bar's spike, not this
    bar's own range, once reached it) — see this module's own top docstring
    for the same argument at the module level. The only cross-bar state this
    object owns is per-ladder cursor bookkeeping (``_ladders``) plus the
    running fill/quantity ledger — the shared evaluator is stateless and is
    handed fresh cursor positions on every call.

    A fired ladder rung that does not exhaust the position's remaining
    quantity does NOT emit a closing outcome — it advances internal state
    (``_remaining_qty``, the firing ladder's cursor, ``_fills``) and the walk
    continues, mirroring the design doc's "a fired rung does not emit its own
    ``ReferenceTrade``" rule. Only the position's FINAL closing event — a
    standalone target (always closes whatever remains) or a rung that leaves
    ``_remaining_qty`` within :data:`_FILL_QTY_REL_TOL` of zero — produces an
    outcome, carrying the qty-weighted average price across every partial fill
    plus that final one.

    Invariants:
      * ``_remaining_qty`` only ever decreases.
      * Each ladder's ``next_rung`` only ever increases.
      * Once :meth:`step` has returned a non-``None`` result, every later call
        returns ``None`` unconditionally — the position is closed, and this
        object records no further fills. This is enforced by an explicit guard
        at the top of :meth:`step`, not left to the caller's own bar-walk
        discipline: this class is also usable directly by a future driver (the
        combined multi-kind simulator this module's docstring anticipates)
        that may not share :func:`resolve_take_profit_family_exit`'s
        stop-on-first-result contract.
      * The FIRST rung of every ladder, and every standalone rule, is only
        ever checked starting at ``entry_bar + 1``: this object's only current
        caller (:func:`resolve_take_profit_family_exit`) starts its bar walk
        there, so ``step`` is simply never invoked for ``entry_bar`` itself —
        the materialization-bar deferral for a position's FIRST candidate
        needs no state on this object either, for the same structural reason
        :class:`_LadderCursor` needs none for a ladder's LATER rungs.
    """

    def __init__(
        self,
        *,
        side: Literal["long", "short"],
        symbol: str,
        anchor: float,
        rules: Sequence[ExitRule],
    ) -> None:
        """Arm a take-profit-family resting-order book for a freshly filled position.

        Preconditions: ``anchor`` is the position's post-slippage
        :func:`entry_price_basis` (finite, ``> 0``); ``rules`` is the WORKING
        exit-rule list from :func:`working_exit_rules` — every member's
        position in ``rules`` is used as-is as its ``exit_rule_index``, so
        this must be the same list (not a filtered subset) the caller derived
        :func:`take_profit_rules`/:func:`scaled_take_profit_rules` from; it may
        freely contain non-take-profit-family members (``StopLossRule``,
        ``SignalExitRule``), which this object evaluates harmlessly but never
        selects as a winner (see class docstring). ``rules`` together must
        contain at least one ``TakeProfitRule``/``ScaledTakeProfitRule`` (the
        caller checks this before constructing). The caller must not invoke
        :meth:`step` for the position's own entry bar — only for
        ``entry_bar + 1`` onward — which is this object's only source of
        materialization-bar deferral for a position's first candidates; see
        this class's own Invariants for why no additional state is needed
        here to enforce that.
        Postconditions: every ladder's cursor starts at rung 0.
        ``_remaining_qty`` starts at the nominal ``_original_qty`` (``1.0`` —
        this step models no real sizing, the same nominal-``qty=1.0`` stance
        ``_RestingStopLoss`` takes).
        """
        self._side = side
        self._symbol = symbol
        self._anchor = anchor
        self._rules = list(rules)
        self._ladders = [
            _LadderCursor(rule_index=idx, rule=rule, next_rung=0)
            for idx, rule in scaled_take_profit_rules(self._rules)
        ]
        # Keyed lookup for step()'s winner -> cursor resolution, built once
        # rather than a linear scan per bar. Its keys are exactly the
        # scaled_take_profit_rules(self._rules) indices, the same set
        # evaluate_exit_rules_for_position draws its "scaled_take_profit"
        # rule_kind classification from — see step()'s own comment for the
        # invariant this relies on.
        self._ladder_by_index = {ladder.rule_index: ladder for ladder in self._ladders}
        self._original_qty = 1.0
        self._remaining_qty = 1.0
        self._fills: List[Tuple[float, float]] = []

    def _position(self) -> PositionState:
        """Snapshot the position with the watermark FROZEN at the anchor.

        Preconditions: none. Postconditions: returns a ``PositionState`` whose
        ``high_since_entry``/``low_since_entry`` both equal ``self._anchor`` —
        never the running extremes — which is the frozen-watermark construction
        this class's own docstring proves makes the shared evaluator's
        watermark-based ladder test equivalent to a current-bar-only test.
        ``qty`` is ``self._remaining_qty`` (only ever called while ``> 0``, per
        :meth:`step`'s own already-closed guard).
        """
        return PositionState(
            symbol=self._symbol,
            side=self._side,
            qty=self._remaining_qty,
            entry_price=self._anchor,
            high_since_entry=self._anchor,
            low_since_entry=self._anchor,
        )

    def step(self, bar: "Bar") -> Optional[_TakeProfitFireResult]:
        """Evaluate ``bar``, applying at most one winning candidate.

        Preconditions: the caller invokes this once per bar, in strictly
        increasing bar order, starting no earlier than the position's
        ``entry_bar + 1`` (see this class's own Invariants for why that is
        this object's only source of materialization-bar deferral).
        Postconditions: returns the position's FINAL closing outcome once this
        bar's winning candidate exhausts ``_remaining_qty`` (within
        :data:`_FILL_QTY_REL_TOL`); returns ``None`` otherwise, whether because
        the position was already fully closed on an earlier call, no candidate
        was reachable this bar, or the winning rung fired but left the
        position open. Internal state advances whenever a candidate fires,
        whether or not this call returns a result.
        """
        if self._fills and self._remaining_qty <= self._original_qty * _FILL_QTY_REL_TOL:
            return None  # already fully closed on an earlier bar; see Invariants

        cursor_map = {ladder.rule_index: ladder.next_rung for ladder in self._ladders}
        intents = evaluate_exit_rules_for_position(
            self._rules,
            self._symbol,
            self._position(),
            _snapshot(bar),
            first_only=False,
            cursor_map=cursor_map,
        )
        winner = next(
            (
                intent
                for intent in intents
                if intent.rule_kind in ("take_profit", "scaled_take_profit")
            ),
            None,
        )
        if winner is None:
            return None

        rule = self._rules[winner.rule_index]
        ladder: Optional[_LadderCursor] = None
        if winner.rule_kind == "take_profit":
            # A standalone rule is always a full close of whatever remains —
            # the design doc's "every other exit rule kind is always a
            # full-position close."
            pct = rule.pct
            qty = self._remaining_qty
        else:
            level = rule.levels[winner.level_index]
            pct = level.pct
            qty = min(winner.qty_fraction * self._original_qty, self._remaining_qty)
            # Invariant this relies on: every intent evaluate_exit_rules_for_position
            # classifies as "scaled_take_profit" has a matching cursor in
            # self._ladder_by_index, since both derive from the SAME
            # scaled_take_profit_rules(self._rules) filtering. Guarded
            # explicitly (rather than left as a bare lookup raising an opaque
            # KeyError) so a future change that breaks this invariant fails
            # loudly, matching this module's Design-by-Contract style.
            ladder = self._ladder_by_index.get(winner.rule_index)
            if ladder is None:  # pragma: no cover - invariant, not a reachable state
                raise AssertionError(
                    f"scaled_take_profit intent for rule_index {winner.rule_index!r} "
                    "has no matching ladder cursor"
                )

        price = _take_profit_target(self._anchor, pct, self._side)
        self._fills.append((qty, price))
        self._remaining_qty -= qty
        if ladder is not None:
            ladder.next_rung += 1

        if self._remaining_qty > self._original_qty * _FILL_QTY_REL_TOL:
            return None  # rung fired, position still open — keep walking

        total_qty = sum(q for q, _ in self._fills)
        weighted_price = sum(q * p for q, p in self._fills) / total_qty
        return _TakeProfitFireResult(
            exit_rule_index=winner.rule_index,
            exit_rule_kind=winner.rule_kind,
            raw_price=weighted_price,
            terminal_price=price,
            level_index=winner.level_index,
        )


def resolve_take_profit_family_exit(
    rules: Sequence[ExitRule],
    entry: ReferenceEntryFill,
    symbol_bars: "Sequence[Bar]",
    *,
    entry_slippage_bps: float = 0.0,
) -> Optional[ReferenceTakeProfitExit]:
    """Model the take-profit-family close of one already-opened reference position.

    The per-position core of :func:`replay_take_profit_family_exits`, exposed
    separately so a later step can drive it against a position whose entry
    came from somewhere other than a whole-spec replay — mirrors
    :func:`resolve_stop_loss_exit`'s own role. A spec's standalone
    ``TakeProfitRule``\\ s and ``ScaledTakeProfitRule`` ladders are resolved
    together by a single :class:`_RestingTakeProfitFamily`, not by two
    independent walks: they must race on the same bar per the design doc's
    tie-break rule, and two independent resolvers could each decide "I fire
    this bar" without knowing about the other, double-closing the position.

    Preconditions:
        - ``rules`` is the WORKING exit-rule list from
          :func:`working_exit_rules` (not raw ``spec.exit_rules``), so every
          index is the one a fired rule/rung should record.
        - ``entry`` was produced against ``symbol_bars``: ``0 <= entry.entry_bar
          < len(symbol_bars)``.
        - ``entry_slippage_bps`` is finite and in ``[0, 10_000)``.

    Postconditions:
        - Returns the position's FINAL closing record — the first bar at or
          after ``entry.entry_bar + 1`` whose winning candidate's fill fully
          closes the position (within :data:`_FILL_QTY_REL_TOL`) — or ``None``
          in either of two cases: the position is still open (never touched,
          or only partially reduced by earlier rungs) when ``symbol_bars``
          runs out (mirrors production reporting a still-open position rather
          than a synthetic force-close), or ``rules`` contains no
          ``TakeProfitRule``/``ScaledTakeProfitRule`` at all, returned
          immediately before any bar walk.
        - The returned record's ``exit_price`` is the qty-weighted average of
          every partial fill plus the final closing fill, rounded once to
          production's own bid-price bucket; its ``exit_rule_index`` indexes
          ``rules``; ``level_index`` is populated iff the final closing event
          was a ladder rung.

    Invariants: does not mutate ``rules``, ``entry``, or ``symbol_bars``, and
    is deterministic in its inputs.
    """
    if not 0 <= entry.entry_bar < len(symbol_bars):
        raise ValueError(
            f"entry.entry_bar {entry.entry_bar!r} is out of range for {len(symbol_bars)} bars"
        )
    if not take_profit_rules(rules) and not scaled_take_profit_rules(rules):
        return None
    anchor = entry_price_basis(symbol_bars[entry.entry_bar].open, entry.side, entry_slippage_bps)
    family = _RestingTakeProfitFamily(
        side=entry.side,
        symbol=entry.symbol,
        anchor=anchor,
        rules=rules,
    )
    for exit_bar in range(entry.entry_bar + 1, len(symbol_bars)):
        bar = symbol_bars[exit_bar]
        fired = family.step(bar)
        if fired is None:
            continue
        return ReferenceTakeProfitExit(
            symbol=entry.symbol,
            entry_bar=entry.entry_bar,
            exit_bar=exit_bar,
            exit_date=bar.timestamp[:10],
            # Bucket from the TERMINAL slice's own price, round the BLENDED
            # average with it — production's order of operations
            # (fill_simulator.py's terminal-close branch derives dp from the
            # terminal slice's reference_price, then rounds the weighted
            # average with that dp), never re-derived from the blended value
            # itself. See _TakeProfitFireResult's docstring for the full
            # argument and production line references.
            exit_price=round(fired.raw_price, _decimals_for(fired.terminal_price)),
            exit_rule_kind=fired.exit_rule_kind,
            exit_rule_index=fired.exit_rule_index,
            level_index=fired.level_index,
        )
    return None


def replay_take_profit_family_exits(
    spec: StrategySpec,
    bars: "Mapping[str, Sequence[Bar]]",
    *,
    entry_slippage_bps: float = 0.0,
) -> List[ReferenceTakeProfitExit]:
    """Replay ``spec``'s take-profit-family exits over ``bars``.

    Opens reference positions with the shared entry-side replay, then models
    each one's take-profit/scaled-take-profit close with resting-order fill
    semantics. Mirrors :func:`replay_stop_loss_exits`'s shape exactly,
    substituting the take-profit-family resolver.

    Preconditions:
        - ``spec`` is a validated ``StrategySpec`` with ``requires_custom_code``
          False.
        - ``bars`` maps symbol to a chronological ``Bar`` sequence (an empty
          sequence is skipped, not an error, mirroring
          :func:`replay_stop_loss_exits`'s own stance).
        - ``entry_slippage_bps`` is finite and in ``[0, 10_000)`` — it shifts
          the post-slippage anchor every target/rung hangs off, so it can
          change both the recorded exit price and which bar the position
          closes on.

    Postconditions:
        - Returns at most one ``ReferenceTakeProfitExit`` per symbol, in the
          order the entry replay yields positions. Fewer whenever a position
          is still open when its bars run out, or the spec has no
          ``TakeProfitRule``/``ScaledTakeProfitRule`` at all.
        - Every returned record's ``exit_rule_index`` indexes
          :func:`working_exit_rules`'s list.

    Invariants:
        - No side effects: does not mutate ``spec`` or ``bars``, and performs
          no I/O.
        - Deterministic: identical inputs always produce an identical list.
        - Imports no module reaching ``trading_service/service.py`` or the four
          forbidden ``trading_service/engine/`` modules (see this module's
          docstring).
    """
    rules = working_exit_rules(spec)
    out: List[ReferenceTakeProfitExit] = []
    for entry in replay_entry_rules(spec, bars):
        found = resolve_take_profit_family_exit(
            rules,
            entry,
            bars[entry.symbol],
            entry_slippage_bps=entry_slippage_bps,
        )
        if found is not None:
            out.append(found)
    return out


# ---------------------------------------------------------------------------
# Signal exits: ``SignalExitRule`` — the one NON-resting kind
# ---------------------------------------------------------------------------


def signal_exit_rules(rules: Sequence[ExitRule]) -> List[Tuple[int, SignalExitRule]]:
    """The ``SignalExitRule`` members of ``rules``, in spec order.

    Same "no side filtering" stance as :func:`take_profit_rules` — and for a
    stronger reason: a signal exit's trigger is a predicate over bar history
    and indicator values, which carries no notion of the position's side at
    all (contrast ``StopLossRule.basis``, where ``trailing_low`` is
    structurally a short-side concept). Every ``SignalExitRule`` in ``rules``
    is a live candidate for a position of either side.

    Preconditions: ``rules`` are ``ExitRule`` members (non-signal members are
    valid input and are skipped).
    Postconditions: returns ``(spec_index, rule)`` pairs in ascending spec
    index, containing exactly the ``SignalExitRule``\\ s in ``rules``. The
    indices are indices into ``rules`` as given, so they are the
    ``exit_rule_index`` values a fired rule records.
    """
    return [(idx, rule) for idx, rule in enumerate(rules) if isinstance(rule, SignalExitRule)]


class _PrefixHistoryView:
    """A ``HistoryView`` over ``view``'s first ``i + 1`` bars.

    Exists to bridge one convention mismatch, and it is the sharpest trap in
    this module. The shared evaluator resolves a ``SignalExitRule``'s predicate
    at ``view.length() - 1`` (``rule_compiler._rule_triggers``), never at a
    caller-supplied index: in the live engine the view is a STREAMING one whose
    last bar is by construction the bar being processed, so "the last bar" and
    "now" are the same thing. This module's view is a
    :class:`~.predicate_evaluator.PandasHistoryView` over a symbol's WHOLE bar
    history, where they are emphatically not. Passing that full view straight
    to the evaluator would silently evaluate every position's signal predicate
    against the FINAL bar of the dataset on every step — textbook look-ahead,
    and one that raises no error and produces plausible-looking output (every
    spec would appear to exit on ``entry_bar + 1``, or never). Truncating
    ``length()`` is what makes "now" mean bar ``i`` again.

    Delegating ``bar_field``/``indicator`` to the wrapped view rather than
    rebuilding a truncated ``PandasHistoryView`` per bar is deliberate on two
    counts. It is O(1) per bar instead of recomputing every indicator series
    over a growing prefix (O(n^2) across the walk). More importantly it reuses
    the SAME cached series the entry side already evaluates its predicates
    against, so an entry predicate and a signal-exit predicate reading the same
    indicator on the same bar can never disagree within this module.

    Safety of that delegation is a property of the evaluator, not an
    assumption: ``evaluate_predicate`` reads exactly two indices — ``i`` and,
    for ``cross_above``/``cross_below`` only, ``i - 1`` — and ``evaluate_tree``
    just recurses at the same ``i``. Nothing reads forward of ``i``, so
    exposing the wrapped view's later rows through ``bar_field``/``indicator``
    is unreachable rather than merely unlikely. (The indicator SERIES are
    computed over the full frame, which is the entry side's own long-standing
    stance for causal indicators; it is unchanged here, not newly introduced.)

    Invariants: ``length()`` is constant at ``i + 1``; the wrapped view is
    never mutated.
    """

    __slots__ = ("_view", "_length")

    def __init__(self, view: HistoryView, i: int) -> None:
        """Wrap ``view``, exposing its first ``i + 1`` bars.

        Preconditions: ``0 <= i < view.length()`` — a prefix past the end of
        the wrapped view would let the evaluator read an out-of-range index,
        and a negative one would make ``length()`` non-positive, which
        ``_rule_triggers`` reads as "no bars" rather than as the bug it is.
        Postconditions: ``length()`` returns ``i + 1``; raises ``ValueError``
        when the precondition is violated.
        """
        if not 0 <= i < view.length():
            raise ValueError(f"prefix index {i!r} is out of range for {view.length()!r} bars")
        self._view = view
        self._length = i + 1

    def length(self) -> int:
        """Postconditions: returns ``i + 1``, so ``length() - 1`` is ``i``."""
        return self._length

    def bar_field(self, field_name: str, i: int) -> float:
        """Postconditions: the wrapped view's value, unmodified.

        Preconditions: ``i`` is within this prefix — guaranteed by the
        evaluator, which only ever reads ``length() - 1`` and ``length() - 2``.
        """
        return self._view.bar_field(field_name, i)

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        """Postconditions: the wrapped view's value, unmodified.

        Preconditions: as :meth:`bar_field`.
        """
        return self._view.indicator(ref, i)


def resolve_signal_exit(
    rules: Sequence[ExitRule],
    entry: ReferenceEntryFill,
    symbol_bars: "Sequence[Bar]",
) -> Optional[ReferenceSignalExit]:
    """Model the ``SignalExitRule`` close of one already-opened reference position.

    The per-position core of :func:`replay_signal_exits`, exposed separately so
    a later step can drive it against a position whose entry came from
    somewhere other than a whole-spec replay — mirrors
    :func:`resolve_stop_loss_exit`/:func:`resolve_take_profit_family_exit`.

    Why resting-order semantics are deliberately NOT applied here
    ------------------------------------------------------------
    The other three rule kinds this module covers are modeled as resting
    orders — filling on their own trigger bar, at a level fixed in advance —
    because the engine's resting-exit migration makes that their target
    behavior, and modelling today's next-bar-open approximation instead would
    make every one of those trades diverge trivially the moment the migration
    lands (see this module's "Target behavior, not shipped behavior" section).
    ``SignalExitRule`` is the kind that migration deliberately leaves alone,
    and for a reason that is not scheduling: a predicate over a bar's OHLC and
    indicator values is only decidable once that bar has CLOSED, so there is no
    resting level to sit on the book in advance and no honest way to fill
    inside the trigger bar. Next-bar-open is the correct, look-ahead-safe fill,
    today and after the migration. Modelling this kind like the other three
    would therefore not anticipate a coming engine change — it would invent a
    divergence on every single signal exit, in a module whose entire purpose is
    to be an oracle the engine can be checked against.

    Two behavioral differences follow, and both are load-bearing:

    * **Eligible on ``entry_bar`` itself.** A resting order is stamped with its
      materialization bar and skipped until strictly after it, so the resting
      kinds start at ``entry_bar + 1``. A signal exit has no order to
      materialize; production's dispatcher evaluates its predicate from the
      first bar the position exists, with the ``just_opened`` gate already
      ``False`` for the market entries this simulator fills. The walk below
      therefore starts at ``entry.entry_bar``.
    * **Fill bar is one past the trigger bar.** ``exit_bar = trigger_bar + 1``,
      the only kind here where the two differ.

    Cross-kind competition is still out of scope, exactly as it is for the
    other two replays: the design doc's FIFO rule (a resting order
    materialized at entry beats a ``signal_exit`` close queued on the same bar)
    belongs to the combined simulator, not here. This walk selects only
    ``signal_exit`` intents and ignores every other kind the shared evaluator
    reports, the same way :class:`_RestingTakeProfitFamily` ignores stop-loss
    intents.

    Preconditions:
        - ``rules`` is the WORKING exit-rule list from
          :func:`working_exit_rules` (not raw ``spec.exit_rules``), so every
          index is the one a fired rule should record.
        - ``entry`` was produced against ``symbol_bars``: ``0 <= entry.entry_bar
          < len(symbol_bars)``.

    Postconditions:
        - Returns the close produced by the FIRST bar at or after
          ``entry.entry_bar`` whose lowest-spec-index ``SignalExitRule``
          predicate fires and whose following bar supplies a usable fill price,
          or ``None`` in any of three cases: ``rules`` contains no
          ``SignalExitRule`` at all (returned immediately, before any bar
          walk); no predicate ever fires; or every firing is dropped by one of
          the two rules below.
        - **Final-bar rule.** A predicate that fires on the last bar of
          ``symbol_bars`` has no next bar to fill against and emits no record,
          rather than fabricating a fill past the end of the data. This is the
          exact treatment :func:`~.reference_entries.replay_entry_rules` gives
          a final-bar entry trigger, and like it the walk then ends — there is
          no later bar to retry from.
        - **Nonpositive fill-bar open.** A firing whose fill bar's ``open`` is
          not a positive finite number — or which rounds away to zero in
          production's own price bucket — does not fire on that bar, exactly as
          if the predicate had not been satisfied, and the walk continues: a
          later bar's firing may still produce a record. Mirrors the design
          doc's uniform nonpositive-exit-reference rule and the entry side's
          identical fill-bar guard.
        - The returned record's ``exit_price`` is the fill bar's open rounded
          to production's own bid-price bucket (the pre-slippage reference
          level ``ReferenceTrade.exit_price`` is defined as), its ``exit_date``
          comes from the FILL bar, and its ``exit_rule_index`` indexes
          ``rules``.

    Note the absent ``entry_slippage_bps`` parameter, which both sibling
    resolvers take: a ``SignalExitRule``'s trigger reads only the
    ``HistoryView`` — never ``PositionState.entry_price`` or its watermarks —
    and its fill price is the fill bar's own open, so slippage provably cannot
    change any output on this path. Taking the parameter would be dead weight
    with a live failure mode attached: routing it through
    :func:`entry_price_basis` would let a degenerate anchor raise on a walk
    that never uses the anchor.

    Invariants: does not mutate ``rules``, ``entry``, or ``symbol_bars``, and
    is deterministic in its inputs.
    """
    n = len(symbol_bars)
    if not 0 <= entry.entry_bar < n:
        raise ValueError(f"entry.entry_bar {entry.entry_bar!r} is out of range for {n} bars")
    if not signal_exit_rules(rules):
        return None
    # Built once per position and shared by every bar's prefix wrapper, so each
    # indicator series is computed at most once for the whole walk. Constructed
    # from the same ``bars_to_frame`` the entry side uses, so both sides index
    # identical rows.
    view = PandasHistoryView(bars_to_frame(symbol_bars), {})
    # Constant across the walk: a signal predicate reads none of these fields
    # (see the docstring's note on the absent slippage parameter). ``qty`` is
    # the nominal 1.0 this module's other resolvers also use — the shared
    # evaluator only requires it to be positive — and the pre-slippage
    # ``entry.entry_price`` stands in for the anchor the non-signal rules
    # would want, since every intent they produce here is discarded unread.
    position = PositionState(
        symbol=entry.symbol,
        side=entry.side,
        qty=1.0,
        entry_price=entry.entry_price,
        high_since_entry=entry.entry_price,
        low_since_entry=entry.entry_price,
    )
    for trigger_bar in range(entry.entry_bar, n):
        intents = evaluate_exit_rules_for_position(
            rules,
            entry.symbol,
            position,
            _snapshot(symbol_bars[trigger_bar]),
            view=_PrefixHistoryView(view, trigger_bar),
            first_only=False,
        )
        winner = next(
            (intent for intent in intents if intent.rule_kind == "signal_exit"),
            None,
        )
        if winner is None:
            continue
        exit_bar = trigger_bar + 1
        if exit_bar >= n:
            break  # final-bar rule: no next bar to fill against
        fill_bar = symbol_bars[exit_bar]
        raw_open = fill_bar.open
        if not (raw_open > 0 and math.isfinite(raw_open)):
            continue  # fill-bar open guard: drop this firing, keep scanning
        exit_price = _round_price(raw_open)
        # A price below its own bucket's resolution survives the guard above
        # and still rounds away to zero (``round(0.00004, 4)`` is ``0.0``),
        # which ``ReferenceSignalExit`` would reject. Treat it as the same
        # unusable-fill case rather than letting it raise: the design doc's
        # uniform rule is that a degenerate bar suppresses one candidate fill,
        # never aborts the run.
        if not exit_price > 0:
            continue
        return ReferenceSignalExit(
            symbol=entry.symbol,
            entry_bar=entry.entry_bar,
            exit_bar=exit_bar,
            # ``Bar.timestamp`` is ISO-8601, so its first 10 characters are the
            # date — production truncates ``bar.timestamp[:10]`` identically.
            # Taken from the FILL bar, not the trigger bar: production stamps
            # the close with the bar it settled on.
            exit_date=fill_bar.timestamp[:10],
            exit_price=exit_price,
            exit_rule_kind="signal_exit",
            exit_rule_index=winner.rule_index,
        )
    return None


def replay_signal_exits(
    spec: StrategySpec,
    bars: "Mapping[str, Sequence[Bar]]",
) -> List[ReferenceSignalExit]:
    """Replay ``spec``'s ``SignalExitRule`` exits over ``bars``.

    Opens reference positions with the shared entry-side replay, then models
    each one's signal close at the next bar's open. Mirrors
    :func:`replay_stop_loss_exits`'s shape, minus the slippage parameter that
    cannot affect this kind (see :func:`resolve_signal_exit`).

    Preconditions:
        - ``spec`` is a validated ``StrategySpec`` with ``requires_custom_code``
          False.
        - ``bars`` maps symbol to a chronological ``Bar`` sequence (an empty
          sequence is skipped, not an error, mirroring the sibling replays'
          own stance).

    Postconditions:
        - Returns at most one ``ReferenceSignalExit`` per symbol, in the order
          the entry replay yields positions. Fewer whenever a position is still
          open when its bars run out, or the spec has no ``SignalExitRule`` at
          all.
        - Every returned record's ``exit_rule_index`` indexes
          :func:`working_exit_rules`'s list.

    Invariants:
        - No side effects: does not mutate ``spec`` or ``bars``, and performs
          no I/O.
        - Deterministic: identical ``(spec, bars)`` always produces an
          identical list — this function is a pure function of its two
          arguments, with no live-engine dependency.
        - Imports no module reaching ``trading_service/service.py`` or the four
          forbidden ``trading_service/engine/`` modules (see this module's
          docstring).
    """
    rules = working_exit_rules(spec)
    out: List[ReferenceSignalExit] = []
    for entry in replay_entry_rules(spec, bars):
        found = resolve_signal_exit(rules, entry, bars[entry.symbol])
        if found is not None:
            out.append(found)
    return out
