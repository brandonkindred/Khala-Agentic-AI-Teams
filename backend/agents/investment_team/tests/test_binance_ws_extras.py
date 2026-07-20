"""Extra coverage for ``trading_service.providers.binance_ws``.

Covers the deterministic pure helpers (parsers + URL builder +
dispatcher), the ``run_binance_live`` error-propagation path via a
patched ``_pump_coroutine``, *and* the async ``_pump_coroutine`` itself
via a stubbed ``websockets.connect`` that scripts the message stream
without any real network I/O.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from unittest.mock import MagicMock

import pytest
import websockets
import websockets.exceptions  # noqa: F401 — populate the ``websockets.exceptions`` attribute

from investment_team.trading_service.data_stream.resampler import NativeBar, NativeTick
from investment_team.trading_service.providers.base import (
    ProviderError,
    ProviderRegionBlocked,
)
from investment_team.trading_service.providers.binance_ws import (
    _build_stream_url,
    _pump_coroutine,
    _PumpState,
    dispatch_binance_message,
    parse_binance_kline,
    parse_binance_trade,
    run_binance_live,
)

# ---------------------------------------------------------------------------
# parse_binance_trade
# ---------------------------------------------------------------------------


def test_parse_binance_trade_returns_native_tick() -> None:
    payload = {"e": "trade", "T": 1704067200000, "s": "BTCUSDT", "p": "60000.1", "q": "0.5"}
    tick = parse_binance_trade(payload)
    assert isinstance(tick, NativeTick)
    assert tick.symbol == "BTCUSDT"
    assert tick.price == 60000.1
    assert tick.size == 0.5
    assert tick.timestamp.endswith("Z")


def test_parse_binance_trade_defaults_missing_size_to_zero() -> None:
    payload = {"T": 1704067200000, "s": "BTCUSDT", "p": "1.0"}
    tick = parse_binance_trade(payload)
    assert tick.size == 0.0


# ---------------------------------------------------------------------------
# parse_binance_kline
# ---------------------------------------------------------------------------


def test_parse_binance_kline_skips_unclosed() -> None:
    payload = {"k": {"x": False, "T": 0, "s": "BTCUSDT", "i": "1m", "o": "1", "h": "2", "l": "0", "c": "1.5"}}
    assert parse_binance_kline(payload) is None


def test_parse_binance_kline_returns_native_bar_for_closed_candle() -> None:
    payload = {
        "k": {
            "x": True,
            "T": 1704067200000,  # close-exclusive ms
            "s": "BTCUSDT",
            "i": "1m",
            "o": "100.0",
            "h": "102.0",
            "l": "99.0",
            "c": "101.0",
            "v": "10.0",
        }
    }
    bar = parse_binance_kline(payload)
    assert isinstance(bar, NativeBar)
    assert bar.symbol == "BTCUSDT"
    assert bar.timeframe == "1m"
    assert bar.open == 100.0
    assert bar.high == 102.0
    assert bar.low == 99.0
    assert bar.close == 101.0
    assert bar.volume == 10.0
    assert bar.timestamp.endswith("Z")


def test_parse_binance_kline_handles_missing_volume() -> None:
    payload = {
        "k": {
            "x": True, "T": 0, "s": "BTCUSDT", "i": "1m",
            "o": "1", "h": "1", "l": "1", "c": "1",
        }
    }
    bar = parse_binance_kline(payload)
    assert bar.volume == 0.0


# ---------------------------------------------------------------------------
# dispatch_binance_message
# ---------------------------------------------------------------------------


def test_dispatch_routes_trade_event() -> None:
    msg = {"e": "trade", "T": 0, "s": "BTCUSDT", "p": "1", "q": "1"}
    out = dispatch_binance_message(msg)
    assert isinstance(out, NativeTick)


def test_dispatch_routes_kline_event() -> None:
    msg = {
        "e": "kline",
        "k": {"x": True, "T": 0, "s": "BTCUSDT", "i": "1m", "o": "1", "h": "2", "l": "0", "c": "1"},
    }
    out = dispatch_binance_message(msg)
    assert isinstance(out, NativeBar)


def test_dispatch_unwraps_combined_stream_envelope() -> None:
    msg = {"stream": "btcusdt@trade", "data": {"e": "trade", "T": 0, "s": "BTCUSDT", "p": "1", "q": "1"}}
    out = dispatch_binance_message(msg)
    assert isinstance(out, NativeTick)


def test_dispatch_returns_none_for_unknown_event() -> None:
    assert dispatch_binance_message({"e": "subscribed", "id": 1}) is None


# ---------------------------------------------------------------------------
# _build_stream_url
# ---------------------------------------------------------------------------


def test_build_stream_url_for_tick_uses_trade_channel() -> None:
    url = _build_stream_url("wss://stream.binance.com:9443", ["BTCUSDT", "ETHUSDT"], "tick")
    assert "btcusdt@trade" in url
    assert "ethusdt@trade" in url
    assert "stream?streams=" in url


def test_build_stream_url_for_kline_uses_timeframe() -> None:
    url = _build_stream_url("wss://stream.binance.com:9443", ["BTCUSDT"], "1m")
    assert "btcusdt@kline_1m" in url


# ---------------------------------------------------------------------------
# run_binance_live — error propagation
# ---------------------------------------------------------------------------


def test_run_binance_live_propagates_error_from_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the pump enqueues None and stashes an error, the iterator raises it."""

    # Stub the thread target so the pump immediately signals completion + error.
    def _fake_thread_target(*args, **kwargs):
        # Pull the pump state out of the closure via the threading module.
        # We rely on the run_binance_live shape: it creates state, starts a
        # daemon thread, then iterates. Patch threading.Thread.start so the
        # thread captures the state and signals error+None synchronously.
        pass

    # Easier: patch ``asyncio.run`` so the coroutine "completes immediately"
    # and stashes an error into the state object.
    import investment_team.trading_service.providers.binance_ws as ws_mod

    captured: dict = {}

    def _intercept_pump(*, url, state):
        # Simulate the async pump completing with a region-blocked error.
        captured["url"] = url
        state.error = ProviderRegionBlocked("HTTP 451")
        state.events.put(None)

    async def _async_intercept(*args, **kwargs):
        _intercept_pump(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "_pump_coroutine", _async_intercept)

    iterator = run_binance_live(base_ws="wss://x", symbols=["BTCUSDT"], native_timeframe="tick")
    with pytest.raises(ProviderRegionBlocked):
        next(iterator)
    assert "stream?streams=" in captured["url"]


def test_run_binance_live_returns_silently_when_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the pump enqueues None without an error, the iterator stops."""
    import investment_team.trading_service.providers.binance_ws as ws_mod

    async def _intercept(*, url, state):
        # Push a single tick then signal completion.
        tick = NativeTick(timestamp="2024-01-01T00:00:00Z", symbol="BTCUSDT", price=1.0, size=1.0)
        state.events.put(tick)
        state.events.put(None)

    monkeypatch.setattr(ws_mod, "_pump_coroutine", _intercept)

    out = list(run_binance_live(base_ws="wss://x", symbols=["BTCUSDT"], native_timeframe="tick"))
    assert len(out) == 1
    assert isinstance(out[0], NativeTick)


# ---------------------------------------------------------------------------
# _PumpState
# ---------------------------------------------------------------------------


def test_pump_state_fields() -> None:
    state = _PumpState(events=queue.Queue(), stop=threading.Event())
    assert state.error is None
    state.stop.set()
    assert state.stop.is_set()


# ---------------------------------------------------------------------------
# _pump_coroutine — stubbed websockets.connect, no real network
# ---------------------------------------------------------------------------


class _StubConnection:
    """Async-iterable WS connection stub: scripts ``recv`` returns."""

    def __init__(self, frames: list[object]) -> None:
        # frames may contain str (returned by recv) or Exception (raised).
        self._frames = list(frames)

    async def recv(self) -> str:
        if not self._frames:
            # No more scripted frames — simulate clean close.
            raise ConnectionError("stubbed connection closed")
        frame = self._frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return frame  # type: ignore[return-value]


class _StubConnect:
    """Async context manager returned by a stubbed ``websockets.connect``."""

    def __init__(self, connection: _StubConnection | None = None, raise_on_enter: BaseException | None = None) -> None:
        self._connection = connection
        self._raise = raise_on_enter
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> "_StubConnect":
        self.calls.append((args, kwargs))
        return self

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.fixture
def pump_state() -> _PumpState:
    return _PumpState(events=queue.Queue(), stop=threading.Event())


def _drain_queue(q: "queue.Queue[object]") -> list[object]:
    out: list[object] = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return out
        out.append(item)


def test_pump_coroutine_dispatches_messages_to_queue(
    monkeypatch: pytest.MonkeyPatch, pump_state: _PumpState
) -> None:
    """Happy path: scripted frames parse and land on the queue as NativeEvents."""
    trade_frame = json.dumps(
        {"stream": "btcusdt@trade", "data": {"e": "trade", "T": 0, "s": "BTCUSDT", "p": "1.5", "q": "0.25"}}
    )
    kline_frame = json.dumps(
        {
            "stream": "btcusdt@kline_1m",
            "data": {
                "e": "kline",
                "k": {"x": True, "T": 0, "s": "BTCUSDT", "i": "1m", "o": "1", "h": "2", "l": "0.5", "c": "1.8", "v": "9"},
            },
        }
    )
    bad_json_frame = "not-json"
    unclosed_kline_frame = json.dumps(
        {"e": "kline", "k": {"x": False, "T": 0, "s": "BTCUSDT", "i": "1m", "o": "1", "h": "2", "l": "0", "c": "1.5"}}
    )

    connection = _StubConnection([trade_frame, bad_json_frame, unclosed_kline_frame, kline_frame])
    connect_stub = _StubConnect(connection=connection)
    monkeypatch.setattr(websockets, "connect", connect_stub)

    asyncio.run(asyncio.wait_for(_pump_coroutine(url="wss://x/stream", state=pump_state), timeout=2.0))

    events = _drain_queue(pump_state.events)
    # 2 dispatched events (trade + closed kline) + None sentinel
    assert len(events) == 3
    assert isinstance(events[0], NativeTick)
    assert isinstance(events[1], NativeBar)
    assert events[2] is None
    # ConnectionError on recv after frames exhausted → ProviderError stashed
    assert isinstance(pump_state.error, ProviderError)
    # connect was called with the url we passed
    assert connect_stub.calls[0][0] == ("wss://x/stream",)


def test_pump_coroutine_stops_when_state_stop_set(
    monkeypatch: pytest.MonkeyPatch, pump_state: _PumpState
) -> None:
    """Clean termination: pre-set stop flag → loop exits without recv errors."""
    connection = _StubConnection([])  # never read from
    connect_stub = _StubConnect(connection=connection)
    monkeypatch.setattr(websockets, "connect", connect_stub)

    pump_state.stop.set()
    asyncio.run(asyncio.wait_for(_pump_coroutine(url="wss://x", state=pump_state), timeout=2.0))

    events = _drain_queue(pump_state.events)
    # Only the sentinel — no events, no error since recv never ran.
    assert events == [None]
    assert pump_state.error is None


def test_pump_coroutine_region_blocked_on_invalid_status_451(
    monkeypatch: pytest.MonkeyPatch, pump_state: _PumpState
) -> None:
    """HTTP 451 upgrade rejection → ProviderRegionBlocked stashed and sentinel queued."""
    response = MagicMock()
    response.status_code = 451
    invalid = websockets.exceptions.InvalidStatus(response)
    connect_stub = _StubConnect(raise_on_enter=invalid)
    monkeypatch.setattr(websockets, "connect", connect_stub)

    asyncio.run(asyncio.wait_for(_pump_coroutine(url="wss://x", state=pump_state), timeout=2.0))

    assert isinstance(pump_state.error, ProviderRegionBlocked)
    assert _drain_queue(pump_state.events) == [None]


def test_pump_coroutine_generic_provider_error_on_other_invalid_status(
    monkeypatch: pytest.MonkeyPatch, pump_state: _PumpState
) -> None:
    """Non-451 upgrade rejection → generic ProviderError with status code in message."""
    response = MagicMock()
    response.status_code = 503
    invalid = websockets.exceptions.InvalidStatus(response)
    connect_stub = _StubConnect(raise_on_enter=invalid)
    monkeypatch.setattr(websockets, "connect", connect_stub)

    asyncio.run(asyncio.wait_for(_pump_coroutine(url="wss://x", state=pump_state), timeout=2.0))

    assert isinstance(pump_state.error, ProviderError)
    assert not isinstance(pump_state.error, ProviderRegionBlocked)
    assert "503" in str(pump_state.error)
    assert _drain_queue(pump_state.events) == [None]


def test_pump_coroutine_recv_error_stashes_provider_error(
    monkeypatch: pytest.MonkeyPatch, pump_state: _PumpState
) -> None:
    """A recv() exception is wrapped into ProviderError and breaks the loop."""
    connection = _StubConnection([RuntimeError("socket boom")])
    connect_stub = _StubConnect(connection=connection)
    monkeypatch.setattr(websockets, "connect", connect_stub)

    asyncio.run(asyncio.wait_for(_pump_coroutine(url="wss://x", state=pump_state), timeout=2.0))

    assert isinstance(pump_state.error, ProviderError)
    assert "socket boom" in str(pump_state.error)
    assert _drain_queue(pump_state.events) == [None]
