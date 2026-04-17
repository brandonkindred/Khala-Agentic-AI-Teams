"""Compatibility shim between the unified :class:`TradingService` and the
orchestrator's legacy sandbox-shaped result.

The strategy-lab orchestrator's refinement loop classifies code-execution
outcomes via ``CodeExecutionResult.{success, error_type, stderr, ...}``.
:func:`run_strategy_code` preserves that interface while routing execution
through :func:`trading_service.modes.backtest.run_backtest`. The shim lives
in the trading-service package (not ``strategy_lab/executor/``) so the
legacy sandbox module can be deleted without leaving a dangling import
location.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ...models import BacktestConfig, StrategySpec, TradeRecord
from .backtest import run_backtest

logger = logging.getLogger(__name__)


@dataclass
class StrategyRunResult:
    """Outcome of executing a strategy's Python code against market data.

    Field names mirror the legacy ``CodeExecutionResult`` where it makes
    sense (``success``, ``error_type``, ``stderr``, ``execution_time_seconds``)
    so callers already keyed off those names don't need refactors. Unlike
    the legacy class, this one carries **finalized** ``TradeRecord``s rather
    than untyped dicts, since ``TradingService`` has already run them
    through ``FillSimulator`` / ``build_trade_records`` is no longer needed.
    """

    success: bool
    trades: List[TradeRecord] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    execution_time_seconds: float = 0.0
    error_type: Optional[str] = None


# Keys here match the legacy sandbox ``error_type`` taxonomy so the
# orchestrator's refinement prompts keep classifying failures the same way.
_LOOKAHEAD_ERROR_TYPE = "lookahead_violation"
_RUNTIME_ERROR_TYPE = "runtime_error"
_OUTPUT_ERROR_TYPE = "output_validation_error"


def run_strategy_code(
    strategy_code: str,
    market_data: Dict,
    config: BacktestConfig,
    *,
    strategy: Optional[StrategySpec] = None,
) -> StrategyRunResult:
    """Execute ``strategy_code`` through :func:`run_backtest`.

    ``strategy`` lets callers pass the full StrategySpec so risk-limits and
    asset-class context flow through; if omitted, a minimal spec is
    synthesised with the provided code. On any sandbox-style error
    (missing code, look-ahead violation, generic crash) the returned
    :class:`StrategyRunResult` has ``success=False`` with an ``error_type``
    the legacy refinement loop recognises.
    """
    start = time.monotonic()

    if strategy is None:
        strategy = StrategySpec(
            strategy_id="_compat_adhoc",
            authored_by="orchestrator",
            asset_class="equity",
            hypothesis="",
            signal_definition="",
            entry_rules=[],
            exit_rules=[],
            strategy_code=strategy_code,
        )
    elif strategy.strategy_code != strategy_code:
        strategy = strategy.model_copy(update={"strategy_code": strategy_code})

    try:
        run = run_backtest(strategy=strategy, config=config, market_data=market_data)
    except ValueError as exc:
        # Typically raised when strategy_code is missing or the market_data
        # arg is ambiguous — surface as a generic runtime error.
        return StrategyRunResult(
            success=False,
            error_type=_RUNTIME_ERROR_TYPE,
            stderr=str(exc)[:2000],
            execution_time_seconds=time.monotonic() - start,
        )

    elapsed = time.monotonic() - start
    service_result = run.service_result

    if service_result.lookahead_violation:
        return StrategyRunResult(
            success=False,
            trades=run.trades,
            error_type=_LOOKAHEAD_ERROR_TYPE,
            stderr=(service_result.error or "")[:2000],
            execution_time_seconds=elapsed,
        )
    if service_result.error:
        # Any surfaced service error — initialisation failure, mid-run
        # strategy crash after earlier fills, harness protocol break —
        # must fail the run. ``TradingService.run`` appends closed trades
        # to ``result.trades`` *before* raising ``StrategyRuntimeError``,
        # so a non-empty trade ledger here indicates *partial* execution,
        # not a successful one. Surface as ``runtime_error`` so the
        # orchestrator's refinement loop can act on it; carry the partial
        # trades through for diagnostic visibility.
        return StrategyRunResult(
            success=False,
            trades=run.trades,
            error_type=_RUNTIME_ERROR_TYPE,
            stderr=service_result.error[:2000],
            execution_time_seconds=elapsed,
        )

    return StrategyRunResult(
        success=True,
        trades=run.trades,
        execution_time_seconds=elapsed,
    )


__all__ = ["StrategyRunResult", "run_strategy_code"]
