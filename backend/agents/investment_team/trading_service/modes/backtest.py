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
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

from ...execution.cost_stress import CostStressReport, CostStressRow
from ...execution.data_quality import validate_market_data
from ...market_data_cache import compute_dataset_fingerprint
from ...market_data_cache.streaming import CachingProviderHistoricalStream
from ...market_data_service import OHLCVBar
from ...models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from ...trade_simulator import compute_metrics
from ..data_stream.historical_replay import HistoricalReplayStream
from ..providers import ProviderRegistry, default_registry
from ..service import TradingService, TradingServiceResult
from ..strategy.contract import UnfilledPolicy

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
    as_of: Optional[str] = None,
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

    # Issue #375 — preflight market-data integrity gate.  Strict mode
    # because a backtest is a research artifact: silent corruption is
    # disastrous and a hard failure is recoverable (re-run with the
    # service-level ``warn`` report attached if the caller wants the
    # detail).  Only the legacy pre-fetched path runs the gate today;
    # the provider-driven path streams bars lazily and would require a
    # buffering rewrite to validate up-front (issue #376 introduces a
    # natural integration point via the point-in-time cache).
    quality_report: Optional[object] = None
    if has_legacy:
        quality_report = validate_market_data(
            bars_by_symbol=market_data,
            expected_frequency=timeframe,
            asset_class=asset_class or strategy.asset_class,
            mode="strict",
        )

    # Issue #376 — dataset fingerprint.  Legacy path: hash the
    # pre-fetched dict directly so callers that bypass the cache still
    # get a reproducibility check.  Provider-driven path: a single
    # ``CachingProviderHistoricalStream`` instance is created per run
    # and its ``dataset_fingerprint`` is read after iteration; cost-
    # stress replays reuse the cache hit so they don't refetch.
    legacy_fingerprint: Optional[str] = None
    if has_legacy and market_data:
        legacy_fingerprint = compute_dataset_fingerprint(market_data)

    streaming_holder: Dict[str, Optional[CachingProviderHistoricalStream]] = {"current": None}
    # Stressed-run streams are tracked separately so we can fall back to
    # one of their fingerprints if the baseline exits early (e.g. via
    # the TradingService drawdown breaker) and never drains its
    # generator — without that fallback, ``dataset_fingerprint`` would
    # silently regress to ``None`` for cost-stress runs that did
    # generate fully replayed data.
    stress_streams: List[CachingProviderHistoricalStream] = []

    def _build_stream(*, capture_fingerprint: bool) -> object:
        if has_legacy:
            return HistoricalReplayStream(market_data, timeframe=timeframe)
        reg = registry or default_registry()
        provider = reg.resolve(
            asset_class=asset_class,
            direction="historical",
            explicit=provider_id,
        )
        stream = CachingProviderHistoricalStream(
            provider=provider,
            symbols=symbols,
            asset_class=asset_class,
            start=config.start_date,
            end=config.end_date,
            timeframe=timeframe,
            as_of=as_of,
        )
        # Only the baseline run records its stream into the holder so the
        # post-loop fingerprint read (#376) is a single-writer operation —
        # cost-stress workers run concurrently (#431) and would otherwise
        # race on this slot. Stressed streams are appended to a side list
        # (list.append is atomic under the GIL) so we still have access
        # to their fingerprints if the baseline never drains.
        if capture_fingerprint:
            streaming_holder["current"] = stream
        else:
            stress_streams.append(stream)
        return stream

    def _run_once(
        run_config: BacktestConfig,
        *,
        capture_fingerprint: bool = False,
    ) -> tuple[TradingServiceResult, BacktestResult]:
        stream = _build_stream(capture_fingerprint=capture_fingerprint)
        service = TradingService(
            strategy_code=strategy.strategy_code,
            config=run_config,
            risk_limits=strategy.risk_limits,
            # Backtests prefer requeueing partial-fill remainders on the next
            # bar over silently dropping them — research artifacts must
            # surface the exposure gap. Gated by
            # TRADING_PARTIAL_FILL_DEFAULTS_ENABLED until #386 wires
            # consumption.
            default_unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
        )
        outcome = service.run(stream)
        run_metrics = compute_metrics(
            outcome.trades,
            run_config.initial_capital,
            run_config.start_date,
            run_config.end_date,
            equity_curve=outcome.streaming_equity_curve,
        )
        return outcome, run_metrics

    service_result, metrics = _run_once(config, capture_fingerprint=True)

    if service_result.error and not service_result.trades:
        logger.warning(
            "backtest for %s ended with error (%s) and no trades",
            strategy.strategy_id,
            service_result.error[:200],
        )

    update: Dict[str, object] = {}

    # Phase 3: propagate the drawdown / look-ahead termination reason from
    # the TradingService layer into the persisted BacktestResult so the API
    # and downstream recording layers can surface it without peeking at the
    # raw service_result.
    if service_result.terminated_reason:
        update["terminated_reason"] = service_result.terminated_reason

    # Phase 4: signals-per-bar is computed off the bars the strategy
    # subprocess actually received — the TradingService counts non-warmup
    # bars as it runs, so both the legacy pre-fetched path and the
    # provider-driven path populate the same ``bars_processed`` counter.
    if service_result.bars_processed > 0:
        signals_per_bar = len(service_result.trades) / service_result.bars_processed
        update["signals_per_bar"] = round(signals_per_bar, 6)
        if config.min_signals_per_bar and signals_per_bar < config.min_signals_per_bar:
            update.setdefault("reject_reason", "low_signals_per_bar")

    # Phase 4: cost-stress replay.  Only runs when the flag is on.
    # Issue #431: replays are pure CPU/sim against the cached dataset
    # (#376), so fan them out across a thread pool. Ordering of
    # ``report.rows`` must match ``config.cost_stress_multipliers``
    # regardless of completion order — the ``min_sharpe_at_2x`` gate and
    # downstream consumers rely on it.
    if config.cost_stress and config.cost_stress_multipliers:
        report = CostStressReport()
        multipliers = list(config.cost_stress_multipliers)
        # Index by input position, not multiplier value: callers may pass
        # duplicate multipliers (e.g. for repeatability checks of a
        # non-deterministic strategy), and each duplicate must produce its
        # own row, matching the prior sequential behavior.
        rows_by_index: List[Optional[CostStressRow]] = [None] * len(multipliers)

        def _stress_row(idx: int) -> CostStressRow:
            multiplier = multipliers[idx]
            stress_result, stress_metrics = _run_once(
                _scaled_cost_config(config, multiplier),
                capture_fingerprint=False,
            )
            return CostStressRow(
                multiplier=multiplier,
                sharpe_ratio=stress_metrics.sharpe_ratio,
                annualized_return_pct=stress_metrics.annualized_return_pct,
                max_drawdown_pct=stress_metrics.max_drawdown_pct,
                # Each stressed run generates its own trade ledger —
                # turnover shifts with costs, so use the stressed
                # run's count, not the baseline's.
                trade_count=len(stress_result.trades),
            )

        # Cache-stampede guard: the snapshot cache is only populated when
        # a CachingProviderHistoricalStream drains EndOfStreamEvent (or
        # cache-hits up-front). When the baseline trips early — e.g. via
        # the TradingService drawdown breaker — the cache is still cold,
        # and a parallel fan-out would have every worker miss cache and
        # duplicate the same upstream provider fetch (rate limits,
        # competing snapshot writes). Detect that case via the baseline's
        # fingerprint and run multipliers sequentially until one warms
        # the cache (its stream gets a non-``None`` ``dataset_fingerprint``
        # via either a cache hit or a successful drain), then fan out
        # the remainder. If no multiplier warms the cache, the whole
        # sweep stays sequential — no parallel stampede possible.
        # The legacy ``market_data`` path skips this entirely — it has
        # no provider cache to warm.
        baseline_stream = streaming_holder["current"]
        cache_warm = has_legacy or (
            baseline_stream is not None and baseline_stream.dataset_fingerprint is not None
        )
        parallel_start = 0
        if not cache_warm:
            for idx in range(len(multipliers)):
                rows_by_index[idx] = _stress_row(idx)
                # ``_build_stream`` appends every non-baseline stream to
                # ``stress_streams``; the just-completed run is at the tail.
                if stress_streams and stress_streams[-1].dataset_fingerprint is not None:
                    parallel_start = idx + 1
                    break
            else:
                parallel_start = len(multipliers)

        parallel_indices = list(range(parallel_start, len(multipliers)))
        if parallel_indices:
            workers = min(len(parallel_indices), os.cpu_count() or 4)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cost-stress") as pool:
                futures = {pool.submit(_stress_row, idx): idx for idx in parallel_indices}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    rows_by_index[idx] = fut.result()

        for row in rows_by_index:
            assert row is not None  # every position filled
            report.rows.append(row)
        update["cost_stress_results"] = report.to_payload()

        # Gate: fail when Sharpe at the 2x multiplier drops below the
        # configured floor.  Only fires when the floor was set and a 2x
        # row is present.
        if config.min_sharpe_at_2x is not None:
            row_2x = report.at(2.0)
            if row_2x is not None and row_2x.sharpe_ratio < config.min_sharpe_at_2x:
                update.setdefault("reject_reason", "fails_cost_stress")

    # Issue #375 — surface the preflight report on the result so the API
    # layer and audit log can record exactly what passed (or warned).
    if quality_report is not None:
        update["data_quality_report"] = quality_report.model_dump()

    # Issue #376 — surface the dataset fingerprint for byte-equality
    # checks on rerun.  Streaming path takes precedence over the legacy
    # hash so both routes converge on the same content-addressed key.
    # Issue #431 — if the baseline exited early (drawdown breaker, etc.)
    # and never drained its generator, fall back to any stressed-run
    # stream that did, so reproducibility doesn't regress when cost-
    # stress executions still produced fully replayed data.
    streaming_fp = (
        streaming_holder["current"].dataset_fingerprint
        if streaming_holder["current"] is not None
        else None
    )
    if streaming_fp is None:
        for s in stress_streams:
            if s.dataset_fingerprint is not None:
                streaming_fp = s.dataset_fingerprint
                break
    fingerprint = streaming_fp or legacy_fingerprint
    if fingerprint:
        update["dataset_fingerprint"] = fingerprint

    if update:
        metrics = metrics.model_copy(update=update)

    return BacktestRunResult(
        result=metrics,
        trades=service_result.trades,
        service_result=service_result,
    )


# ---------------------------------------------------------------------------
# Helpers (Phase 4)
# ---------------------------------------------------------------------------


def _scaled_cost_config(base: BacktestConfig, multiplier: float) -> BacktestConfig:
    return base.model_copy(
        update={
            "transaction_cost_bps": base.transaction_cost_bps * multiplier,
            "slippage_bps": base.slippage_bps * multiplier,
            # Avoid recursion: the replayed runs are plain backtests.
            "cost_stress": False,
            "min_signals_per_bar": 0.0,
        }
    )
