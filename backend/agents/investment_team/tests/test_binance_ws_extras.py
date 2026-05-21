"""Extra coverage for ``trading_service.providers.binance_ws``.

The async pump is hard to exercise without a real WebSocket server.
These tests focus on the deterministic pure helpers (parsers + URL
builder + dispatcher) plus the run_binance_live error-propagation path
with a stubbed event loop.
"""

from __future__ import annotations

import queue
import threading

import pytest

from investment_team.trading_service.data_stream.resampler import NativeBar, NativeTick
from investment_team.trading_service.providers.base import (
    ProviderRegionBlocked,
)
from investment_team.trading_service.providers.binance_ws import (
    _build_stream_url,
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
