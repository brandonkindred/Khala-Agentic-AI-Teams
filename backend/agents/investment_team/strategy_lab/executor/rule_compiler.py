"""Pure-functional evaluator for structured ``ExitRule`` discriminated unions.

Issue #527 — the executor's bar loop owns enforcement of structured exit
rules (``TimeStopRule`` / ``StopLossRule`` / ``TakeProfitRule``). Strategy
code authors only the entry/signal logic; the engine reads the spec's
``exit_rules`` and emits close orders when a rule fires.

This module is intentionally side-effect free: it takes the current per-
position state and the current bar, and returns a list of ``ExitIntent``
records. The caller (``TradingService``) is responsible for translating
each intent into an actual close order on the order book.

``SignalExitRule`` is deferred to a follow-up — the engine does not have a
shared per-bar indicator runtime today (the streaming harness computes
indicators inside the strategy subprocess, not the parent), so evaluating
``Predicate`` would require new plumbing out of scope for the MVP. The
evaluator treats it as a silent no-op (``False`` from ``_rule_triggers``)
so a spec mixing ``SignalExitRule`` with other rules still gets the
other rules enforced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from ..spec_dsl import (
    ExitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
)

ExitRuleKind = Literal["time_stop", "stop_loss", "take_profit", "signal_exit"]


@dataclass(frozen=True)
class PositionState:
    """Snapshot of one open position, as seen by the rule evaluator."""

    symbol: str
    side: Literal["long", "short"]
    qty: float
    entry_price: float
    bars_held: int
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
) -> list[ExitIntent]:
    """Return one ``ExitIntent`` per (open position × first triggered rule).

    Order semantics:
      * Iterate rules in spec order; the first rule that fires for a given
        position wins. Subsequent rules for the same position are skipped
        on the same bar (only one close per position per bar).
      * Positions with no open qty (or missing from ``bars``) are skipped.
      * ``SignalExitRule`` is a silent no-op (returns ``False`` from
        ``_rule_triggers``) — see module docstring for rationale.
    """
    intents: list[ExitIntent] = []
    for symbol, position in positions.items():
        if position.qty <= 0:
            continue
        bar = bars.get(symbol)
        if bar is None:
            continue
        for idx, rule in enumerate(rules):
            if _rule_triggers(rule, position, bar):
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
    if isinstance(rule, TimeStopRule):
        return "time_stop"
    if isinstance(rule, StopLossRule):
        return "stop_loss"
    if isinstance(rule, TakeProfitRule):
        return "take_profit"
    if isinstance(rule, SignalExitRule):
        return "signal_exit"
    raise TypeError(f"unknown ExitRule subclass: {type(rule).__name__}")


def _rule_triggers(rule: ExitRule, position: PositionState, bar: BarSnapshot) -> bool:
    if isinstance(rule, TimeStopRule):
        # ``n_bars`` counts inclusively from the entry bar — ``n_bars=1`` means
        # "close on the entry bar itself", ``n_bars=10`` means close once the
        # position has been held for at least 10 bars (entry + 9 subsequent).
        return position.bars_held >= rule.n_bars

    if isinstance(rule, StopLossRule):
        return _stop_loss_triggers(rule, position, bar)

    if isinstance(rule, TakeProfitRule):
        return _take_profit_triggers(rule, position, bar)

    if isinstance(rule, SignalExitRule):
        # Predicate-based exits need a shared per-bar indicator runtime in
        # the parent engine — out of scope for the issue #527 MVP. Treat as
        # a no-op so the evaluator can still process surrounding rules; the
        # conformance gate flags SignalExit presence in an info row. Callers
        # who need explicit detection should check ``isinstance`` before
        # calling :func:`evaluate_exit_rules`.
        return False

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
