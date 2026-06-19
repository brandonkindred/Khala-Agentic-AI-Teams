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
from typing import Literal, Mapping, Optional, Sequence

from ..spec_dsl import (
    ExitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    protective_limit_price,
)
from .predicate_evaluator import HistoryView, evaluate_signal_exit_rules

ExitRuleKind = Literal["stop_loss", "take_profit", "signal_exit"]


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


def is_limit_stop_rule(rule: ExitRule) -> bool:
    """Whether ``rule`` is a limit-style stop-loss — the only rule kind the
    structured-exit path rests as a STOP_LIMIT. Single source of this predicate
    (used by the dispatcher's ``_has_limit_stop_rule``).

    Postconditions: ``True`` iff ``rule`` is a ``StopLossRule`` with
    ``style == "limit"``.
    """
    return isinstance(rule, StopLossRule) and getattr(rule, "style", "market") == "limit"


def evaluate_exit_rules(
    rules: Sequence[ExitRule],
    positions: Mapping[str, PositionState],
    bars: Mapping[str, BarSnapshot],
    *,
    views: Optional[Mapping[str, HistoryView]] = None,
    first_only: bool = True,
) -> list[ExitIntent]:
    """Return triggered ``ExitIntent``\\ s per open position, in spec priority order.

    Order semantics:
      * Iterate rules in spec order. With ``first_only`` (default), the first
        rule that fires for a position wins and the rest are skipped (one close
        per position per bar). With ``first_only=False``, all triggered rules for
        the position are returned in spec order, so the caller can choose among
        them (e.g. skip an exit whose structured order is already in flight) —
        the pure evaluator stays unaware of any such runtime state.
      * Positions with no open qty (or missing from ``bars``) are skipped.
      * ``SignalExitRule`` evaluation requires a ``HistoryView`` for the
        symbol (passed via ``views``). When ``views`` is ``None`` or the
        symbol has no view, ``SignalExitRule`` is a silent no-op for
        backward compatibility.
    """
    intents: list[ExitIntent] = []
    for symbol, position in positions.items():
        if position.qty <= 0:
            continue
        bar = bars.get(symbol)
        if bar is None:
            continue
        sym_view = views.get(symbol) if views is not None else None
        for idx, rule in enumerate(rules):
            if _rule_triggers(rule, position, bar, sym_view):
                style = getattr(rule, "style", "market") or "market"
                # A limit-style stop carries its fully-resolved stop level and
                # protective-side limit so the dispatcher can rest a STOP_LIMIT
                # without re-deriving prices. Limit-style is restricted to the
                # ``entry_price`` basis (see ``StopLossRule``), so the level is a
                # static offset off the entry price.
                stop_price: Optional[float] = None
                limit_price: Optional[float] = None
                if isinstance(rule, StopLossRule) and style == "limit":
                    stop_price = _stop_loss_level(rule, position)
                    offset = stop_price * rule.limit_offset_pct
                    limit_price = protective_limit_price(
                        stop_price, offset, closing_long=(position.side == "long")
                    )
                intents.append(
                    ExitIntent(
                        symbol=symbol,
                        rule_kind=_kind_of(rule),
                        rule_index=idx,
                        note=getattr(rule, "note", "") or "",
                        basis=getattr(rule, "basis", None),
                        style=style,
                        stop_price=stop_price,
                        limit_price=limit_price,
                    )
                )
                if first_only:
                    break
    return intents


def _stop_loss_level(rule: StopLossRule, position: PositionState) -> float:
    """Resolve the price level at which ``rule`` floors (long) / caps (short)
    the position. Single source of the stop-level geometry: :func:`_stop_loss_triggers`
    compares the bar against this level, and the limit-style evaluator rests a
    STOP_LIMIT here, so the trigger decision and the resting limit can never
    disagree.

    Preconditions: ``rule`` is side-compatible with ``position`` — the basis can
    fire for this side. ``_stop_loss_triggers`` enforces this by returning early
    for a mismatched basis (``trailing_low`` on a long / ``trailing_high`` on a
    short) before calling this helper, and the evaluator only resolves a level
    for a rule that just triggered.
    Postconditions: returns ``entry_price * (1 - pct)`` for a long and
    ``entry_price * (1 + pct)`` for a short on the ``entry_price`` basis; for a
    trailing basis it floors off the running high (long) / caps off the running
    low (short).
    """
    pct = rule.pct
    if position.side == "long":
        ref = position.high_since_entry if rule.basis == "trailing_high" else position.entry_price
        return ref * (1.0 - pct)
    ref = position.low_since_entry if rule.basis == "trailing_low" else position.entry_price
    return ref * (1.0 + pct)


def _kind_of(rule: ExitRule) -> ExitRuleKind:
    if isinstance(rule, StopLossRule):
        return "stop_loss"
    if isinstance(rule, TakeProfitRule):
        return "take_profit"
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
        return _stop_loss_triggers(rule, position, bar)

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


def _stop_loss_triggers(rule: StopLossRule, position: PositionState, bar: BarSnapshot) -> bool:
    if position.side == "long":
        if rule.basis == "trailing_low":
            # ``trailing_low`` only makes sense for shorts; treated as no-op
            # for longs rather than firing, so a misconfigured spec doesn't
            # silently flush every long position on bar 1.
            return False
        return bar.low <= _stop_loss_level(rule, position)

    # short
    if rule.basis == "trailing_high":
        # ``trailing_high`` is the long-side counterpart; no-op for shorts.
        return False
    return bar.high >= _stop_loss_level(rule, position)


def _take_profit_triggers(rule: TakeProfitRule, position: PositionState, bar: BarSnapshot) -> bool:
    pct = rule.pct
    if position.side == "long":
        target = position.entry_price * (1.0 + pct)
        return bar.high >= target
    target = position.entry_price * (1.0 - pct)
    return bar.low <= target
