"""Executor utilities kept after PR 3.

The legacy ``SandboxRunner`` / ``CodeExecutionResult`` pair has been retired —
strategy code now runs through the unified ``TradingService`` event loop
(see ``trading_service.modes.backtest.run_backtest`` and
``trading_service.modes.sandbox_compat.run_strategy_code``). What's left
here is genuinely shared plumbing:

* :func:`build_trade_records` — converts raw trade dicts to ``TradeRecord``
  objects; still used by legacy test fixtures that predate PR 3.
* ``indicators.py`` — pre-built technical indicators copied into the
  strategy subprocess by the streaming harness.
* :mod:`rule_compiler` — pure-functional evaluator for structured
  ``ExitRule`` discriminated unions (issue #527). The trading service's
  bar loop calls :func:`evaluate_exit_rules` after delivering each bar to
  the strategy and emits any returned ``ExitIntent`` as a close order.
* :mod:`reference_entries` — entry-side replay for the reference-ledger
  simulator (``system_design/reference_ledger_trade_model.md``): reuses
  ``evaluate_entry_rules`` to open a reference position at the next bar's
  open.
* :mod:`reference_exits` — exit-side replay for the same simulator, covering
  all four exit rule kinds. The three resting-order kinds (``StopLossRule``
  across all four basis/style variants, standalone ``TakeProfitRule``, and
  laddered ``ScaledTakeProfitRule``) fill on their own trigger bar — exact
  level on a through-bar, worse open on a gap for a stop. ``SignalExitRule``
  deliberately does not: a bar-close predicate fills at the NEXT bar's open,
  the engine's current and unchanged semantics for that kind. Reuses
  :mod:`rule_compiler`'s trigger geometry and adds the fill mechanics that
  geometry deliberately omits.
"""

from .reference_entries import ReferenceEntryFill, bars_to_frame, replay_entry_rules
from .reference_exits import (
    ReferenceSignalExit,
    ReferenceStopLossExit,
    ReferenceTakeProfitExit,
    entry_price_basis,
    replay_signal_exits,
    replay_stop_loss_exits,
    replay_take_profit_family_exits,
    resolve_signal_exit,
    resolve_stop_loss_exit,
    resolve_take_profit_family_exit,
    scaled_take_profit_rules,
    signal_exit_rules,
    stop_loss_rules_for_side,
    take_profit_rules,
    working_exit_rules,
)
from .rule_compiler import (
    BarSnapshot,
    ExitIntent,
    PositionState,
    StopLimitPrices,
    evaluate_exit_rules,
    stop_limit_prices,
    stop_loss_level,
    stop_loss_triggers,
)
from .trade_builder import build_trade_records

__all__ = [
    "BarSnapshot",
    "ExitIntent",
    "PositionState",
    "ReferenceEntryFill",
    "ReferenceSignalExit",
    "ReferenceStopLossExit",
    "ReferenceTakeProfitExit",
    "StopLimitPrices",
    "bars_to_frame",
    "build_trade_records",
    "entry_price_basis",
    "evaluate_exit_rules",
    "replay_entry_rules",
    "replay_signal_exits",
    "replay_stop_loss_exits",
    "replay_take_profit_family_exits",
    "resolve_signal_exit",
    "resolve_stop_loss_exit",
    "resolve_take_profit_family_exit",
    "scaled_take_profit_rules",
    "signal_exit_rules",
    "stop_limit_prices",
    "stop_loss_level",
    "stop_loss_rules_for_side",
    "stop_loss_triggers",
    "take_profit_rules",
    "working_exit_rules",
]
