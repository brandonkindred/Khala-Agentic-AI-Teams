"""Paper-trade mode — drives :class:`TradingService` against a LiveStream.

Public entrypoint: :func:`run_paper_trade`. The mode:

1. Resolves a live provider via the registry (with Binance → Coinbase
   geo-failover at session open for crypto).
2. Builds a :class:`LiveStream` that warms up from history, then streams
   live bars.
3. Feeds the resulting event stream through the same
   :class:`TradingService` used by backtests, setting ``is_warmup`` on
   warm-up bars so the service suppresses fills for them.
4. Enforces termination: ≥ ``min_fills`` OR user stop OR wall-clock
   guard OR provider error.
5. Returns a :class:`PaperTradeRunResult` that the API layer wraps into a
   :class:`PaperTradingSession`.

See ``system_design/pr2_live_data_and_paper_cutover.md`` §5.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, Iterator, List, Optional

from ...execution.data_quality import LiveGapMonitor, validate_market_data
from ...market_data_cache import compute_dataset_fingerprint, get_default_cache
from ...market_data_service import OHLCVBar
from ...models import BacktestConfig, StrategySpec, TradeRecord
from ..data_stream.live_stream import (
    CutoverEvent,
    LiveBarEvent,
    LiveStream,
    LiveStreamConfig,
    LiveStreamEnd,
    LiveStreamError,
    LiveStreamEvent,
    WarmupBarEvent,
)
from ..data_stream.protocol import BarEvent, EndOfStreamEvent, StreamEvent
from ..providers import (
    ProviderRegionBlocked,
    ProviderRegistry,
    canonical_asset_class,
    default_registry,
)
from ..service import TradingService, TradingServiceResult
from ..strategy.contract import UnfilledPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + result
# ---------------------------------------------------------------------------


@dataclass
class PaperTradeConfig:
    """Paper-mode-specific knobs layered on top of :class:`BacktestConfig`."""

    symbols: List[str]
    asset_class: str
    strategy_timeframe: str
    min_fills: int = 20
    max_hours: float = 72.0
    warmup_bars: int = 500
    provider_id: Optional[str] = None  # explicit registry override


@dataclass
class PaperTradeRunResult:
    trades: List[TradeRecord]
    service_result: TradingServiceResult
    provider_id: str
    cutover_ts: Optional[str]
    fill_count: int
    terminated_reason: str
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    # Issue #375 — preflight data-quality report captured at warm-up
    # cut-over (``validate_market_data(mode='warn')``).  None when no
    # warm-up bars were observed before the stream ended.
    data_quality_report: Optional[dict] = None
    # Issue #376 — content-addressed fingerprint of the warm-up snapshot,
    # captured at cut-over.  Live bars are not cached, so this hashes
    # only the historical warm-up window.  None when the stream ended
    # before cut-over.
    dataset_fingerprint: Optional[str] = None


# ---------------------------------------------------------------------------
# Stop controller (shared with API layer to wire up POST /stop)
# ---------------------------------------------------------------------------


class StopController:
    """Thread-safe flag read by :class:`LiveStream`'s stop hook.

    The API's ``POST /strategy-lab/paper-trade/{session_id}/stop`` endpoint
    sets the flag; the running session inspects it between bars and ends
    the iterator cleanly. Idempotent.
    """

    def __init__(self) -> None:
        self._ev = threading.Event()

    def request_stop(self) -> None:
        self._ev.set()

    def is_stopped(self) -> bool:
        return self._ev.is_set()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_paper_trade(
    *,
    strategy: StrategySpec,
    backtest_config: BacktestConfig,
    paper_config: PaperTradeConfig,
    stop_controller: Optional[StopController] = None,
    registry: Optional[ProviderRegistry] = None,
    clock: Callable[[], float] = time.time,
) -> PaperTradeRunResult:
    """Run a paper-trading session until termination.

    ``strategy.strategy_code`` must be present (same rule as backtests —
    the LLM-per-bar fallback is gone).
    """
    if not strategy.strategy_code:
        raise ValueError(
            "StrategySpec.strategy_code is required; regenerate the strategy "
            "via the Strategy Lab ideation agent"
        )

    reg = registry or default_registry()

    # Normalise the asset_class so every downstream component — the
    # registry, LiveStream, and the adapter's ``smallest_available`` call —
    # sees the same canonical label. Strategy-Lab specs commonly use
    # "stocks" / "forex" whereas providers declare ``supports={"equities",
    # "fx"}``; without this step non-crypto paper sessions would either
    # resolve to nothing (``LookupError`` → ``no_provider``) or resolve
    # but then have the adapter return ``None`` from ``smallest_available``.
    canonical_cls = canonical_asset_class(paper_config.asset_class)
    if canonical_cls != paper_config.asset_class:
        paper_config = replace(paper_config, asset_class=canonical_cls)

    # ------------------------------------------------------------------
    # Resolve provider (with crypto geo-failover at session open).
    # ------------------------------------------------------------------
    try:
        resolution = reg.resolve_live(
            asset_class=paper_config.asset_class,
            explicit=paper_config.provider_id,
        )
    except LookupError as exc:
        return PaperTradeRunResult(
            trades=[],
            service_result=TradingServiceResult(),
            provider_id="",
            cutover_ts=None,
            fill_count=0,
            terminated_reason="no_provider",
            error=str(exc),
        )

    provider = resolution.primary
    provider_id = resolution.primary_name

    # ------------------------------------------------------------------
    # Run — wrapped in a fill-count + wall-clock termination guard.
    # ------------------------------------------------------------------
    warnings: List[str] = []
    if paper_config.min_fills < 20:
        warnings.append("min_fills_below_recommended")

    controller = stop_controller or StopController()
    start_wall = clock()
    deadline = start_wall + paper_config.max_hours * 3600.0

    fill_counter = _FillCounter()
    cutover_seen: dict = {"ts": None}
    terminated_reason = {"reason": "unknown"}
    # Issue #375 — captures the preflight report at warm-up cut-over
    # (mode='warn').  Stays None if the stream ends before cut-over.
    # Issue #376 — ``fingerprint`` is filled at the same cut-over from
    # the buffered warm-up bars.
    quality_state: dict = {"report": None, "fingerprint": None}

    def _should_stop() -> bool:
        if controller.is_stopped():
            terminated_reason["reason"] = "user_stop"
            return True
        if fill_counter.count >= paper_config.min_fills:
            terminated_reason["reason"] = "fill_target_reached"
            return True
        if clock() >= deadline:
            terminated_reason["reason"] = "max_hours"
            return True
        return False

    def _build_live_stream(provider_adapter) -> LiveStream:
        return LiveStream(
            provider=provider_adapter,
            config=LiveStreamConfig(
                symbols=paper_config.symbols,
                asset_class=paper_config.asset_class,
                strategy_timeframe=paper_config.strategy_timeframe,
                warmup_bars=paper_config.warmup_bars,
                stop_flag=_should_stop,
            ),
        )

    service = TradingService(
        strategy_code=strategy.strategy_code,
        config=backtest_config,
        risk_limits=strategy.risk_limits,
        # Paper trading mirrors live exchange behavior — when the
        # participation cap clips an order, the unfilled remainder is
        # dropped, not requeued. Explicit at the call site to document
        # divergence from backtest mode. Gated by
        # TRADING_PARTIAL_FILL_DEFAULTS_ENABLED until #386 wires
        # consumption.
        default_unfilled_policy=UnfilledPolicy.DROP,
    )

    # First attempt: primary provider.
    try:
        stream_source = _translate(
            _build_live_stream(provider).events(),
            fill_counter=fill_counter,
            cutover_seen=cutover_seen,
            terminated_reason=terminated_reason,
            paper_config=paper_config,
            warnings=warnings,
            quality_state=quality_state,
            provider_id=provider_id,
        )
        service_result = service.run(
            stream_source, on_trade=lambda _trade: fill_counter.increment()
        )
    except ProviderRegionBlocked as exc:
        # Geo-failover: try secondary if one was resolved.
        if resolution.fallback is None:
            return PaperTradeRunResult(
                trades=[],
                service_result=TradingServiceResult(),
                provider_id=provider_id,
                cutover_ts=None,
                fill_count=0,
                terminated_reason="region_blocked",
                error=str(exc),
                warnings=warnings,
                data_quality_report=quality_state["report"],
                dataset_fingerprint=quality_state["fingerprint"],
            )
        logger.info(
            "primary provider %s region-blocked; failing over to %s",
            provider_id,
            resolution.fallback_name,
        )
        provider = resolution.fallback
        provider_id = resolution.fallback_name or "unknown"
        stream_source = _translate(
            _build_live_stream(provider).events(),
            fill_counter=fill_counter,
            cutover_seen=cutover_seen,
            terminated_reason=terminated_reason,
            paper_config=paper_config,
            warnings=warnings,
            quality_state=quality_state,
            provider_id=provider_id,
        )
        service_result = service.run(
            stream_source, on_trade=lambda _trade: fill_counter.increment()
        )

    # ------------------------------------------------------------------
    # Determine final termination reason.
    # ------------------------------------------------------------------
    if service_result.lookahead_violation:
        final_reason = "lookahead_violation"
    elif service_result.error:
        final_reason = "provider_error"
    elif service_result.terminated_reason and service_result.terminated_reason.startswith(
        "max_drawdown"
    ):
        final_reason = "max_drawdown"
    elif terminated_reason["reason"] != "unknown":
        final_reason = terminated_reason["reason"]
    else:
        final_reason = "provider_end"

    return PaperTradeRunResult(
        trades=service_result.trades,
        service_result=service_result,
        provider_id=provider_id,
        cutover_ts=cutover_seen["ts"],
        fill_count=fill_counter.count,
        terminated_reason=final_reason,
        warnings=warnings,
        error=service_result.error,
        data_quality_report=quality_state["report"],
        dataset_fingerprint=quality_state["fingerprint"],
    )


# ---------------------------------------------------------------------------
# Event translation: LiveStreamEvent → StreamEvent
# ---------------------------------------------------------------------------


class _FillCounter:
    """Side-channel counter updated by the TradingService's ``on_trade`` hook.

    The paper-trade mode reads this inside its ``_should_stop`` closure so
    termination (``min_fills``) doesn't require the service to know about
    paper-mode concerns.
    """

    def __init__(self) -> None:
        self._count = 0

    def increment(self) -> None:
        self._count += 1

    @property
    def count(self) -> int:
        return self._count


def _translate(
    live_events: Iterator[LiveStreamEvent],
    *,
    fill_counter: _FillCounter,
    cutover_seen: dict,
    terminated_reason: dict,
    paper_config: PaperTradeConfig,
    warnings: List[str],
    quality_state: dict,
    provider_id: str = "",
) -> Iterator[StreamEvent]:
    """Convert :class:`LiveStreamEvent` to :class:`StreamEvent` the engine understands.

    Issue #375 — also runs the preflight data-quality gate at warm-up
    cut-over (``mode='warn'``) and a per-symbol live-gap monitor on
    streaming bars; advisories accumulate on ``warnings`` and the warm-up
    report ends up on ``quality_state['report']``.

    Issue #376 — at the same cut-over, hashes the buffered warm-up bars
    into a content-addressed fingerprint and persists each per-symbol
    warm-up window as a cache snapshot.  The fingerprint lands on
    ``quality_state['fingerprint']``; live bars are not cached.
    """
    # Warm-up bar buffer keyed by symbol; flushed at cutover.
    warmup_buffer: Dict[str, List[OHLCVBar]] = {}
    gap_monitor = LiveGapMonitor(bar_frequency=paper_config.strategy_timeframe)

    for event in live_events:
        if isinstance(event, WarmupBarEvent):
            warmup_buffer.setdefault(event.bar.symbol, []).append(_bar_to_ohlcv(event.bar))
            yield BarEvent(bar=event.bar, is_warmup=True)
        elif isinstance(event, CutoverEvent):
            cutover_seen["ts"] = event.cutover_ts
            # Issue #375 — validate the warm-up window now that we have
            # the full set of historical bars.  ``mode='warn'`` because
            # paper trading must not crash on transient feed issues —
            # callers see the report on ``warnings`` and the structured
            # report attached to the session.
            if warmup_buffer:
                report = validate_market_data(
                    bars_by_symbol=warmup_buffer,
                    expected_frequency=paper_config.strategy_timeframe,
                    asset_class=paper_config.asset_class,
                    mode="warn",
                )
                quality_state["report"] = report.model_dump()
                if report.severity != "ok":
                    warnings.append(f"data_quality:warmup:{report.severity}")
                # Issue #376 — fingerprint the warm-up window and persist
                # a per-symbol cache snapshot, then surface the
                # fingerprint to the caller.  Cache writes are best-
                # effort; failures are logged but do not abort the
                # session.
                quality_state["fingerprint"] = compute_dataset_fingerprint(warmup_buffer)
                try:
                    cache = get_default_cache()
                    for sym, sym_bars in warmup_buffer.items():
                        if not sym_bars:
                            continue
                        cache.record_bars_snapshot(
                            symbol=sym,
                            asset_class=paper_config.asset_class,
                            frequency=paper_config.strategy_timeframe,
                            provider=provider_id or "live",
                            bars=sym_bars,
                            start=sym_bars[0].date,
                            end=sym_bars[-1].date,
                        )
                except Exception:
                    logger.exception("warm-up snapshot persistence failed")
            # No StreamEvent to emit — the service doesn't care about the
            # cut-over, only whether a bar is marked warm-up or not.
        elif isinstance(event, LiveBarEvent):
            # Defense in depth: live bars must have a timestamp >= cutover.
            ts = event.bar.timestamp
            if cutover_seen["ts"] is not None and ts < cutover_seen["ts"]:
                logger.warning(
                    "dropping live bar with timestamp %s < cutover %s",
                    ts,
                    cutover_seen["ts"],
                )
                continue
            # Issue #375 — per-symbol live-gap monitor.  Emits a single
            # warning whenever consecutive bars are >5x the expected
            # frequency apart.
            warning = gap_monitor.observe(event.bar.symbol, ts)
            if warning is not None and warning not in warnings:
                warnings.append(warning)
            yield BarEvent(bar=event.bar, is_warmup=False)
        elif isinstance(event, LiveStreamEnd):
            # Preserve the reason the _should_stop closure already recorded;
            # LiveStream only knows a generic "stopped"-style label.
            if terminated_reason["reason"] == "unknown":
                terminated_reason["reason"] = event.reason
            yield EndOfStreamEvent(reason=event.reason)
            return
        elif isinstance(event, LiveStreamError):
            if event.is_region_block:
                raise ProviderRegionBlocked(event.reason)
            terminated_reason["reason"] = "provider_error"
            yield EndOfStreamEvent(reason="provider_error")
            return

    # Upstream iterator exhausted without a terminal event.
    yield EndOfStreamEvent(reason="upstream_end")


def _bar_to_ohlcv(bar) -> OHLCVBar:
    """Convert a :class:`Bar` (live-stream model) to :class:`OHLCVBar`.

    The two share the same fields modulo the timestamp / date naming;
    keeping a thin adapter here avoids leaking ``OHLCVBar`` semantics
    into the live-stream protocol module.
    """
    return OHLCVBar(
        date=bar.timestamp,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


__all__ = [
    "PaperTradeConfig",
    "PaperTradeRunResult",
    "StopController",
    "run_paper_trade",
]
