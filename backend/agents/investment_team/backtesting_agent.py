"""
Backtesting Agent — runs historical backtests using real market data and LLM-driven trade decisions.

Delegates the bar-walking simulation to :class:`TradeSimulationEngine` and provides
the LLM evaluation callback with backtest-specific prompt context.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from llm_service.interface import LLMClient

from .market_data_service import OHLCVBar
from .models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from .trade_simulator import (
    OpenPosition,
    TradeSimulationEngine,
    compute_metrics,
    evaluate_bar,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_EVALUATE_SYSTEM = (
    "You are an expert quantitative trader backtesting a swing trading strategy against historical data. "
    "You evaluate daily price bars against the strategy's entry and exit rules to decide whether to trade. "
    "Be disciplined — only trade when the rules are clearly met by the price data. "
    "Remember: this is a backtest, so you must only use information available on the current bar's date."
)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class BacktestingAgent:
    """
    Runs backtests against real historical market data by walking through OHLCV bars
    chronologically and using the LLM to interpret strategy entry/exit rules.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm = llm_client

    def run_backtest(
        self,
        strategy: StrategySpec,
        config: BacktestConfig,
        market_data: Dict[str, List[OHLCVBar]],
    ) -> tuple[BacktestResult, List[TradeRecord]]:
        """
        Walk through historical market data bar-by-bar, making LLM-driven trade decisions.

        Returns (BacktestResult, trade_ledger).
        """
        engine = TradeSimulationEngine(
            initial_capital=config.initial_capital,
            transaction_cost_bps=config.transaction_cost_bps,
            slippage_bps=config.slippage_bps,
        )

        def _evaluate(
            symbol: str,
            bar: OHLCVBar,
            recent: List[OHLCVBar],
            position: Optional[OpenPosition],
            capital: float,
        ) -> Dict[str, Any]:
            return evaluate_bar(
                self.llm.complete_json,
                strategy,
                _EVALUATE_SYSTEM,
                symbol,
                bar,
                recent,
                position,
                capital,
            )

        sim = engine.run(market_data, _evaluate)

        result = compute_metrics(
            sim.trades, config.initial_capital, config.start_date, config.end_date
        )
        return result, sim.trades
