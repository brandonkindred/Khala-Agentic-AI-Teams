"""Binance provider adapter — crypto primary default (free, keyless).

Historical: REST ``/api/v3/klines`` (free, no auth).
Live: public market-data websocket at ``wss://stream.binance.com:9443``
(implementation in :mod:`binance_ws`; this file owns the adapter Protocol
wiring and REST klines).

See ``system_design/pr2_live_data_and_paper_cutover.md`` §2.2 / §13.
"""

from __future__ import annotations

import logging
import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from ..strategy.contract import Bar
from .base import ProviderCapabilities, ProviderError

logger = logging.getLogger(__name__)


CAPABILITIES = ProviderCapabilities(
    name="binance",
    supports={"crypto"},
    is_paid=False,
    historical_timeframes={"1s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"},
    live_timeframes={"tick", "1s", "15s", "1m"},
)


# Binance's kline interval labels differ from ours for some values (e.g. ``1s``
# is written as ``1s``, ``1m`` as ``1m``, etc.) — the mapping below is the
# source of truth. "tick" is handled via the trade stream, not klines.
_KLINE_INTERVAL_MAP = {
    "1s": "1s",
    "15s": "15s",  # only on WS, not REST; REST lookups rejected below
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

_REST_UNSUPPORTED_TIMEFRAMES = {"15s"}  # 15s is WS-only per Binance docs


class BinanceAdapter:
    """Free, keyless Binance market-data adapter."""

    capabilities = CAPABILITIES

    def __init__(self, *, base_rest: Optional[str] = None, base_ws: Optional[str] = None) -> None:
        self._rest = base_rest or os.environ.get("BINANCE_REST_URL", "https://api.binance.com")
        self._ws = base_ws or os.environ.get("BINANCE_WS_URL", "wss://stream.binance.com:9443")

    # ------------------------------------------------------------------

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class != "crypto":
            return None
        return "tick" if live else "1s"

    # ------------------------------------------------------------------

    def historical(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start: str,
        end: str,
        timeframe: str,
    ) -> Iterator[BarEvent]:
        if asset_class != "crypto":
            raise ValueError("binance adapter only serves asset_class='crypto'")
        if timeframe in _REST_UNSUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"binance historical endpoint does not support timeframe={timeframe!r}; "
                "request the next-larger timeframe or use the live path with resampler"
            )
        if timeframe not in _KLINE_INTERVAL_MAP:
            raise ValueError(
                f"binance adapter cannot serve timeframe={timeframe!r}; "
                f"supported: {sorted(self.capabilities.historical_timeframes)}"
            )

        # Interleaved by timestamp across symbols — the service expects bars
        # in chronological order.
        per_symbol: dict[str, List[BarEvent]] = {}
        for symbol in symbols:
            per_symbol[symbol] = list(
                self._fetch_klines(symbol=symbol, interval=timeframe, start=start, end=end)
            )
        timeline: List[tuple[str, BarEvent]] = []
        for sym, bars in per_symbol.items():
            for b in bars:
                timeline.append((b.bar.timestamp, b))
        timeline.sort(key=lambda x: x[0])
        for _, b in timeline:
            yield b

    def _fetch_klines(
        self, *, symbol: str, interval: str, start: str, end: str
    ) -> Iterator[BarEvent]:
        import httpx  # local import keeps cold-start cheap

        start_ms = _iso_to_ms(start)
        end_ms = _iso_to_ms(end)
        cursor = start_ms
        url = f"{self._rest}/api/v3/klines"
        with httpx.Client(timeout=30.0) as client:
            while cursor < end_ms:
                params = {
                    "symbol": symbol.upper().replace("-", ""),
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                }
                resp = client.get(url, params=params)
                if resp.status_code == 451:  # regional block
                    from .base import ProviderRegionBlocked

                    raise ProviderRegionBlocked("binance REST returned HTTP 451 (region blocked)")
                if resp.status_code != 200:
                    raise ProviderError(f"binance REST error {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                if not data:
                    break
                for row in data:
                    # row: [open_time, open, high, low, close, volume, close_time, ...]
                    close_ms = int(row[6])
                    yield BarEvent(
                        bar=Bar(
                            symbol=symbol,
                            timestamp=_ms_to_iso(close_ms + 1),  # close-inclusive ISO label
                            timeframe=interval,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                        )
                    )
                # Advance cursor to avoid refetching the last bar.
                next_cursor = int(data[-1][6]) + 1
                if next_cursor <= cursor:
                    break
                cursor = next_cursor

    # ------------------------------------------------------------------

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        if asset_class != "crypto":
            raise ValueError("binance adapter only serves asset_class='crypto'")
        if native_timeframe not in self.capabilities.live_timeframes:
            raise ValueError(f"binance live feed does not support timeframe={native_timeframe!r}")
        from .binance_ws import run_binance_live

        # Binance REST uses "BTCUSDT"; WS lowercases to "btcusdt". Normalise.
        normalised = [s.upper().replace("-", "") for s in symbols]
        yield from run_binance_live(
            base_ws=self._ws,
            symbols=normalised,
            native_timeframe=native_timeframe,
        )


def build() -> BinanceAdapter:
    return BinanceAdapter()


def _iso_to_ms(iso: str) -> int:
    from datetime import datetime, timezone

    s = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
    dt = (
        datetime.fromisoformat(s)
        if len(iso) > 10
        else datetime.fromisoformat(iso + "T00:00:00+00:00")
    )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["BinanceAdapter", "CAPABILITIES", "build"]
