"""Targeted tests for ``LiveStream._live`` — the live native-event pump.

These tests exercise the live pump in isolation by feeding ``LiveStream``
a stub provider that yields a scripted sequence of native bars. No real
provider, websocket, or asyncio scheduler is involved; every test
finishes in milliseconds because the provider's ``live()`` returns a
plain generator.

``_live()`` is the runtime path for every live session after warm-up
(``events()`` always delegates here once warm-up finishes), so the
branches covered here are:

* one or more native events emitted → cutover captured + LiveBarEvent
* ``smallest_available(...) is None`` → LiveStreamError + early return
* stop-flag set → LiveStreamEnd("user_stop") mid-iteration
* provider generator exhausted → LiveStreamEnd("provider_end")
* provider raises ``ProviderRegionBlocked`` inside ``_live`` → outer
  ``events()`` catches and yields LiveStreamError(is_region_block=True)
* provider raises ``ProviderError`` inside ``_live`` → outer
  ``events()`` catches and yields LiveStreamError(is_region_block=False)
"""

from __future__ import annotations

from typing import Iterator, List, Optional

from investment_team.trading_service.data_stream.live_stream import (
    CutoverEvent,
    LiveBarEvent,
    LiveStream,
    LiveStreamConfig,
    LiveStreamEnd,
    LiveStreamError,
)
from investment_team.trading_service.data_stream.resampler import NativeBar, NativeEvent
from investment_team.trading_service.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderRegionBlocked,
)

# ---------------------------------------------------------------------------
# Stub provider — supplies a scripted live() generator and a configurable
# smallest_available() return so tests can pivot the pump's branches.
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(
        self,
        *,
        name: str = "stub",
        smallest: Optional[str] = "1m",
        live_events: Optional[List[NativeEvent]] = None,
        live_raises: Optional[Exception] = None,
    ) -> None:
        self.capabilities = ProviderCapabilities(
            name=name,
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        )
        self._smallest = smallest
        self._live_events = live_events or []
        self._live_raises = live_raises

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        return self._smallest

    def live(self, **kwargs) -> Iterator[NativeEvent]:
        if self._live_raises is not None:
            raise self._live_raises
        yield from self._live_events


def _native_bar(ts: str, close: float, symbol: str = "BTC") -> NativeBar:
    return NativeBar(
        symbol=symbol,
        timestamp=ts,
        timeframe="1m",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000.0,
    )


def _config(stop_flag=None, warmup_bars: int = 0) -> LiveStreamConfig:
    return LiveStreamConfig(
        symbols=["BTC"],
        asset_class="crypto",
        strategy_timeframe="1m",
        warmup_bars=warmup_bars,
        stop_flag=stop_flag,
    )


# ---------------------------------------------------------------------------
# Tests for _live()
# ---------------------------------------------------------------------------


def test_live_pump_emits_cutover_then_bar_then_provider_end() -> None:
    """Happy path: one native bar yields CutoverEvent + LiveBarEvent,
    provider exhaustion yields LiveStreamEnd('provider_end')."""
    provider = _StubProvider(
        live_events=[
            _native_bar("2024-05-01T12:01:00Z", 100.0),
            _native_bar("2024-05-01T12:02:00Z", 101.0),
        ]
    )
    stream = LiveStream(provider=provider, config=_config(warmup_bars=0))

    events = list(stream._live())

    # First yielded event must be the cutover marker bound to the first bar.
    assert isinstance(events[0], CutoverEvent)
    assert events[0].cutover_ts == "2024-05-01T12:01:00Z"
    # The two LiveBarEvents follow (native_tf == strategy_tf → passthrough).
    bar_events = [e for e in events if isinstance(e, LiveBarEvent)]
    assert len(bar_events) == 2
    assert bar_events[0].bar.close == 100.0
    assert bar_events[1].bar.close == 101.0
    # Provider generator exhausted → clean termination marker.
    assert isinstance(events[-1], LiveStreamEnd)
    assert events[-1].reason == "provider_end"
    # cutover_ts is exposed on the public property too.
    assert stream.cutover_ts == "2024-05-01T12:01:00Z"


def test_live_pump_returns_error_when_provider_has_no_live_feed() -> None:
    """``smallest_available`` returns None → LiveStreamError with provider
    name in the reason, and the pump never asks the provider for events."""
    provider = _StubProvider(smallest=None, live_events=[])
    stream = LiveStream(provider=provider, config=_config())

    events = list(stream._live())

    assert len(events) == 1
    assert isinstance(events[0], LiveStreamError)
    assert "stub" in events[0].reason
    assert "no live feed" in events[0].reason
    assert events[0].is_region_block is False
    # Pump terminated before capturing a cut-over.
    assert stream.cutover_ts is None


def test_live_pump_terminates_on_stop_flag() -> None:
    """A stop flag tripped after the first bar must yield LiveStreamEnd
    with reason='user_stop' and not continue to subsequent native events."""
    stop_state = {"calls": 0}

    def _stop() -> bool:
        stop_state["calls"] += 1
        # Stop AFTER the first iteration finishes (the check sits at the end
        # of the loop body, so the second call is the one that returns True).
        return stop_state["calls"] >= 2

    provider = _StubProvider(
        live_events=[
            _native_bar("2024-05-01T12:01:00Z", 100.0),
            _native_bar("2024-05-01T12:02:00Z", 101.0),
            # Should NEVER be drained — proves the stop flag short-circuits.
            _native_bar("2024-05-01T12:03:00Z", 102.0),
        ]
    )
    stream = LiveStream(provider=provider, config=_config(stop_flag=_stop))

    events = list(stream._live())

    # Cutover + at least one bar + user_stop terminator.
    assert isinstance(events[0], CutoverEvent)
    bar_events = [e for e in events if isinstance(e, LiveBarEvent)]
    assert 1 <= len(bar_events) <= 2  # depends on which iteration the stop trips
    assert isinstance(events[-1], LiveStreamEnd)
    assert events[-1].reason == "user_stop"
    # The provider_end marker must NOT appear — termination was user-driven.
    ends = [e for e in events if isinstance(e, LiveStreamEnd)]
    assert len(ends) == 1


def test_live_pump_yields_clean_end_when_provider_generator_exhausts_immediately() -> None:
    """An empty live() generator falls through to the LiveStreamEnd marker
    (no cutover captured because no events were observed)."""
    provider = _StubProvider(live_events=[])
    stream = LiveStream(provider=provider, config=_config())

    events = list(stream._live())

    assert events == [LiveStreamEnd(reason="provider_end")]
    assert stream.cutover_ts is None


def test_events_wraps_live_region_block_into_terminal_error() -> None:
    """A ``ProviderRegionBlocked`` raised by ``live()`` must surface via
    ``events()`` as a single LiveStreamError with ``is_region_block=True``."""
    provider = _StubProvider(live_raises=ProviderRegionBlocked("blocked in region"))
    stream = LiveStream(provider=provider, config=_config(warmup_bars=0))

    events = list(stream.events())

    region_errors = [e for e in events if isinstance(e, LiveStreamError)]
    assert len(region_errors) == 1
    assert region_errors[0].is_region_block is True
    assert "blocked" in region_errors[0].reason


def test_events_wraps_live_provider_error_into_terminal_error() -> None:
    """A generic ``ProviderError`` raised by ``live()`` must surface via
    ``events()`` as a single LiveStreamError with ``is_region_block=False``."""
    provider = _StubProvider(live_raises=ProviderError("feed disconnected"))
    stream = LiveStream(provider=provider, config=_config(warmup_bars=0))

    events = list(stream.events())

    errors = [e for e in events if isinstance(e, LiveStreamError)]
    assert len(errors) == 1
    assert errors[0].is_region_block is False
    assert "disconnected" in errors[0].reason
