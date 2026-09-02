"""Exit-side replay for the reference-ledger simulator: ``StopLossRule``.

Second half of the reference-ledger simulator designed in
``system_design/reference_ledger_trade_model.md``, attaching exit modelling to
the entry-side replay in :mod:`reference_entries`. This step covers exactly one
exit rule kind — ``StopLossRule``, the most-used one — across all four variants
(``entry_price`` / trailing bases x ``market`` / ``limit`` styles). Take-profit,
scaled take-profit, signal exits, and the combined simulator that lets every
kind compete on the same bar are later steps.

Trigger decisions are NOT re-derived here: they come from the shared, pure
``rule_compiler.stop_loss_triggers`` (and its ``stop_loss_level`` /
``stop_limit_prices`` geometry), the same functions the live evaluator and the
post-hoc conformance gate call. What this module adds — and what those shared
functions deliberately do not cover — is resting-order FILL mechanics: which
bar the order fills on, at what price, gap handling, the trailing watermark's
own bar-by-bar ratchet, and the stop-limit arm/latch.

Modeled behavior is the design doc's TARGET resting-order behavior, not what
the engine ships today
--------------------------------------------------------------------------
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
  re-enters, so this returns at most one exit per symbol. Re-entry after a stop
  closes a position needs the combined driver that resolves exits before
  entries on each bar.
* No quantity/sizing, capital ledger, or risk-limit admission gates; no
  cross-symbol merged ``(timestamp, symbol)`` timeline; no competition against
  other exit rule kinds (a take-profit or signal exit that would have closed the
  position first is simply not modeled yet, so a stop here may fire on a bar
  where the full simulator would not have had a position left to close).
* ``ReferenceStopLossExit`` is correspondingly narrower than the design doc's
  ``ReferenceTrade``, exactly as ``ReferenceEntryFill`` is on the entry side:
  its fields match ``ReferenceTrade``'s exit-side fields 1:1 in
  name/type/semantics so a later step can join an entry fill and an exit into a
  full ``ReferenceTrade`` without renaming anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Literal, Mapping, Optional, Sequence, Tuple

from ...models import StrategySpec
from ..spec_dsl import ExitRule, StopLossRule, first_side_stop_factor, stop_caps_side
from .reference_entries import ReferenceEntryFill, replay_entry_rules
from .rule_compiler import (
    BarSnapshot,
    PositionState,
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


def entry_price_basis(raw_open: float, side: str, entry_slippage_bps: float) -> float:
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
    Postconditions: returns ``round(raw_open * (1 + bps/10_000), dp)`` for a
    long and ``round(raw_open * (1 - bps/10_000), dp)`` for a short, with ``dp``
    taken from ``raw_open``'s own bucket. Strictly positive.
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
    return round(raw_open * multiplier, _decimals_for(raw_open))


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
    ``requires_custom_code`` False.
    Postconditions: returns a NEW list — ``spec.exit_rules`` is never mutated —
    equal to ``spec.exit_rules`` when the spec has no short entry rule or
    already carries an effective short-side stop, else that list with one
    synthetic ``StopLossRule`` appended at index ``len(spec.exit_rules)``.
    """
    rules: List[ExitRule] = list(spec.exit_rules)
    shorts_possible = any(rule.side == "short" for rule in spec.entry_rules)
    if shorts_possible and first_side_stop_factor(rules, "short") is None:
        rules.append(StopLossRule(pct=_SHORT_SAFETY_STOP_PCT, basis=_SHORT_SAFETY_STOP_BASIS))
    return rules


def stop_loss_rules_for_side(
    rules: Sequence[ExitRule], side: str
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


class _RestingStopLoss:
    """One modeled resting stop order for one open position.

    Owns the two pieces of per-position state the shared, stateless
    ``rule_compiler`` decision functions cannot carry across bars: the trailing
    watermark and the stop-limit arm/latch flag. Kept as an object rather than
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
        self._armed: dict[int, bool] = {}

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
        when the rule does not fire on this bar. May latch ``_armed[idx]`` as a
        side effect for a limit-style rule whose stop level is breached — that
        latch is deliberate engine state that must survive a non-filling bar.
        """
        position = self._position()
        if rule.style == "limit":
            _stop_price, limit_price = stop_limit_prices(rule, position)
            if not self._armed.get(idx, False):
                if not stop_loss_triggers(rule, position, _snapshot(bar)):
                    return None
                self._armed[idx] = True
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
