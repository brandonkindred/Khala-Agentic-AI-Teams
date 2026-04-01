"""Market data service — fetches real OHLCV price data from free public APIs."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Dict, List

import httpx
from pydantic import BaseModel

from .models import StrategySpec
from .strategy_lab_context import normalize_asset_class
from .symbols import (
    COINGECKO_IDS,
    COMMODITY_SYMBOLS,
    CRYPTO_SYMBOLS,
    FOREX_SYMBOLS,
    FUTURES_SYMBOLS,
    STOCK_SYMBOLS,
)

logger = logging.getLogger(__name__)


class OHLCVBar(BaseModel):
    """A single OHLCV price bar."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketDataService:
    """Fetches real market data from public sources.

    - Stocks/ETFs/Forex/Futures: Yahoo Finance via yfinance
    - Crypto: CoinGecko free API (no key required)
    """

    def __init__(self, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_ohlcv(self, symbol: str, asset_class: str, days: int = 365) -> List[OHLCVBar]:
        """Route to best data source for the asset class (recent N days)."""
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=days)
        return self.fetch_ohlcv_range(symbol, asset_class, start_dt.isoformat(), end_dt.isoformat())

    def fetch_ohlcv_range(
        self, symbol: str, asset_class: str, start_date: str, end_date: str
    ) -> List[OHLCVBar]:
        """Fetch OHLCV data for an explicit date range. Routes by asset class."""
        if normalize_asset_class(asset_class) == "crypto":
            return self._fetch_crypto(symbol, start_date, end_date)
        # All non-crypto assets are fetched via yfinance (stocks, forex, futures,
        # commodities, options).  yfinance supports =X (forex) and =F (futures)
        # suffixes natively.
        return self._fetch_stock(symbol, start_date, end_date)

    def get_symbols_for_strategy(self, strategy: StrategySpec) -> List[str]:
        """Return relevant symbols based on the strategy's asset class."""
        asset = normalize_asset_class(strategy.asset_class)
        symbol_map = {
            "crypto": CRYPTO_SYMBOLS,
            "stocks": STOCK_SYMBOLS,
            "options": STOCK_SYMBOLS,
            "forex": FOREX_SYMBOLS,
            "futures": FUTURES_SYMBOLS,
            "commodities": COMMODITY_SYMBOLS,
        }
        return list(symbol_map.get(asset, STOCK_SYMBOLS))

    def fetch_multi_symbol(
        self, symbols: List[str], asset_class: str, days: int = 365
    ) -> Dict[str, List[OHLCVBar]]:
        """Fetch OHLCV data for multiple symbols in parallel (recent N days)."""
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=days)
        return self.fetch_multi_symbol_range(
            symbols, asset_class, start_dt.isoformat(), end_dt.isoformat()
        )

    def fetch_multi_symbol_range(
        self, symbols: List[str], asset_class: str, start_date: str, end_date: str
    ) -> Dict[str, List[OHLCVBar]]:
        """Fetch OHLCV data for multiple symbols over an explicit date range.

        Uses a thread pool to fetch symbols in parallel.
        """
        result: Dict[str, List[OHLCVBar]] = {}
        workers = min(len(symbols), 5)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.fetch_ohlcv_range, sym, asset_class, start_date, end_date): sym
                for sym in symbols
            }
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    bars = future.result()
                    if bars:
                        result[sym] = bars
                except Exception as exc:
                    logger.warning("Failed to fetch %s: %s", sym, exc)
        return result

    # ------------------------------------------------------------------
    # Internal: Yahoo Finance (stocks, forex, futures, commodities)
    # ------------------------------------------------------------------

    def _fetch_stock(
        self, symbol: str, start_date: str, end_date: str, max_retries: int = 3
    ) -> List[OHLCVBar]:
        """Fetch stock/ETF/forex/futures OHLCV data via yfinance for an arbitrary date range.

        Retries with exponential backoff on transient failures, matching the
        CoinGecko retry pattern.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed — falling back to empty data for %s", symbol)
            return []

        for attempt in range(max_retries):
            try:
                ticker = yf.Ticker(symbol)
                df = ticker.history(start=start_date, end=end_date, interval="1d")
            except Exception as exc:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "yfinance fetch failed for %s, retrying in %ds (attempt %d): %s",
                        symbol,
                        wait,
                        attempt + 1,
                        exc,
                    )
                    time.sleep(wait)
                    continue
                logger.error(
                    "yfinance fetch failed for %s after %d attempts: %s", symbol, max_retries, exc
                )
                return []

            if df is not None and not df.empty:
                bars: List[OHLCVBar] = []
                for idx, row in df.iterrows():
                    bar_date = (
                        idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                    )
                    bars.append(
                        OHLCVBar(
                            date=bar_date,
                            open=round(float(row["Open"]), 4),
                            high=round(float(row["High"]), 4),
                            low=round(float(row["Low"]), 4),
                            close=round(float(row["Close"]), 4),
                            volume=float(row.get("Volume", 0)),
                        )
                    )
                return bars

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "No data from yfinance for %s, retrying in %ds (attempt %d)",
                    symbol,
                    wait,
                    attempt + 1,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    "No data returned from yfinance for %s after %d attempts", symbol, max_retries
                )

        return []

    # ------------------------------------------------------------------
    # Internal: CoinGecko (crypto)
    # ------------------------------------------------------------------

    def _fetch_crypto(self, symbol: str, start_date: str, end_date: str) -> List[OHLCVBar]:
        """Fetch crypto OHLCV data via CoinGecko ``/market_chart/range`` endpoint.

        Uses Unix timestamps so arbitrary historical windows work correctly.
        Includes retry with exponential backoff for rate-limit (429) responses.
        """
        coin_id = COINGECKO_IDS.get(symbol.upper())
        if not coin_id:
            logger.warning("Unknown crypto symbol %s — no CoinGecko mapping", symbol)
            return []

        try:
            start_dt = date.fromisoformat(start_date)
            end_dt = date.fromisoformat(end_date)
        except ValueError:
            logger.error("Invalid date range for crypto fetch: %s - %s", start_date, end_date)
            return []

        import calendar
        from datetime import datetime as dt_cls
        from datetime import timezone as tz_cls

        start_ts = int(
            calendar.timegm(
                dt_cls(start_dt.year, start_dt.month, start_dt.day, tzinfo=tz_cls.utc).timetuple()
            )
        )
        end_ts = int(
            calendar.timegm(
                dt_cls(
                    end_dt.year, end_dt.month, end_dt.day, 23, 59, 59, tzinfo=tz_cls.utc
                ).timetuple()
            )
        )

        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
        params = {"vs_currency": "usd", "from": str(start_ts), "to": str(end_ts)}

        raw = self._coingecko_get(url, params)
        if raw is None:
            return []

        if not isinstance(raw, dict) or "prices" not in raw:
            logger.warning("Unexpected CoinGecko response for %s", symbol)
            return []

        # Group price points by date to build daily OHLCV bars.
        # The /market_chart/range endpoint returns granularity based on span
        # (5-min for <1 day, hourly for 1-90 days, daily for >90 days).
        daily: Dict[str, List[float]] = {}
        for ts_ms, price in raw.get("prices", []):
            bar_date = date.fromtimestamp(ts_ms / 1000).isoformat()
            daily.setdefault(bar_date, []).append(float(price))

        bars: List[OHLCVBar] = []
        for bar_date in sorted(daily):
            prices = daily[bar_date]
            bars.append(
                OHLCVBar(
                    date=bar_date,
                    open=round(prices[0], 4),
                    high=round(max(prices), 4),
                    low=round(min(prices), 4),
                    close=round(prices[-1], 4),
                    volume=0.0,
                )
            )
        return bars

    def _coingecko_get(self, url: str, params: Dict[str, str], max_retries: int = 3) -> object:
        """HTTP GET with retry + exponential backoff for CoinGecko rate limits."""
        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "CoinGecko rate limited, retrying in %ds (attempt %d)", wait, attempt + 1
                    )
                    time.sleep(wait)
                else:
                    logger.error("CoinGecko request failed: %s", exc)
                    return None
            except Exception as exc:
                logger.error("CoinGecko request failed: %s", exc)
                return None
        return None
