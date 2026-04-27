"""Cache-aware wrapper around ``ProviderHistoricalStream`` (issue #376).

Wraps the lazy provider stream so that a backtest's
``provider-driven`` data path participates in the same content-addressed
cache as ``MarketDataService.fetch_*`` does for the legacy pre-fetched
path.

Two responsibilities:

* On ``__iter__`` start, attempt a multi-symbol cache hit.  When every
  requested symbol has a snapshot covering ``[start, end]`` with the
  same provider, replay from Parquet and skip the upstream provider
  entirely.  Otherwise fall through to the wrapped stream.
* On the live (miss) path, tee each ``BarEvent`` into per-symbol
  buffers; when ``EndOfStreamEvent`` arrives, persist a snapshot per
  symbol.  Compute and expose ``dataset_fingerprint`` once the iterator
  has been fully drained.

The wrapper preserves the original lazy semantics of
``ProviderHistoricalStream``: bars are still yielded one at a time and
nothing is buffered in memory beyond the per-symbol snapshot list,
which matches what the provider would have produced anyway.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterator, List, Optional

from ..market_data_service import OHLCVBar
from ..trading_service.data_stream.protocol import BarEvent, EndOfStreamEvent, StreamEvent
from ..trading_service.data_stream.provider_stream import ProviderHistoricalStream
from ..trading_service.providers.base import ProviderAdapter
from .store import (
    MarketDataCache,
    SnapshotMeta,
    compute_dataset_fingerprint,
    get_default_cache,
)

logger = logging.getLogger(__name__)


class CachingProviderHistoricalStream:
    """Drop-in replacement for ``ProviderHistoricalStream`` that caches.

    Same constructor signature so callers can swap one for the other.
    The wrapper is single-shot — iterate it once per backtest.  After
    iteration, ``dataset_fingerprint`` is populated whether the data
    came from the cache or from the provider.
    """

    def __init__(
        self,
        *,
        provider: ProviderAdapter,
        symbols: List[str],
        asset_class: str,
        start: str,
        end: str,
        timeframe: str,
        cache: Optional[MarketDataCache] = None,
        as_of: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._symbols = list(symbols)
        self._asset_class = asset_class
        self._start = start
        self._end = end
        self._timeframe = timeframe
        self._cache = cache or get_default_cache()
        self._as_of = as_of
        self._dataset_fingerprint: Optional[str] = None
        self._snapshots: Dict[str, SnapshotMeta] = {}
        self._cache_hit: bool = False

    @property
    def dataset_fingerprint(self) -> Optional[str]:
        return self._dataset_fingerprint

    @property
    def snapshots(self) -> Dict[str, SnapshotMeta]:
        return dict(self._snapshots)

    @property
    def cache_hit(self) -> bool:
        return self._cache_hit

    def __iter__(self) -> Iterator[StreamEvent]:
        cached = self._try_full_cache_replay()
        if cached is not None:
            self._cache_hit = True
            yield from cached
            return

        # Miss path — tee provider bars into per-symbol buffers and
        # persist snapshots on EOS.
        provider_name = getattr(getattr(self._provider, "capabilities", None), "name", "")
        if not provider_name:
            provider_name = self._provider.__class__.__name__.lower()

        buffers: Dict[str, List[OHLCVBar]] = {sym: [] for sym in self._symbols}

        upstream = ProviderHistoricalStream(
            provider=self._provider,
            symbols=self._symbols,
            asset_class=self._asset_class,
            start=self._start,
            end=self._end,
            timeframe=self._timeframe,
        )

        for event in upstream:
            if isinstance(event, BarEvent):
                bar = event.bar
                buffers.setdefault(bar.symbol, []).append(_bar_to_ohlcv(bar))
                yield event
            elif isinstance(event, EndOfStreamEvent):
                self._persist_buffers(buffers, provider_name)
                yield event
                return
            else:  # pragma: no cover - defensive
                yield event

        # Upstream exhausted without an explicit EOS — still persist what
        # we got and finalize the fingerprint.
        self._persist_buffers(buffers, provider_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _try_full_cache_replay(self) -> Optional[List[StreamEvent]]:
        """If every requested symbol has a covering snapshot, replay it.

        Returns the ordered ``StreamEvent`` list to yield, or ``None``
        when any symbol misses (the caller falls through to the live
        provider).  We require all-symbols-hit because partial replay
        risks merging bars from inconsistent fetch dates.
        """
        if not self._symbols:
            return None
        per_symbol_bars: Dict[str, List[OHLCVBar]] = {}
        snapshots: Dict[str, SnapshotMeta] = {}
        for sym in self._symbols:
            meta = self._cache.lookup_snapshot(
                symbol=sym,
                asset_class=self._asset_class,
                frequency=self._timeframe,
                start=self._start,
                end=self._end,
                as_of=self._as_of,
            )
            if meta is None:
                return None
            bars = self._cache.read_snapshot(meta)
            if bars is None:
                return None
            trimmed = [b for b in bars if self._start <= b.date <= self._end]
            per_symbol_bars[sym] = trimmed
            snapshots[sym] = meta

        # Build a chronologically-ordered BarEvent stream from the
        # cached bars.  Across symbols this matches the engine's
        # expectation: the upstream provider already interleaved by
        # timestamp, and ``HistoricalReplayStream`` would too.
        events = _interleave_bars(per_symbol_bars)
        events.append(EndOfStreamEvent())
        self._snapshots = snapshots
        self._dataset_fingerprint = compute_dataset_fingerprint(per_symbol_bars)
        return events

    def _persist_buffers(
        self,
        buffers: Dict[str, List[OHLCVBar]],
        provider_name: str,
    ) -> None:
        nonempty: Dict[str, List[OHLCVBar]] = {}
        for sym, bars in buffers.items():
            if not bars:
                continue
            meta = self._cache.record_bars_snapshot(
                symbol=sym,
                asset_class=self._asset_class,
                frequency=self._timeframe,
                provider=provider_name,
                bars=bars,
                start=self._start,
                end=self._end,
            )
            if meta is not None:
                self._snapshots[sym] = meta
                nonempty[sym] = bars
        if nonempty:
            self._dataset_fingerprint = compute_dataset_fingerprint(nonempty)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar_to_ohlcv(bar) -> OHLCVBar:
    """Translate a ``trading_service.strategy.contract.Bar`` to ``OHLCVBar``.

    The two share the same OHLCV fields modulo the timestamp / date
    naming convention; bar.timestamp is an ISO-formatted string.
    """
    return OHLCVBar(
        date=str(bar.timestamp),
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(getattr(bar, "volume", 0.0)),
    )


def _interleave_bars(per_symbol: Dict[str, List[OHLCVBar]]) -> List[StreamEvent]:
    """Round-robin per-symbol bars in chronological order.

    Reconstructs the ordering a provider's historical endpoint would
    have produced.  Within a tied timestamp, ordering follows symbol
    name to keep the replay deterministic.
    """
    from ..trading_service.strategy.contract import Bar  # local import to avoid cycle

    flat: List[tuple[str, str, OHLCVBar]] = []
    for symbol, bars in per_symbol.items():
        for bar in bars:
            flat.append((bar.date, symbol, bar))
    flat.sort(key=lambda t: (t[0], t[1]))

    events: List[StreamEvent] = []
    for ts, symbol, bar in flat:
        events.append(
            BarEvent(
                bar=Bar(
                    symbol=symbol,
                    timestamp=ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                ),
            )
        )
    return events


__all__ = ["CachingProviderHistoricalStream"]
