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
    # Limit offset (fraction of ``stop_price``) and the resolved stop trigger
    # level, populated only for ``style="limit"`` stop-loss intents so
    # ``_build_close_order`` can construct the STOP_LIMIT without re-reading the
    # spec or the position.
    limit_offset_pct: Optional[float] = None
    stop_price: Optional[float] = None


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
                style = getattr(rule, "style", "market") or "market"
                limit_offset_pct = getattr(rule, "limit_offset_pct", None)
                # A limit-style stop needs the resolved trigger level so the
                # dispatcher can rest a STOP_LIMIT there. Limit-style is
                # restricted to ``entry_price`` basis (see ``StopLossRule``),
                # so the level is a static offset off the entry price.
                stop_price = (
                    _stop_loss_level(rule, position)
                    if isinstance(rule, StopLossRule) and style == "limit"
                    else None
                )
                intents.append(
                    ExitIntent(
                        symbol=symbol,
                        rule_kind=_kind_of(rule),
                        rule_index=idx,
                        note=getattr(rule, "note", "") or "",
                        basis=getattr(rule, "basis", None),
                        style=style,
                        limit_offset_pct=limit_offset_pct,
                        stop_price=stop_price,
                    )
                )
                break
    return intents


def _stop_loss_level(rule: StopLossRule, position: PositionState) -> float:
    """Resolve the price level at which ``rule`` floors (long) / caps (short)
    the position, matching :func:`_stop_loss_triggers`'s geometry.

    Preconditions: ``rule`` is side-compatible with ``position`` (the caller only
    resolves a level for a rule that just triggered, so the basis can fire for
    this side).
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
