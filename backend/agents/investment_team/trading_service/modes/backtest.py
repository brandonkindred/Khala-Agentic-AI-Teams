"""Backtest mode — replays historical bars through the Trading Service.

Two supported data-sourcing paths:

1. **Pre-fetched market data** (legacy): callers pass a
   ``Dict[str, List[OHLCVBar]]`` produced by ``MarketDataService``. Daily
   bars only, unchanged from PR 1.
2. **Provider-driven** (PR 2): callers pass ``(symbols, asset_class)``
   plus an optional ``provider_id`` / ``registry`` override. The function
   resolves a historical provider and pulls data at the requested
   ``timeframe`` — this is the path that unlocks sub-daily backtests
   (e.g. ``"15m"`` via Binance REST klines) without any change to
   ``MarketDataService``.

The two paths share the same event-loop and metric-computation code; the
only branch is where the ``MarketDataStream`` comes from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from ...market_data_service import OHLCVBar
from ...models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from ...trade_simulator import compute_metrics
from ..data_stream.historical_replay import HistoricalReplayStream
from ..data_stream.provider_stream import ProviderHistoricalStream
from ..providers import ProviderRegistry, default_registry
from ..service import TradingService, TradingServiceResult

logger = logging.getLogger(__name__)


@dataclass
class BacktestRunResult:
    result: BacktestResult
    trades: List[TradeRecord]
    service_result: TradingServiceResult


def run_backtest(
    *,
    strategy: StrategySpec,
    config: BacktestConfig,
    market_data: Optional[Dict[str, List[OHLCVBar]]] = None,
    symbols: Optional[List[str]] = None,
    asset_class: Optional[str] = None,
    timeframe: str = "1d",
    provider_id: Optional[str] = None,
    registry: Optional[ProviderRegistry] = None,
) -> BacktestRunResult:
    """Run a backtest for ``strategy``.

    Exactly one data source must be provided:

    * ``market_data`` — pre-fetched dict of symbol→bars (legacy daily path).
    * ``(symbols, asset_class)`` — resolve a historical provider and stream
      bars at ``timeframe``. ``provider_id`` explicitly overrides registry
      selection; ``registry`` defaults to the process-wide registry.

    Raises ``ValueError`` if neither or both data sources are supplied, or
    if ``strategy.strategy_code`` is missing (the LLM-per-bar fallback is
    intentionally gone).
    """
    if not strategy.strategy_code:
        raise ValueError(
            "StrategySpec.strategy_code is required; the LLM-per-bar backtest "
            "path has been removed. Regenerate the strategy via the Strategy "
            "Lab ideation agent."
        )

    has_legacy = market_data is not None
    has_provider = symbols is not None and asset_class is not None
    if has_legacy == has_provider:
        raise ValueError(
            "run_backtest requires exactly one data source: either "
            "'market_data' (pre-fetched) or ('symbols', 'asset_class') "
            "(provider-driven)"
        )

    if has_legacy:
        stream = HistoricalReplayStream(market_data, timeframe=timeframe)
    else:
        reg = registry or default_registry()
        provider = reg.resolve(
            asset_class=asset_class,
            direction="historical",
            explicit=provider_id,
        )
        stream = ProviderHistoricalStream(
            provider=provider,
            symbols=symbols,
            asset_class=asset_class,
            start=config.start_date,
            end=config.end_date,
            timeframe=timeframe,
        )

    service = TradingService(
        strategy_code=strategy.strategy_code,
        config=config,
        risk_limits=strategy.risk_limits,
    )
    service_result = service.run(stream)

    if service_result.error and not service_result.trades:
        logger.warning(
            "backtest for %s ended with error (%s) and no trades",
            strategy.strategy_id,
            service_result.error[:200],
        )

    metrics = compute_metrics(
        service_result.trades,
        config.initial_capital,
        config.start_date,
        config.end_date,
    )
    # Phase 3: propagate the drawdown / look-ahead termination reason from
    # the TradingService layer into the persisted BacktestResult so the API
    # and downstream recording layers can surface it without peeking at the
    # raw service_result.
    if service_result.terminated_reason:
        metrics = metrics.model_copy(update={"terminated_reason": service_result.terminated_reason})
    return BacktestRunResult(
        result=metrics,
        trades=service_result.trades,
        service_result=service_result,
    )
