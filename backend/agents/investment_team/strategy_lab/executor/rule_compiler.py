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

Bar-counting "time stops" are deliberately absent: real traders close
on price action, P&L, or signal reversal — not on an arbitrary "Nth
bar held" counter.

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


def evaluate_exit_rules(
    rules: Sequence[ExitRule],
    positions: Mapping[str, PositionState],
    bars: Mapping[str, BarSnapshot],
    *,
    views: Optional[Mapping[str, HistoryView]] = None,
) -> list[ExitIntent]:
    """Return one ``ExitIntent`` per (open position × first triggered rule).

    Order semantics:
      * Iterate rules in spec order; the first rule that fires for a given
        position wins. Subsequent rules for the same position are skipped
        on the same bar (only one close per position per bar).
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
                intents.append(
                    ExitIntent(
                        symbol=symbol,
                        rule_kind=_kind_of(rule),
                        rule_index=idx,
                        note=getattr(rule, "note", "") or "",
                    )
                )
                break
    return intents


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
    pct = rule.pct
    if position.side == "long":
        if rule.basis == "entry_price":
            floor = position.entry_price * (1.0 - pct)
        elif rule.basis == "trailing_high":
            floor = position.high_since_entry * (1.0 - pct)
        else:
            # ``trailing_low`` only makes sense for shorts; treated as no-op
            # for longs rather than firing, so a misconfigured spec doesn't
            # silently flush every long position on bar 1.
            return False
        return bar.low <= floor

    # short
    if rule.basis == "entry_price":
        ceiling = position.entry_price * (1.0 + pct)
    elif rule.basis == "trailing_low":
        ceiling = position.low_since_entry * (1.0 + pct)
    else:
        # ``trailing_high`` is the long-side counterpart; no-op for shorts.
        return False
    return bar.high >= ceiling


def _take_profit_triggers(rule: TakeProfitRule, position: PositionState, bar: BarSnapshot) -> bool:
    pct = rule.pct
    if position.side == "long":
        target = position.entry_price * (1.0 + pct)
        return bar.high >= target
    target = position.entry_price * (1.0 - pct)
    return bar.low <= target
