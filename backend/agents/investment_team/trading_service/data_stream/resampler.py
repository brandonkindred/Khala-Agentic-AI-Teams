"""Candle resampler — provider-native events → strategy-timeframe BarEvents.

The resampler is the bridge between whatever a provider natively emits
(ticks, 1s/1m aggregates, etc.) and the timeframe the strategy asked for
(``"15m"``, ``"1m"``, ``"1h"``, …). It enforces three invariants critical
to the look-ahead guarantee:

1. **Only finalized bars leave.** A bar for interval ``[t_start, t_end)``
   is emitted strictly *after* a native event with timestamp ``>= t_end``
   arrives. The in-progress bar is never observable.

2. **Monotonic timestamps.** Native events whose timestamp is earlier
   than the last emitted bar close are dropped (and counted) as late
   prints. A bar never carries an earlier close than its predecessor.

3. **No fabricated bars in gaps.** If no native event arrives in a full
   interval, the resampler emits nothing for that interval — a new bar
   is opened when a later event comes in, aligned to its own interval.

See ``system_design/pr2_live_data_and_paper_cutover.md`` §3.4 / §4.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, Union

from pydantic import BaseModel

from ..strategy.contract import Bar
from .protocol import BarEvent

# ---------------------------------------------------------------------------
# Native event types (internal to the data_stream layer).
# ---------------------------------------------------------------------------


class NativeTick(BaseModel):
    """A single trade print from a provider tick feed."""

    timestamp: str  # ISO-8601; may or may not include offset (parsed as UTC if naive)
    symbol: str
    price: float
    size: float = 0.0


class NativeBar(BaseModel):
    """A provider-native finalized candle (e.g. Binance kline_1m)."""

    timestamp: str  # bar **close** time, ISO-8601
    symbol: str
    timeframe: str  # provider-native timeframe label; informational only
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


NativeEvent = Union[NativeTick, NativeBar]


# ---------------------------------------------------------------------------
# Timeframe parsing
# ---------------------------------------------------------------------------


_TIMEFRAME_SECONDS: Dict[str, int] = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "30s": 30,
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def timeframe_to_seconds(tf: str) -> int:
    """Return the duration of ``tf`` in seconds, raising on unknown labels."""
    try:
        return _TIMEFRAME_SECONDS[tf]
    except KeyError as exc:
        raise ValueError(
            f"unknown timeframe {tf!r}; supported: {sorted(_TIMEFRAME_SECONDS)}"
        ) from exc


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp. Naive values are assumed to be UTC."""
    # Python stdlib fromisoformat accepts most ISO strings; trailing 'Z' needs
    # a tiny replacement to stay compatible across 3.10 / 3.11.
    s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_epoch(ts: str) -> float:
    return _parse_iso(ts).timestamp()


def _from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Per-symbol partial-bar state.
# ---------------------------------------------------------------------------


@dataclass
class _PartialBar:
    """Working state for the currently-open target bar for one symbol."""

    bar_start_epoch: float
    bar_end_epoch: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class ResamplerStats:
    """Diagnostic counters; callers may observe these for metrics."""

    out_of_order_dropped: int = 0
    bars_emitted: int = 0
    symbols: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resampler
# ---------------------------------------------------------------------------


class Resampler:
    """Build strategy-timeframe candles from a provider-native event stream.

    Call :meth:`feed_native` for each incoming event and iterate the yielded
    ``BarEvent`` values (zero or more per call). When the upstream feed is
    closing, call :meth:`flush_on_end` to drain state; partial (not-yet-
    finalized) bars are *not* emitted — this is by design.
    """

    def __init__(self, target_timeframe: str) -> None:
        self._target_timeframe = target_timeframe
        self._target_seconds = timeframe_to_seconds(target_timeframe)
        self._partial: Dict[str, _PartialBar] = {}
        # Highest bar-end epoch emitted so far, per symbol. Used to reject
        # late native events — a bar whose close time precedes this watermark
        # would violate monotonicity and is dropped.
        self._watermark: Dict[str, float] = {}
        self.stats = ResamplerStats()

    # ------------------------------------------------------------------

    @property
    def target_timeframe(self) -> str:
        return self._target_timeframe

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def feed_native(self, event: NativeEvent) -> Iterator[BarEvent]:
        """Feed one native event; yield zero-or-more finalized BarEvents."""
        if isinstance(event, NativeTick):
            yield from self._feed_tick(event)
        elif isinstance(event, NativeBar):
            yield from self._feed_native_bar(event)
        else:  # pragma: no cover - defensive
            raise TypeError(f"unknown native event type: {type(event).__name__}")

    def flush_on_end(self) -> Iterator[BarEvent]:
        """Drain state at end-of-stream.

        Partial bars are **not** emitted — they would violate the "only
        finalized bars leave" invariant. This method exists so callers can
        explicitly signal end-of-stream without the resampler guessing.
        """
        # Intentionally empty: no partial bars are emitted.
        return iter(())

    # ------------------------------------------------------------------
    # Internals — tick path
    # ------------------------------------------------------------------

    def _feed_tick(self, tick: NativeTick) -> Iterator[BarEvent]:
        ts_epoch = _to_epoch(tick.timestamp)
        # Late-print guard: if the tick's enclosing bar-end is at or before
        # the watermark for this symbol, drop it.
        bar_start, bar_end = self._interval_for(ts_epoch)
        wm = self._watermark.get(tick.symbol)
        if wm is not None and bar_end <= wm:
            self.stats.out_of_order_dropped += 1
            return

        partial = self._partial.get(tick.symbol)
        if partial is not None and bar_start > partial.bar_start_epoch:
            # A new interval has begun — finalize the old one before opening
            # the new partial.
            yield self._finalize(tick.symbol, partial)
            partial = None

        if partial is None:
            partial = _PartialBar(
                bar_start_epoch=bar_start,
                bar_end_epoch=bar_end,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.size,
            )
            self._partial[tick.symbol] = partial
            return

        # Extend the in-progress bar.
        partial.high = max(partial.high, tick.price)
        partial.low = min(partial.low, tick.price)
        partial.close = tick.price
        partial.volume += tick.size

    # ------------------------------------------------------------------
    # Internals — native-bar path
    # ------------------------------------------------------------------

    def _feed_native_bar(self, nb: NativeBar) -> Iterator[BarEvent]:
        """Upscale provider-native bars into the target timeframe.

        Native timeframe must divide the target evenly. If they are equal,
        the native bar is emitted directly as a ``BarEvent`` with the
        target-timeframe label. If the native timeframe is coarser than the
        target (configuration error), we raise — that would require downscaling
        which is not safe without price-path information.
        """
        native_seconds = timeframe_to_seconds(nb.timeframe)
        if native_seconds > self._target_seconds:
            raise ValueError(
                f"native timeframe {nb.timeframe} is coarser than target "
                f"{self._target_timeframe}; downscaling is unsafe"
            )
        if self._target_seconds % native_seconds != 0:
            raise ValueError(
                f"native timeframe {nb.timeframe} does not evenly divide "
                f"target {self._target_timeframe}"
            )

        # Native bar timestamps are their *close* time; translate to the
        # enclosing interval using the close - 1ns boundary. We use close -
        # half-a-native-interval to land safely inside the bar.
        close_epoch = _to_epoch(nb.timestamp)
        mid_epoch = close_epoch - (native_seconds / 2.0)
        bar_start, bar_end = self._interval_for(mid_epoch)

        wm = self._watermark.get(nb.symbol)
        if wm is not None and bar_end <= wm:
            self.stats.out_of_order_dropped += 1
            return

        partial = self._partial.get(nb.symbol)

        if native_seconds == self._target_seconds:
            # Direct passthrough — emit a target-tf bar at this close time.
            if partial is not None:
                # Flush any in-progress bar if native/target equal (shouldn't
                # normally happen, but keep state consistent).
                yield self._finalize(nb.symbol, partial)
                partial = None
            bar_event = BarEvent(
                bar=Bar(
                    symbol=nb.symbol,
                    timestamp=_from_epoch(bar_end),
                    timeframe=self._target_timeframe,
                    open=nb.open,
                    high=nb.high,
                    low=nb.low,
                    close=nb.close,
                    volume=nb.volume,
                )
            )
            self._watermark[nb.symbol] = bar_end
            self.stats.bars_emitted += 1
            self.stats.symbols[nb.symbol] = self.stats.symbols.get(nb.symbol, 0) + 1
            yield bar_event
            return

        # Upscale: fold this native bar into the target-tf partial.
        if partial is not None and bar_start > partial.bar_start_epoch:
            yield self._finalize(nb.symbol, partial)
            partial = None

        if partial is None:
            partial = _PartialBar(
                bar_start_epoch=bar_start,
                bar_end_epoch=bar_end,
                open=nb.open,
                high=nb.high,
                low=nb.low,
                close=nb.close,
                volume=nb.volume,
            )
            self._partial[nb.symbol] = partial
        else:
            partial.high = max(partial.high, nb.high)
            partial.low = min(partial.low, nb.low)
            partial.close = nb.close
            partial.volume += nb.volume

        # Finalize eagerly if this native bar closed on a target boundary —
        # the next native event will belong to a new interval.
        if abs(close_epoch - partial.bar_end_epoch) < 1e-6:
            yield self._finalize(nb.symbol, partial)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _interval_for(self, ts_epoch: float) -> tuple[float, float]:
        """Return the (bar_start, bar_end) epochs for ``ts_epoch``.

        Alignment is to the Unix epoch — consistent across time zones and
        matches how exchanges align candles (``:00``, ``:15``, ``:30``, …
        in UTC).
        """
        bar_start = (int(ts_epoch) // self._target_seconds) * self._target_seconds
        bar_end = bar_start + self._target_seconds
        return float(bar_start), float(bar_end)

    def _finalize(self, symbol: str, partial: _PartialBar) -> BarEvent:
        """Emit the given partial bar and clear per-symbol state."""
        bar_event = BarEvent(
            bar=Bar(
                symbol=symbol,
                timestamp=_from_epoch(partial.bar_end_epoch),
                timeframe=self._target_timeframe,
                open=partial.open,
                high=partial.high,
                low=partial.low,
                close=partial.close,
                volume=partial.volume,
            )
        )
        self._watermark[symbol] = partial.bar_end_epoch
        # Remove whichever is the current partial for this symbol (it may be
        # a different instance than ``partial`` if the caller already moved on,
        # but typically this clears it).
        cur = self._partial.get(symbol)
        if cur is partial:
            del self._partial[symbol]
        self.stats.bars_emitted += 1
        self.stats.symbols[symbol] = self.stats.symbols.get(symbol, 0) + 1
        return bar_event


__all__ = [
    "NativeBar",
    "NativeEvent",
    "NativeTick",
    "Resampler",
    "ResamplerStats",
    "timeframe_to_seconds",
]
