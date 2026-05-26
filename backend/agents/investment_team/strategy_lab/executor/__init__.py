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
"""

from .rule_compiler import (
    BarSnapshot,
    ExitIntent,
    PositionState,
    evaluate_exit_rules,
)
from .trade_builder import build_trade_records

__all__ = [
    "BarSnapshot",
    "ExitIntent",
    "PositionState",
    "build_trade_records",
    "evaluate_exit_rules",
]
