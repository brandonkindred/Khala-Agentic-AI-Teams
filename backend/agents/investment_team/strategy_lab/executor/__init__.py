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
"""

from .trade_builder import build_trade_records

__all__ = ["build_trade_records"]
