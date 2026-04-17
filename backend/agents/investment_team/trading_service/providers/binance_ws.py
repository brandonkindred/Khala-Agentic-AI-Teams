"""Binance websocket pump — async client → sync iterator bridge.

The adapter Protocol requires a synchronous generator; ``websockets`` is
async. This module runs an asyncio event loop in a background thread, the
async coroutine pushes parsed :class:`NativeEvent` objects onto a
``queue.Queue``, and the sync generator yields them.

Split out from ``binance.py`` so the adapter file stays small and the
pump's async plumbing is independently testable.

Message shapes (see Binance WebSocket Market Streams docs):

* ``@trade`` — single trade print:
  ``{"e":"trade","T":1700000000000,"s":"BTCUSDT","p":"60000.1","q":"0.01", …}``
* ``@kline_<tf>`` — candle updates; ``k.x=true`` when the candle closes:
  ``{"e":"kline","k":{"t":..,"T":..,"o":..,"c":..,"h":..,"l":..,"v":..,"x":bool}}``
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Iterator, List, Optional

from ..data_stream.resampler import NativeBar, NativeEvent, NativeTick
from .base import ProviderError, ProviderRegionBlocked

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing — pure functions, unit-testable without any network
# ---------------------------------------------------------------------------


def parse_binance_trade(payload: dict) -> NativeTick:
    """Convert a Binance ``@trade`` message to a :class:`NativeTick`.

    Binance gives millisecond Unix timestamps; we emit ISO-8601 UTC.
    """
    import datetime as _dt

    ts_ms = int(payload["T"])
    return NativeTick(
        timestamp=_dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        symbol=str(payload["s"]),
        price=float(payload["p"]),
        size=float(payload.get("q", 0.0)),
    )


def parse_binance_kline(payload: dict) -> Optional[NativeBar]:
    """Return a :class:`NativeBar` iff the kline has closed, else ``None``.

    Binance sends kline updates continuously; ``k.x == True`` flags the
    final update for a given interval. We only emit on close so downstream
    consumers (the resampler) never see a partial candle.
    """
    import datetime as _dt

    k = payload.get("k") or {}
    if not k.get("x"):
        return None

    close_ms = int(k["T"]) + 1  # Binance uses close-exclusive; shift +1ms
    return NativeBar(
        timestamp=_dt.datetime.fromtimestamp(close_ms / 1000.0, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        symbol=str(k["s"]),
        timeframe=str(k["i"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k.get("v", 0.0)),
    )


def dispatch_binance_message(data: dict) -> Optional[NativeEvent]:
    """Route a parsed JSON payload to the right converter, or None if ignorable."""
    # Combined-stream messages wrap the payload under ``data``.
    if "stream" in data and "data" in data:
        payload = data["data"]
    else:
        payload = data

    event_type = payload.get("e")
    if event_type == "trade":
        return parse_binance_trade(payload)
    if event_type == "kline":
        return parse_binance_kline(payload)
    # Subscription acks, heartbeats, other noise — safe to ignore.
    return None


# ---------------------------------------------------------------------------
# Async pump running in a background thread
# ---------------------------------------------------------------------------


@dataclass
class _PumpState:
    events: "queue.Queue[Optional[NativeEvent]]"
    error: Optional[BaseException] = None
    stop: threading.Event = None  # type: ignore[assignment]


def _build_stream_url(base_ws: str, symbols: List[str], native_timeframe: str) -> str:
    """Combined-streams URL for the given symbols at the given timeframe.

    ``native_timeframe == "tick"`` selects the ``@trade`` channel; any other
    value goes through ``@kline_<tf>``.
    """
    channel_suffix = "@trade" if native_timeframe == "tick" else f"@kline_{native_timeframe}"
    streams = "/".join(f"{s.lower()}{channel_suffix}" for s in symbols)
    return f"{base_ws}/stream?streams={streams}"


async def _pump_coroutine(
    *,
    url: str,
    state: _PumpState,
) -> None:
    """Open the WS, parse each message, enqueue NativeEvents."""
    import websockets
    from websockets.exceptions import InvalidStatus

    try:
        async with websockets.connect(url, open_timeout=10.0, ping_interval=20.0) as ws:
            while not state.stop.is_set():
                try:
                    raw = await ws.recv()
                except Exception as exc:  # connection closed / timeout
                    state.error = ProviderError(f"binance ws recv failed: {exc}")
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("binance ws: non-JSON frame dropped")
                    continue
                event = dispatch_binance_message(data)
                if event is not None:
                    state.events.put(event)
    except InvalidStatus as exc:
        # HTTP upgrade rejected. 451 = region block; anything else is a
        # generic provider error.
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 451:
            state.error = ProviderRegionBlocked(
                "binance websocket rejected connection with HTTP 451 (region blocked)"
            )
        else:
            state.error = ProviderError(
                f"binance websocket upgrade failed: HTTP {status_code or '???'}"
            )
    except Exception as exc:  # pragma: no cover - defensive
        state.error = ProviderError(f"binance ws pump crashed: {exc}")
    finally:
        # Sentinel: wake the sync consumer and signal clean shutdown.
        state.events.put(None)


def run_binance_live(
    *,
    base_ws: str,
    symbols: List[str],
    native_timeframe: str,
    max_queue: int = 1024,
) -> Iterator[NativeEvent]:
    """Synchronous live-feed iterator.

    Starts an asyncio event loop in a background thread; yields parsed
    events from the queue as they arrive. When the caller stops iterating
    or the pump signals an error, the loop is terminated cleanly.

    Raises :class:`ProviderRegionBlocked` if Binance rejects the
    connection upgrade with HTTP 451 (geo-failover trigger). Raises
    :class:`ProviderError` for any other terminal failure.
    """
    url = _build_stream_url(base_ws, symbols, native_timeframe)
    state = _PumpState(events=queue.Queue(maxsize=max_queue), stop=threading.Event())

    def _thread_target() -> None:
        import asyncio

        asyncio.run(_pump_coroutine(url=url, state=state))

    thread = threading.Thread(target=_thread_target, name="binance-ws-pump", daemon=True)
    thread.start()

    try:
        while True:
            event = state.events.get()
            if event is None:
                # Pump signaled completion — propagate any stashed error.
                if state.error is not None:
                    raise state.error
                return
            yield event
    finally:
        state.stop.set()
        # Drain up to a couple seconds waiting for the async pump to exit.
        thread.join(timeout=2.0)


__all__ = [
    "dispatch_binance_message",
    "parse_binance_kline",
    "parse_binance_trade",
    "run_binance_live",
]
