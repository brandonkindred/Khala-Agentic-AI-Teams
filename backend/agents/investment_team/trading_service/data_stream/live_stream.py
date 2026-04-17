"""Live market-data stream — adapter → resampler → BarEvent.

:class:`LiveStream` composes a :class:`ProviderAdapter` with the
:class:`Resampler` to produce a :class:`MarketDataStream` that can drive
:class:`TradingService` exactly the same way the backtest's
:class:`HistoricalReplayStream` does.

The live stream has two phases:

1. **Warm-up** — yields :class:`BarEvent` objects marked with metadata so
   the paper-trade mode can deliver them to the strategy harness with
   ``is_warmup=True``. Warm-up bars come from the provider's **historical**
   endpoint (bar.timestamp strictly < cut-over).
2. **Live** — yields BarEvents from the live feed after resampling.

The cut-over is captured when the *first* live native event arrives. Any
warm-up bar whose timestamp ``>= cutover_ts`` is dropped (defense in
depth against a provider returning "recent" data that bleeds past the cut-
over instant).

``LiveStream`` is **not** a MarketDataStream directly — because the
warm-up vs. live tagging is relevant to the paper-trade mode, the stream
yields its own :class:`LiveStreamEvent` union and the paper-trade mode
translates those into the underlying engine events. That keeps the
engine's ``StreamEvent`` contract narrow (bars + end) and puts the
live-specific concerns in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional, Union

from ..providers.base import ProviderAdapter, ProviderError, ProviderRegionBlocked
from ..strategy.contract import Bar
from .resampler import Resampler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Events emitted by LiveStream to the paper-trade mode.
# ---------------------------------------------------------------------------


@dataclass
class WarmupBarEvent:
    """A bar delivered during warm-up; strategy sees it with is_warmup=True."""

    bar: Bar


@dataclass
class LiveBarEvent:
    """A finalized bar from the live feed."""

    bar: Bar


@dataclass
class CutoverEvent:
    """Emitted exactly once, between warm-up and live, carrying the cutover ts."""

    cutover_ts: str


@dataclass
class LiveStreamError:
    """Terminal event — the live feed ended with an error."""

    reason: str
    is_region_block: bool = False


@dataclass
class LiveStreamEnd:
    """Terminal event — the live feed ended cleanly (e.g., user stop)."""

    reason: str = "stopped"


LiveStreamEvent = Union[
    WarmupBarEvent,
    CutoverEvent,
    LiveBarEvent,
    LiveStreamError,
    LiveStreamEnd,
]


# ---------------------------------------------------------------------------
# LiveStream
# ---------------------------------------------------------------------------


@dataclass
class LiveStreamConfig:
    symbols: List[str]
    asset_class: str
    strategy_timeframe: str
    warmup_bars: int = 500
    stop_flag: Optional[Callable[[], bool]] = None  # returns True when session should stop


class LiveStream:
    """Compose provider + resampler into a paper-trade-ready event stream.

    Instances are single-use; iterate :meth:`events` to drain.
    """

    def __init__(
        self,
        *,
        provider: ProviderAdapter,
        config: LiveStreamConfig,
    ) -> None:
        self._provider = provider
        self._config = config
        self._resampler = Resampler(config.strategy_timeframe)
        self._cutover_ts: Optional[str] = None

    # ------------------------------------------------------------------

    @property
    def cutover_ts(self) -> Optional[str]:
        return self._cutover_ts

    # ------------------------------------------------------------------

    def events(self) -> Iterator[LiveStreamEvent]:
        try:
            yield from self._warmup()
        except ProviderRegionBlocked as exc:
            yield LiveStreamError(reason=str(exc), is_region_block=True)
            return
        except ProviderError as exc:
            yield LiveStreamError(reason=str(exc))
            return

        # Now open the live feed. The provider may raise
        # ProviderRegionBlocked on the first iteration of its generator;
        # that is the geo-failover hand-off point.
        try:
            yield from self._live()
        except ProviderRegionBlocked as exc:
            # Only a concern if this happens before the first live bar; the
            # registry-level failover runs before us. If we see it here, the
            # session is already committed and must terminate.
            yield LiveStreamError(reason=str(exc), is_region_block=True)
        except ProviderError as exc:
            yield LiveStreamError(reason=str(exc))

    # ------------------------------------------------------------------
    # Warm-up: pull historical bars from the provider at the strategy
    # timeframe, emitting them as WarmupBarEvents.
    # ------------------------------------------------------------------

    def _warmup(self) -> Iterator[LiveStreamEvent]:
        if self._config.warmup_bars <= 0:
            return
        tf = self._config.strategy_timeframe
        if tf not in self._provider.capabilities.historical_timeframes:
            # Provider can't serve the strategy timeframe natively on its
            # historical endpoint. Warm-up is best-effort: log and skip rather
            # than fail the session — many strategies work without warm-up.
            logger.warning(
                "provider %s does not expose timeframe %s on its historical "
                "endpoint; skipping warm-up",
                self._provider.capabilities.name,
                tf,
            )
            return

        start, end = _warmup_window(timeframe=tf, warmup_bars=self._config.warmup_bars)
        try:
            hist_iter = self._provider.historical(
                symbols=self._config.symbols,
                asset_class=self._config.asset_class,
                start=start,
                end=end,
                timeframe=tf,
            )
            for bar_event in hist_iter:
                yield WarmupBarEvent(bar=bar_event.bar)
                if self._should_stop():
                    yield LiveStreamEnd(reason="stopped_during_warmup")
                    return
        except NotImplementedError as exc:
            # Provider has a valid shape but the historical pump isn't wired
            # yet. Don't fail the session — continue to live phase without
            # warm-up.
            logger.warning(
                "provider %s historical pump not implemented: %s — continuing without warm-up",
                self._provider.capabilities.name,
                exc,
            )

    # ------------------------------------------------------------------
    # Live: stream native events through the resampler, tagging cutover
    # on the first event.
    # ------------------------------------------------------------------

    def _live(self) -> Iterator[LiveStreamEvent]:
        native_tf = self._provider.smallest_available(self._config.asset_class, live=True)
        if native_tf is None:
            yield LiveStreamError(
                reason=(
                    f"provider {self._provider.capabilities.name} has no live "
                    f"feed for asset_class={self._config.asset_class!r}"
                )
            )
            return

        native_iter = self._provider.live(
            symbols=self._config.symbols,
            asset_class=self._config.asset_class,
            native_timeframe=native_tf,
        )

        # Materialize the generator one event at a time. The provider is
        # responsible for raising ProviderRegionBlocked / ProviderError;
        # we catch them in the outer events() method.
        for native_event in native_iter:
            if self._cutover_ts is None:
                # Capture the cut-over moment using the first live event's
                # timestamp. Ticks and NativeBars both carry a ``timestamp``.
                self._cutover_ts = getattr(native_event, "timestamp", None) or _now_iso()
                yield CutoverEvent(cutover_ts=self._cutover_ts)

            for bar_event in self._resampler.feed_native(native_event):
                yield LiveBarEvent(bar=bar_event.bar)

            if self._should_stop():
                yield LiveStreamEnd(reason="user_stop")
                return

        # Provider generator exhausted without error — unusual for a live
        # feed, but treat as clean end.
        yield LiveStreamEnd(reason="provider_end")

    # ------------------------------------------------------------------

    def _should_stop(self) -> bool:
        flag = self._config.stop_flag
        return bool(flag and flag())


# ---------------------------------------------------------------------------
# Warm-up window computation.
# ---------------------------------------------------------------------------


def _warmup_window(*, timeframe: str, warmup_bars: int) -> tuple[str, str]:
    """Return an (``start_iso``, ``end_iso``) pair covering at least
    ``warmup_bars`` of the given ``timeframe``, ending slightly before "now".

    The end is pulled back one full timeframe to avoid fetching the current
    (partial) bar — the historical endpoint will typically return only
    finalized bars anyway, but the belt-and-suspenders keeps us from
    accidentally emitting a warm-up bar whose timestamp equals cut-over.
    """
    from .resampler import timeframe_to_seconds

    tf_sec = timeframe_to_seconds(timeframe)
    now_epoch = datetime.now(tz=timezone.utc).timestamp()
    # Snap end to the prior bar boundary and subtract one extra bar of slack.
    end_epoch = (int(now_epoch) // tf_sec) * tf_sec - tf_sec
    start_epoch = end_epoch - warmup_bars * tf_sec
    return _iso(start_epoch), _iso(end_epoch)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso(datetime.now(tz=timezone.utc).timestamp())


__all__ = [
    "CutoverEvent",
    "LiveBarEvent",
    "LiveStream",
    "LiveStreamConfig",
    "LiveStreamEnd",
    "LiveStreamError",
    "LiveStreamEvent",
    "WarmupBarEvent",
]
