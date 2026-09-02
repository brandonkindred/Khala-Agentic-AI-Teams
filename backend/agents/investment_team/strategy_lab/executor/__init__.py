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
* :mod:`reference_exits` — exit-side replay for the same simulator, currently
  covering ``StopLossRule`` across all four basis/style variants with
  resting-order fill semantics (exact level on a through-bar, worse open on a
  gap). Reuses :mod:`rule_compiler`'s trigger geometry and adds the fill
  mechanics that geometry deliberately omits.
"""

from .reference_entries import ReferenceEntryFill, replay_entry_rules
from .reference_exits import (
    ReferenceStopLossExit,
    replay_stop_loss_exits,
    resolve_stop_loss_exit,
    working_exit_rules,
)
from .rule_compiler import (
    BarSnapshot,
    ExitIntent,
    PositionState,
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
    "ReferenceStopLossExit",
    "build_trade_records",
    "evaluate_exit_rules",
    "replay_entry_rules",
    "replay_stop_loss_exits",
    "resolve_stop_loss_exit",
    "stop_limit_prices",
    "stop_loss_level",
    "stop_loss_triggers",
    "working_exit_rules",
]
