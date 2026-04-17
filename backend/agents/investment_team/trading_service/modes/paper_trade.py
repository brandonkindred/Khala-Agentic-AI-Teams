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
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional

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
from ..providers import ProviderRegionBlocked, ProviderRegistry, default_registry
from ..service import TradingService, TradingServiceResult

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
    )

    # First attempt: primary provider.
    try:
        stream_source = _translate(
            _build_live_stream(provider).events(),
            fill_counter=fill_counter,
            cutover_seen=cutover_seen,
            terminated_reason=terminated_reason,
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
) -> Iterator[StreamEvent]:
    """Convert :class:`LiveStreamEvent` to :class:`StreamEvent` the engine understands."""
    for event in live_events:
        if isinstance(event, WarmupBarEvent):
            yield BarEvent(bar=event.bar, is_warmup=True)
        elif isinstance(event, CutoverEvent):
            cutover_seen["ts"] = event.cutover_ts
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


__all__ = [
    "PaperTradeConfig",
    "PaperTradeRunResult",
    "StopController",
    "run_paper_trade",
]
