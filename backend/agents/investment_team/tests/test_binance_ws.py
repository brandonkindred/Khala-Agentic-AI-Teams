"""Unit tests for the Binance websocket message parsers.

The async pump itself (``run_binance_live``) is exercised via manual
smoke scripts with real network; these tests cover the pure-function
parsing layer that converts Binance's JSON wire format to our
:class:`NativeTick` / :class:`NativeBar` types. Keeping parsing separate
from I/O is what makes this layer testable without network access.
"""

from __future__ import annotations

from investment_team.trading_service.data_stream.resampler import (
    NativeBar,
    NativeTick,
)
from investment_team.trading_service.providers.binance_ws import (
    _build_stream_url,
    dispatch_binance_message,
    parse_binance_kline,
    parse_binance_trade,
)

# ---------------------------------------------------------------------------
# Trade parsing
# ---------------------------------------------------------------------------


def test_parse_binance_trade_typical() -> None:
    # Binance @trade schema — the "T" field is trade-time in ms.
    msg = {
        "e": "trade",
        "E": 1700000000100,
        "s": "BTCUSDT",
        "t": 12345,
        "p": "60000.10",
        "q": "0.01",
        "T": 1700000000050,
        "m": True,
        "M": True,
    }
    tick = parse_binance_trade(msg)
    assert isinstance(tick, NativeTick)
    assert tick.symbol == "BTCUSDT"
    assert tick.price == 60000.10
    assert tick.size == 0.01
    # 1700000000050 ms → 2023-11-14T22:13:20.050Z
    assert tick.timestamp.startswith("2023-11-14T22:13:20")


# ---------------------------------------------------------------------------
# Kline parsing — only emit on close (x=True)
# ---------------------------------------------------------------------------


def test_parse_binance_kline_open_is_none() -> None:
    """In-progress (x=False) klines must NOT emit — that would leak a partial."""
    msg = {
        "e": "kline",
        "k": {
            "t": 1700000000000,
            "T": 1700000059999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "60000.0",
            "c": "60010.0",
            "h": "60020.0",
            "l": "59990.0",
            "v": "1.2",
            "x": False,
        },
    }
    assert parse_binance_kline(msg) is None


def test_parse_binance_kline_close_emits_bar() -> None:
    msg = {
        "e": "kline",
        "k": {
            "t": 1700000000000,
            "T": 1700000059999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "60000.0",
            "c": "60015.0",
            "h": "60020.0",
            "l": "59990.0",
            "v": "1.2",
            "x": True,
        },
    }
    bar = parse_binance_kline(msg)
    assert isinstance(bar, NativeBar)
    assert bar.symbol == "BTCUSDT"
    assert bar.timeframe == "1m"
    assert bar.open == 60000.0
    assert bar.close == 60015.0
    assert bar.high == 60020.0
    assert bar.low == 59990.0
    assert bar.volume == 1.2
    # Close timestamp should be Binance's T + 1ms → 1700000060000 ms
    assert bar.timestamp.startswith("2023-11-14T22:14:20")


# ---------------------------------------------------------------------------
# dispatch_binance_message — routing + combined-stream envelope
# ---------------------------------------------------------------------------


def test_dispatch_routes_trade_to_tick() -> None:
    msg = {"e": "trade", "s": "ETHUSDT", "p": "3000", "q": "0.5", "T": 1700000000000}
    result = dispatch_binance_message(msg)
    assert isinstance(result, NativeTick)
    assert result.symbol == "ETHUSDT"


def test_dispatch_unwraps_combined_stream_envelope() -> None:
    """Combined-stream messages wrap the payload under ``data``."""
    envelope = {
        "stream": "btcusdt@trade",
        "data": {
            "e": "trade",
            "s": "BTCUSDT",
            "p": "60000",
            "q": "0.1",
            "T": 1700000000000,
        },
    }
    result = dispatch_binance_message(envelope)
    assert isinstance(result, NativeTick)
    assert result.symbol == "BTCUSDT"


def test_dispatch_ignores_unknown_event_type() -> None:
    """Subscription acks / heartbeats / unknown events drop silently."""
    assert dispatch_binance_message({"result": None, "id": 1}) is None
    assert dispatch_binance_message({"e": "bookTicker"}) is None


def test_dispatch_kline_in_progress_returns_none() -> None:
    """Even when routing, an in-progress kline should not emit."""
    msg = {
        "e": "kline",
        "k": {
            "t": 0,
            "T": 59999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "1",
            "c": "1",
            "h": "1",
            "l": "1",
            "v": "0",
            "x": False,
        },
    }
    assert dispatch_binance_message(msg) is None


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_build_stream_url_tick_uses_trade_channel() -> None:
    url = _build_stream_url("wss://stream.binance.com:9443", ["BTCUSDT", "ETHUSDT"], "tick")
    assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade"


def test_build_stream_url_kline_uses_kline_channel() -> None:
    url = _build_stream_url("wss://stream.binance.com:9443", ["BTCUSDT"], "1m")
    assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m"


def test_build_stream_url_normalises_symbol_case() -> None:
    # Symbols should always be lowercased for Binance's stream names.
    url = _build_stream_url("wss://stream.binance.com:9443", ["btcUSDT"], "tick")
    assert "btcusdt@trade" in url
