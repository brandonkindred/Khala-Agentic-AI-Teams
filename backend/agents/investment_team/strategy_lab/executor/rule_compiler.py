"""Pure-functional evaluator for structured ``ExitRule`` discriminated unions.

Issue #527 — the executor's bar loop owns enforcement of structured exit
rules. Strategy code authors the entry/signal logic; the engine reads
``StrategySpec.exit_rules`` and emits close orders when a rule fires.

Supported rule kinds (matching the discriminated ``ExitRule`` union in
``spec_dsl``):

* ``StopLossRule(pct, basis)`` — close when the bar's low (long) or
  high (short) crosses the rule's price floor. ``basis`` selects
  ``entry_price`` / ``trailing_high`` / ``trailing_low``.
* ``TakeProfitRule(pct)`` — close when the bar's high (long) or low
  (short) clears the rule's price target.
* ``ScaledTakeProfitRule(levels)`` — laddered take-profit: emit one
  partial-close intent for the next un-fired rung (the per-position cursor)
  when its target is reached, each sized as a fraction of the original entry
  qty. Rungs fire in order, one tranche per bar (the dispatcher advances the
  cursor); the remainder rides the other exit rules.
* ``SignalExitRule(when)`` — close when a predicate fires.  Requires a
  ``HistoryView`` per symbol passed via the ``views`` keyword to
  :func:`evaluate_exit_rules`.  When no view is available, the rule is
  a silent no-op for backward compatibility.

Only price-, P&L-, and signal-based exit rules are supported.

This module is intentionally side-effect free: it takes the current
per-position state and the current bar, and returns a list of
``ExitIntent`` records. The caller (``TradingService``) is responsible
for translating each intent into an actual close order on the order
book.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, NamedTuple, Optional, Sequence

from ..spec_dsl import (
    ExitRule,
    OcoBracketRule,
    ScaledTakeProfitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    protective_limit_price,
    protective_stop_price,
)
from .predicate_evaluator import HistoryView, evaluate_signal_exit_rules

ExitRuleKind = Literal["stop_loss", "take_profit", "scaled_take_profit", "signal_exit"]


@dataclass(frozen=True)
class PositionState:
    """Snapshot of one open position, as seen by the rule evaluator."""

    symbol: str
    side: Literal["long", "short"]
    qty: float
    entry_price: float
    high_since_entry: float
    low_since_entry: float


@dataclass(frozen=True)
class BarSnapshot:
    """Minimal bar payload the evaluator needs. Decoupled from ``contract.Bar``
    so unit tests can pass plain dataclasses without importing the trading
    service plumbing.
    """

    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExitIntent:
    """One rule-triggered close order request, ready for the engine to submit."""

    symbol: str
    rule_kind: ExitRuleKind
    rule_index: int  # index into the spec's ``exit_rules`` list, for traceability
    note: str = ""
    # ``StopLossRule.basis`` (``entry_price`` / ``trailing_high`` /
    # ``trailing_low``) for stop-loss intents, else ``None``. Carried as
    # additive metadata so downstream telemetry can distinguish a trailing
    # stop fire from a fixed stop fire WITHOUT changing ``rule_kind`` (and
    # therefore without changing the ``engine_exit:<rule_kind>`` close
    # ``reason`` that the conformance + alignment gates match by exact
    # equality).
    basis: Optional[str] = None
    # Execution style for the close the engine builds from this intent.
    # ``"market"`` (default, and the only value for non-stop-loss intents) emits
    # a guaranteed market close. ``"limit"`` (only for ``StopLossRule`` with
    # ``style="limit"``) tells the dispatcher to emit a *resting* STOP_LIMIT.
    style: str = "market"
    # Fully-resolved stop trigger level and protective-side limit price,
    # populated only for ``style="limit"`` stop-loss intents so the dispatcher
    # can construct the STOP_LIMIT without re-deriving prices or re-reading the
    # spec/position. ``limit_price`` already encodes the protective side (below
    # the stop for a long, above for a short).
    stop_price: Optional[float] = None
    limit_price: Optional[float] = None
    # Partial-exit metadata for scaled (laddered) take-profits. ``qty_fraction``
    # is the fraction of the position's *original entry qty* this intent closes
    # (``1.0`` = full close, the default for every non-scaled intent).
    # ``level_index`` identifies which rung of a ``ScaledTakeProfitRule`` fired
    # (``None`` for every other rule kind), so the dispatcher can fire each rung
    # at most once and diagnostics can count rungs separately.
    qty_fraction: float = 1.0
    level_index: Optional[int] = None

    def __post_init__(self) -> None:
        """Enforce the intent's structural contract at construction.

        These are invariants every producer (``_intent_for_rule``) already
        upholds; validating here turns a future producer bug into an immediate,
        local failure instead of a silently mis-sized or mis-routed close.

        Preconditions (on the constructor arguments):
          * ``style`` is ``"market"`` or ``"limit"``.
          * ``0 < qty_fraction <= 1.0`` — a close covers a positive fraction of
            the original entry qty, at most the whole position.
          * A scaled rung (``level_index is not None``) is ALWAYS a market
            scale-out (``style == "market"``): the dispatcher's scale-out path
            has no resting/requeue lifecycle for a limit rung, and a
            ``level_index >= 0`` rung index is well-formed.
        Postconditions: raises ``ValueError`` if any precondition is violated;
        otherwise the instance is a structurally valid intent.
        """
        if self.style not in ("market", "limit"):
            raise ValueError(f"ExitIntent.style must be 'market' or 'limit', got {self.style!r}")
        if not (0.0 < self.qty_fraction <= 1.0):
            raise ValueError(
                f"ExitIntent.qty_fraction must be in (0, 1], got {self.qty_fraction!r}"
            )
        if self.level_index is not None:
            if self.level_index < 0:
                raise ValueError(
                    f"ExitIntent.level_index must be non-negative, got {self.level_index!r}"
                )
            if self.style != "market":
                raise ValueError(
                    "a scaled-take-profit rung must be a market scale-out "
                    f"(style='market'), got style={self.style!r}"
                )

    @property
    def is_scaled_rung(self) -> bool:
        """Whether this intent is a scaled-take-profit LADDER RUNG.

        This reflects the intent's ORIGIN, not whether it closes 100%: a rung with
        ``qty_fraction == 1.0`` (or the final rung of a ladder summing to 1.0) is
        still a scaled rung, so the dispatcher routes it through the scale-out
        handler and sizes it off the ORIGINAL entry qty (and defers it while the
        entry is still filling). The engine then decides full-vs-partial cleanups
        from the resulting close qty vs the remaining position, not from this flag —
        so an emptying rung still retires competing resting orders. This keeps the
        scale-out path decoupled from the literal ``"scaled_take_profit"`` kind.

        Preconditions: none (reads only ``level_index``). Postconditions: ``True``
        iff ``level_index`` is set (only laddered rungs carry one).
        """
        return self.level_index is not None


def is_limit_stop_rule(rule: ExitRule) -> bool:
    """Whether ``rule`` is a limit-style stop-loss — the only rule kind the
    structured-exit path rests as a STOP_LIMIT. Single source of this predicate
    (used by the dispatcher's ``_has_limit_stop_rule``).

    Preconditions: ``rule`` is an ``ExitRule`` instance (a discriminated-union
    member). Non-``StopLossRule`` members are valid input and yield ``False``.
    Postconditions: ``True`` iff ``rule`` is a ``StopLossRule`` with
    ``style == "limit"``.
    """
    return isinstance(rule, StopLossRule) and getattr(rule, "style", "market") == "limit"


# Shared immutable empty cursor map — only ``.get()``-queried — so the common
# no-ladder / no-cursor path allocates nothing per bar, and serves as the default
# ``cursor_map`` for non-laddered specs. Wrapped in ``MappingProxyType`` so an
# accidental mutation can't corrupt the shared default for all callers.
_EMPTY_CURSOR: Mapping[int, int] = MappingProxyType({})
# The outer (``symbol -> cursor map``) empty default — a distinct type from
# ``_EMPTY_CURSOR`` so the ``scaled_cursors`` fallback stays type-correct.
_EMPTY_SYMBOL_CURSORS: Mapping[str, Mapping[int, int]] = MappingProxyType({})


def evaluate_exit_rules(
    rules: Sequence[ExitRule],
    positions: Mapping[str, PositionState],
    bars: Mapping[str, BarSnapshot],
    *,
    views: Optional[Mapping[str, HistoryView]] = None,
    first_only: bool = True,
    scaled_cursors: Optional[Mapping[str, Mapping[int, int]]] = None,
    exclude_rule_index: Optional[int] = None,
) -> list[ExitIntent]:
    """Return triggered ``ExitIntent``\\ s per open position, in spec priority order.

    Order semantics:
      * Iterate rules in spec order. With ``first_only`` (default), the first
        rule that fires for a position wins and the rest are skipped (one close
        per position per bar). With ``first_only=False``, all triggered rules for
        the position are returned in spec order, so the caller can choose among
        them (e.g. skip an exit whose structured order is already in flight) —
        the pure evaluator stays unaware of any such runtime state.
      * ``SignalExitRule`` evaluation requires a ``HistoryView`` for the
        symbol (passed via ``views``). When ``views`` is ``None`` or the
        symbol has no view, ``SignalExitRule`` is a silent no-op for
        backward compatibility.
      * A ``ScaledTakeProfitRule`` yields **at most one** intent per call: its
        next un-fired rung (the cursor), and only when that rung's target has
        been reached. ``scaled_cursors`` maps ``symbol -> rule_index -> next
        un-fired rung index``; an absent entry defaults to cursor ``0``. Because
        rungs fire in strict order one tranche per bar, the cursor rung is the
        only one that can fire next, so the evaluator never allocates intents for
        rungs that already fired or are not yet actionable.
      * ``exclude_rule_index``, when set, drops the rule at that exact spec
        index for every symbol — used when a resting-order mechanism has
        been attached for that rule instead of this bar-close evaluator (see
        :func:`_filtered_intent_for_rule`).

    Preconditions: each ``positions``/``bars`` value is keyed by symbol; ``rules``
    are ``ExitRule`` members; ``views`` (if given) maps symbols to ``HistoryView``;
    ``scaled_cursors`` (if given) maps symbol to a ``rule_index -> rung`` cursor map.
    Postconditions: returns one ``ExitIntent`` per (open position × triggered rule)
    in spec order — at most one per position when ``first_only``, all triggered
    otherwise; a scaled rule contributes at most its cursor rung; the rule at
    ``exclude_rule_index`` never contributes an intent. Positions with
    non-positive qty or no matching bar yield none. Each limit-style stop intent
    carries its fully-resolved ``stop_price``/``limit_price`` regardless of
    ``first_only`` (``first_only`` controls only how many intents are returned).
    """
    intents: list[ExitIntent] = []
    for symbol, position in positions.items():
        if position.qty <= 0:
            continue
        bar = bars.get(symbol)
        if bar is None:
            continue
        sym_view = views.get(symbol) if views is not None else None
        # Explicit ``is not None`` (not ``or``) so an empty ``scaled_cursors`` —
        # "no cursors for any symbol" — is honoured, mirroring the ``views`` check.
        outer = scaled_cursors if scaled_cursors is not None else _EMPTY_SYMBOL_CURSORS
        cursor_map = outer.get(symbol, _EMPTY_CURSOR)
        intents.extend(
            evaluate_exit_rules_for_position(
                rules,
                symbol,
                position,
                bar,
                view=sym_view,
                first_only=first_only,
                cursor_map=cursor_map,
                exclude_rule_index=exclude_rule_index,
            )
        )
    return intents


def evaluate_exit_rules_for_position(
    rules: Sequence[ExitRule],
    symbol: str,
    position: PositionState,
    bar: BarSnapshot,
    *,
    view: Optional[HistoryView] = None,
    first_only: bool = True,
    cursor_map: Mapping[int, int] = _EMPTY_CURSOR,
    exclude_limit_style: bool = False,
    exclude_scaled: bool = False,
    exclude_rule_index: Optional[int] = None,
) -> list[ExitIntent]:
    """Triggered ``ExitIntent``\\ s for ONE open position, in spec priority order.

    The single-symbol core of :func:`evaluate_exit_rules`. The per-bar dispatcher
    calls this directly, so the hot path never wraps a lone position/bar/cursor in
    throwaway ``{symbol: ...}`` dicts.

    ``exclude_limit_style`` drops any limit-style stop intent — used when that stop
    already rests as a STOP_LIMIT — so the first *non-resting* rule wins in a single
    pass, with no "collect every trigger then filter" second walk. ``exclude_scaled``
    drops any scaled-take-profit rung (regardless of ``qty_fraction``) — used when
    the position's entry is still filling, so a deferred rung (which must be sized
    off the not-yet-settled original qty) does not pre-empt a lower-priority
    full-position exit (e.g. a stop) that should still fire this bar. ``exclude_rule_index``
    drops the rule at that exact spec index outright — used when a resting-order
    mechanism has been attached for that rule instead of this bar-close evaluator
    (see :func:`_filtered_intent_for_rule`).

    ``position`` is read-only here and is NOT retained past this call — the engine
    may pass a single mutable view it reuses each bar (``_PositionStateView``), so a
    caller must never store the reference; every value an :class:`ExitIntent` needs
    is copied out before returning.

    Preconditions: ``position.qty > 0``; ``bar`` exposes ``high``/``low``/``close``
    (a :class:`BarSnapshot`, or any structurally-compatible bar such as
    ``contract.Bar``); ``cursor_map`` maps a ladder ``rule_index`` to its next
    un-fired rung.
    Postconditions: returns the triggered intents in spec order — at most one when
    ``first_only`` — each ``ScaledTakeProfitRule`` contributing at most its cursor
    rung; the rule at ``exclude_rule_index`` omitted outright, limit-style intents
    omitted when ``exclude_limit_style``, and scaled rungs omitted when
    ``exclude_scaled``.
    """
    if first_only:
        # Hot path: the per-bar dispatcher wants only the spec-priority winner, so
        # delegate to the allocation-free scan and wrap its single result (or none).
        intent = first_exit_intent_for_position(
            rules,
            symbol,
            position,
            bar,
            view=view,
            cursor_map=cursor_map,
            exclude_limit_style=exclude_limit_style,
            exclude_scaled=exclude_scaled,
            exclude_rule_index=exclude_rule_index,
        )
        return [intent] if intent is not None else []
    intents: list[ExitIntent] = []
    for idx, rule in enumerate(rules):
        intent = _filtered_intent_for_rule(
            rule,
            symbol,
            idx,
            position,
            bar,
            view,
            cursor_map,
            exclude_limit_style=exclude_limit_style,
            exclude_scaled=exclude_scaled,
            exclude_rule_index=exclude_rule_index,
        )
        if intent is not None:
            intents.append(intent)
    return intents


def _filtered_intent_for_rule(
    rule: ExitRule,
    symbol: str,
    idx: int,
    position: PositionState,
    bar: BarSnapshot,
    view: Optional[HistoryView],
    cursor_map: Mapping[int, int],
    *,
    exclude_limit_style: bool,
    exclude_scaled: bool,
    exclude_rule_index: Optional[int] = None,
) -> Optional[ExitIntent]:
    """:func:`_intent_for_rule` with the dispatcher's skip filters applied.

    Single source of the "does this rule produce an actionable intent this bar"
    decision, shared by the list-returning :func:`evaluate_exit_rules_for_position`
    and the allocation-free :func:`first_exit_intent_for_position` so the filter
    logic (exclude already-resting limit stops / deferred scaled rungs / a rule
    ceded to a resting-order mechanism) lives in one place and the two entry
    points can never diverge.

    ``exclude_rule_index`` drops the rule at that exact spec index outright,
    before ``_intent_for_rule`` is even called — unlike the two intent-shape
    filters below, which act on what the produced intent looks like,
    exclusion here is by rule identity (its position in ``exit_rules``), since
    the excluded rule's intent would otherwise be indistinguishable from a
    same-kind rule that must keep firing here (e.g. the short-safety auto-stop
    shares ``rule_kind``/``basis``/``style`` with the resting-eligible variant
    but is never resting-attached — see ``_is_resting_stop_loss`` in
    ``trading_service.service``). Used when a resting-order mechanism (the
    entry_price/market stop-loss migration) has been attached for that rule
    instead, so the bar-close evaluator must never also produce an intent for
    it — see ``_EngineExitDispatcher.exclude_rule_index`` for the mutual-
    exclusion contract this enforces.

    Preconditions: as :func:`_intent_for_rule`.
    Postconditions: returns the rule's intent, or ``None`` when ``idx ==
    exclude_rule_index`` OR the rule does not fire OR the intent is a
    limit-style stop and ``exclude_limit_style`` OR the intent is a scaled
    rung and ``exclude_scaled``.
    """
    if idx == exclude_rule_index:
        return None
    intent = _intent_for_rule(rule, symbol, idx, position, bar, view, cursor_map)
    if intent is None:
        return None
    if exclude_limit_style and intent.style == "limit":
        return None
    if exclude_scaled and intent.is_scaled_rung:
        return None
    return intent


def first_exit_intent_for_position(
    rules: Sequence[ExitRule],
    symbol: str,
    position: PositionState,
    bar: BarSnapshot,
    *,
    view: Optional[HistoryView] = None,
    cursor_map: Mapping[int, int] = _EMPTY_CURSOR,
    exclude_limit_style: bool = False,
    exclude_scaled: bool = False,
    exclude_rule_index: Optional[int] = None,
) -> Optional[ExitIntent]:
    """The highest-priority triggered ``ExitIntent`` for ONE position, or ``None``.

    The allocation-free ``first_only`` core of
    :func:`evaluate_exit_rules_for_position`. The per-bar dispatcher evaluates one
    open position per symbol per bar and acts on a single intent, so this walks the
    rules in spec order and returns the first that fires and survives the
    ``exclude_limit_style`` / ``exclude_scaled`` / ``exclude_rule_index`` filters —
    without building the throwaway one-element list the list-returning variant
    would allocate on every bar of every open position.

    ``exclude_rule_index``, when set, is the spec index of a rule ceded to a
    resting-order mechanism for this run (e.g. the entry_price/market
    stop-loss migration's resting ``STOP`` attachment) — see
    :func:`_filtered_intent_for_rule` for why this is an index match rather
    than an intent-shape filter like the other two.

    Preconditions: as :func:`evaluate_exit_rules_for_position` — ``position.qty >
    0``; ``bar`` exposes ``high``/``low``/``close``; ``cursor_map`` maps a ladder
    ``rule_index`` to its next un-fired rung.
    Postconditions: returns the spec-priority winning intent — the rule at
    ``exclude_rule_index`` skipped outright, a limit-style intent skipped when
    ``exclude_limit_style``, a scaled rung skipped when ``exclude_scaled`` — or
    ``None`` when no rule fires (or the only triggers are excluded). Allocates
    no result list.
    """
    for idx, rule in enumerate(rules):
        intent = _filtered_intent_for_rule(
            rule,
            symbol,
            idx,
            position,
            bar,
            view,
            cursor_map,
            exclude_limit_style=exclude_limit_style,
            exclude_scaled=exclude_scaled,
            exclude_rule_index=exclude_rule_index,
        )
        if intent is not None:
            return intent
    return None


def _intent_for_rule(
    rule: ExitRule,
    symbol: str,
    idx: int,
    position: PositionState,
    bar: BarSnapshot,
    view: Optional[HistoryView],
    cursor_map: Mapping[int, int],
) -> Optional[ExitIntent]:
    """Build the single ``ExitIntent`` a ``rule`` triggers for one position.

    Preconditions: ``position.qty > 0`` and ``bar`` is the symbol's current bar;
    ``cursor_map`` maps a ladder ``rule_index`` to its next un-fired rung.
    Postconditions: returns ``None`` when the rule does not fire; one intent for a
    triggered stop-loss / take-profit / signal-exit; and the cursor-rung intent
    (only when its target is reached) for a ``ScaledTakeProfitRule``. A limit-style
    stop's intent carries its fully-resolved ``stop_price`` / ``limit_price``.

    An ``OcoBracketRule`` is NOT evaluated here: the bracket is attached to the
    entry order and materialized by the engine into resting OCO children, so the
    bar-by-bar dispatcher must not also emit a close for it (dual emission). It is
    skipped at this single chokepoint (both evaluation entry points funnel through
    here) rather than reaching ``_rule_triggers`` / ``_kind_of``, which only know
    the bar-by-bar kinds.
    """
    if isinstance(rule, OcoBracketRule):
        return None
    if isinstance(rule, ScaledTakeProfitRule):
        rung = _next_scaled_rung(rule, position, bar, cursor_map.get(idx, 0))
        if rung is None:
            return None
        level = rule.levels[rung]
        return ExitIntent(
            symbol=symbol,
            rule_kind="scaled_take_profit",
            rule_index=idx,
            note=level.note or rule.note or "",
            style="market",  # a scaled rung always emits a market scale-out
            level_index=rung,
            qty_fraction=level.qty_fraction,
        )

    if not _rule_triggers(rule, position, bar, view):
        return None

    style = getattr(rule, "style", "market") or "market"
    # A limit-style stop carries its fully-resolved stop level + protective-side
    # limit so the dispatcher can rest a STOP_LIMIT without re-deriving prices.
    # (Limit-style is restricted to the ``entry_price`` basis — see
    # ``StopLossRule`` — so the level is a static offset off the entry price.)
    stop_price: Optional[float] = None
    limit_price: Optional[float] = None
    if isinstance(rule, StopLossRule) and style == "limit":
        prices = stop_limit_prices(rule, position)
        stop_price = prices.stop_price
        limit_price = prices.limit_price
    return ExitIntent(
        symbol=symbol,
        rule_kind=_kind_of(rule),
        rule_index=idx,
        note=getattr(rule, "note", "") or "",
        basis=getattr(rule, "basis", None),
        style=style,
        stop_price=stop_price,
        limit_price=limit_price,
    )


def _next_scaled_rung(
    rule: ScaledTakeProfitRule, position: PositionState, bar: BarSnapshot, cursor: int
) -> Optional[int]:
    """The next rung to scale out (``cursor``), if its target has been reached.

    Only the cursor rung can fire next: rungs ``< cursor`` already fired, and
    because ``pct`` is strictly increasing the un-fired rungs above the cursor
    have strictly higher targets, so none of them can be reached while the cursor
    rung is not. Checking the single cursor level is therefore both sufficient and
    O(1) per bar — no per-bar scan of every rung, no allocation of intents for
    rungs that already fired or are not yet actionable.

    Eligibility is high-water-mark based: the rung counts as reached once the
    position's favorable extreme SINCE ENTRY (the running watermark, with this
    bar's extreme folded in) has reached ``entry * (1 ± level.pct)`` — so a rung a
    gap bar cleared stays eligible even after a later-bar retrace.

    Preconditions: ``rule.levels`` has strictly-increasing ``pct`` (DSL-enforced);
    ``0 <= cursor`` is the next un-fired rung (enforced with an explicit raise so it
    holds under ``python -O``); ``position.side`` is ``"long"`` or ``"short"`` (any
    other value fails fast); ``position.high_since_entry`` / ``low_since_entry`` are
    the running watermarks as of the prior bar — non-None ``float``\\ s on
    :class:`PositionState` (initialized to ``entry_price`` at open, never ``None``),
    so the ``max``/``min`` below are total.
    Postconditions: returns ``cursor`` when the cursor rung's target is reached and
    ``cursor`` is in range, else ``None`` (ladder exhausted or target not reached).
    """
    if cursor < 0:
        raise ValueError(f"cursor must be non-negative (the next un-fired rung), got {cursor}")
    if cursor >= len(rule.levels):
        return None
    level = rule.levels[cursor]
    if position.side == "long":
        peak = max(position.high_since_entry, bar.high)
        return cursor if peak >= position.entry_price * (1.0 + level.pct) else None
    if position.side == "short":
        trough = min(position.low_since_entry, bar.low)
        return cursor if trough <= position.entry_price * (1.0 - level.pct) else None
    # ``side`` is a Literal["long", "short"]; fail fast rather than silently
    # applying the short branch to an unexpected value.
    raise ValueError(  # pragma: no cover - side is Literal["long", "short"]
        f"Unsupported position side: {position.side!r}"
    )


def stop_loss_level(rule: StopLossRule, position: PositionState) -> float:
    """Resolve the price level at which ``rule`` floors (long) / caps (short)
    the position. Single source of the stop-level *reference selection*
    (entry price vs. the running trailing extreme); the actual floor/cap
    geometry off that reference is :func:`spec_dsl.protective_stop_price`,
    shared with every other consumer that must derive the same level from the
    same reference (see that function's docstring for the full list) so none
    of them can silently drift apart. :func:`stop_loss_triggers` compares the
    bar against this level, :func:`stop_limit_prices` rests a STOP_LIMIT off
    it, and the reference-ledger exit replay derives its fill price from it —
    so the trigger decision, the resting limit, and the modeled fill can never
    disagree. Public for that last caller, which lives outside this module and
    must not re-derive the formula.

    Preconditions: ``rule`` is side-compatible with ``position`` — the basis can
    fire for this side. ``stop_loss_triggers`` enforces this by returning early
    for a mismatched basis (``trailing_low`` on a long / ``trailing_high`` on a
    short) before calling this helper, and the evaluator only resolves a level
    for a rule that just triggered.
    Postconditions: returns ``entry_price * (1 - pct)`` for a long and
    ``entry_price * (1 + pct)`` for a short on the ``entry_price`` basis; for a
    trailing basis it floors off the running high (long) / caps off the running
    low (short).
    """
    is_long = position.side == "long"
    if is_long:
        ref = position.high_since_entry if rule.basis == "trailing_high" else position.entry_price
    else:
        ref = position.low_since_entry if rule.basis == "trailing_low" else position.entry_price
    return protective_stop_price(ref, rule.pct, is_long=is_long)


class StopLimitPrices(NamedTuple):
    """The two resting prices of a limit-style stop, named so they cannot swap.

    Both are same-typed floats whose only distinguishing property — the limit
    sits on the protective side of the stop — lives in their meaning, not their
    type. Returned as a named pair so a call site reads
    ``prices.limit_price`` rather than relying on positional order: a
    transposed unpack would type-check silently and rest a STOP_LIMIT with the
    two prices swapped, which is a wrong fill rather than a loud failure.
    """

    stop_price: float
    limit_price: float


def stop_limit_prices(rule: StopLossRule, position: PositionState) -> StopLimitPrices:
    """Resolve a limit-style stop's stop/limit price pair.

    Single source of the limit-style geometry, the way :func:`stop_loss_level` is
    for the trigger level: the offset is a fraction OF THE STOP LEVEL
    (``stop_price * limit_offset_pct``), and only then does
    :func:`protective_limit_price` apply the side's sign convention. Splitting
    those two steps across call sites is how the two halves drift, so callers
    that need the resting prices — the dispatcher's intent builder here, and the
    reference-ledger exit replay — take them from this one place.

    Preconditions, on ``rule``: ``style == "limit"``, ``basis ==
    "entry_price"``, and ``limit_offset_pct`` populated and in ``(0, 1)``.
    ``StopLossRule``'s validator already enforces all four, but they are
    re-asserted here rather than delegated: this helper is the single source of
    the limit-style geometry for both the dispatcher and the reference ledger,
    so a future loosening of that validator would otherwise let it silently
    compute prices for a shape it was never designed for — a trailing-basis
    limit stop re-prices every bar, which a static resting limit cannot
    represent, and an offset outside ``(0, 1)`` rests the limit level with the
    stop (``0.0``) or through zero (``>= 1.0``), neither of which is
    protective.
    Preconditions, on ``position``: ``side`` is ``"long"``/``"short"`` and
    ``entry_price > 0``. These are the CALLER's to establish and are not
    re-checked here — :func:`stop_loss_level`, which resolves the level this
    builds on, reads the same fields under the same unchecked contract, so
    guarding them on this path alone would make the market-style and
    limit-style paths disagree about who owns them.
    Postconditions: returns a :class:`StopLimitPrices` whose ``stop_price`` is
    the level :func:`stop_loss_level` resolves and whose ``limit_price`` sits on
    the protective side of it — below for a long close, above for a short. Both
    are strictly positive: ``pct < 1.0`` and ``limit_offset_pct < 1.0`` keep a
    long's ``entry * (1 - pct) * (1 - limit_offset_pct)`` above zero, and the
    short side only ever adds.
    Raises ``ValueError`` when any of the four ``rule`` preconditions above is
    violated.
    """
    if rule.style != "limit":
        raise ValueError(f"stop_limit_prices requires style='limit', got {rule.style!r}")
    if rule.basis != "entry_price":
        raise ValueError(
            "a limit-style stop rests at a static level, so it requires "
            f"basis='entry_price', got {rule.basis!r}"
        )
    if rule.limit_offset_pct is None:
        raise ValueError("limit-style StopLossRule requires limit_offset_pct")
    if not 0.0 < rule.limit_offset_pct < 1.0:
        raise ValueError(
            "limit_offset_pct must be in (0, 1) for the limit to rest on the "
            f"protective side of the stop, got {rule.limit_offset_pct!r}"
        )
    stop_price = stop_loss_level(rule, position)
    offset = stop_price * rule.limit_offset_pct
    limit_price = protective_limit_price(stop_price, offset, closing_long=(position.side == "long"))
    return StopLimitPrices(stop_price=stop_price, limit_price=limit_price)


def _kind_of(rule: ExitRule) -> ExitRuleKind:
    if isinstance(rule, StopLossRule):
        return "stop_loss"
    if isinstance(rule, TakeProfitRule):
        return "take_profit"
    if isinstance(rule, ScaledTakeProfitRule):
        return "scaled_take_profit"
    if isinstance(rule, SignalExitRule):
        return "signal_exit"
    raise TypeError(f"unknown ExitRule subclass: {type(rule).__name__}")


def _rule_triggers(
    rule: ExitRule,
    position: PositionState,
    bar: BarSnapshot,
    view: Optional[HistoryView] = None,
) -> bool:
    if isinstance(rule, StopLossRule):
        return stop_loss_triggers(rule, position, bar)

    if isinstance(rule, TakeProfitRule):
        return _take_profit_triggers(rule, position, bar)

    if isinstance(rule, SignalExitRule):
        if view is None:
            return False
        i = view.length() - 1
        if i < 0:
            return False
        match = evaluate_signal_exit_rules([rule], view, i)
        return match is not None

    raise TypeError(f"unknown ExitRule subclass: {type(rule).__name__}")


def stop_loss_triggers(rule: StopLossRule, position: PositionState, bar: BarSnapshot) -> bool:
    """Decide whether ``rule`` would fire against ``bar`` for ``position``.

    Public trigger-decision entry point: besides the executor's own bar loop
    (via :func:`_rule_triggers`), the post-hoc ``ExitRuleConformanceGate``
    trailing replay imports this so the gate and the engine share one source of
    stop-trigger geometry and can never drift. Pairs with the public
    :class:`PositionState` / :class:`BarSnapshot` evaluator types.

    Preconditions: ``rule`` is a ``StopLossRule``; ``position``/``bar`` are
    populated snapshots. Postconditions: returns ``True`` iff the bar breaches the
    rule's floor (long) / cap (short) for the matching side; a basis that does not
    apply to ``position.side`` (``trailing_low`` on a long / ``trailing_high`` on a
    short) is a no-op returning ``False``.
    """
    if position.side == "long":
        if rule.basis == "trailing_low":
            # ``trailing_low`` only makes sense for shorts; treated as no-op
            # for longs rather than firing, so a misconfigured spec doesn't
            # silently flush every long position on bar 1.
            return False
        return bar.low <= stop_loss_level(rule, position)

    # short
    if rule.basis == "trailing_high":
        # ``trailing_high`` is the long-side counterpart; no-op for shorts.
        return False
    return bar.high >= stop_loss_level(rule, position)


def _take_profit_triggers(rule: TakeProfitRule, position: PositionState, bar: BarSnapshot) -> bool:
    pct = rule.pct
    if position.side == "long":
        target = position.entry_price * (1.0 + pct)
        return bar.high >= target
    target = position.entry_price * (1.0 - pct)
    return bar.low <= target
