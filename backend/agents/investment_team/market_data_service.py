"""Market data service — fetches real OHLCV price data with multi-source fallback.

Provider priority:
1. Yahoo Finance (yfinance) — all asset classes, no key
2. Twelve Data (REST) — stocks, forex, crypto, commodities, no key (800 req/day free)
3. CoinGecko /market_chart (crypto only, no key) / Alpha Vantage (non-crypto, optional key)
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Callable, Dict, List

import httpx
from pydantic import BaseModel

from .data_providers.symbol_maps import (
    resolve_alphavantage_forex,
    resolve_alphavantage_stock,
    resolve_twelve_data,
)
from .models import StrategySpec
from .strategy_lab_context import normalize_asset_class
from .symbols import (
    COINGECKO_IDS,
    COMMODITY_SYMBOLS,
    CRYPTO_SYMBOLS,
    FOREX_SYMBOLS,
    FUTURES_SYMBOLS,
    STOCK_SYMBOLS,
    YAHOO_CRYPTO_TICKERS,
)

logger = logging.getLogger(__name__)

_ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()


class OHLCVBar(BaseModel):
    """A single OHLCV price bar."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


# Type alias for a fetch function used in the provider chain.
_FetchFn = Callable[[str, str, str, str], List[OHLCVBar]]


class MarketDataService:
    """Fetches real market data with automatic multi-source fallback.

    For each symbol the service tries providers in priority order until one
    returns data.  The chain varies by asset class:

    - **crypto**: Yahoo Finance → Twelve Data → CoinGecko
    - **stocks / options / commodities**: Yahoo Finance → Twelve Data → Alpha Vantage
    - **forex / futures**: Yahoo Finance → Twelve Data → Alpha Vantage

    Alpha Vantage is only included when ``ALPHA_VANTAGE_API_KEY`` is set.
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
        """Fetch OHLCV data for an explicit date range with automatic fallback."""
        ac = normalize_asset_class(asset_class)
        for fetch_fn in self._get_provider_chain(ac):
            try:
                bars = fetch_fn(symbol, ac, start_date, end_date)
                if bars:
                    return bars
            except Exception as exc:
                logger.warning("Provider failed for %s (%s): %s", symbol, ac, exc)
        logger.warning("All providers exhausted for %s (%s)", symbol, ac)
        return []

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
    # Provider chain
    # ------------------------------------------------------------------

    def _get_provider_chain(self, asset_class: str) -> list[_FetchFn]:
        """Return an ordered list of fetch functions for the given asset class."""
        if asset_class == "crypto":
            return [self._fetch_yahoo, self._fetch_twelve_data, self._fetch_coingecko]
        chain: list[_FetchFn] = [self._fetch_yahoo, self._fetch_twelve_data]
        if _ALPHA_VANTAGE_API_KEY:
            chain.append(self._fetch_alphavantage)
        return chain

    # ------------------------------------------------------------------
    # Provider 1: Yahoo Finance
    # ------------------------------------------------------------------

    def _fetch_yahoo(
        self, symbol: str, asset_class: str, start_date: str, end_date: str, max_retries: int = 3
    ) -> List[OHLCVBar]:
        """Fetch OHLCV data via yfinance. Handles all asset classes.

        Uses ``auto_adjust=True`` (Phase 5) so prices are automatically adjusted
        for splits and dividends — eliminates a class of survivorship/corporate-
        action bugs in backtests.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed — skipping Yahoo for %s", symbol)
            return []

        # Map crypto symbols to Yahoo tickers (e.g. BTC → BTC-USD)
        if asset_class == "crypto":
            yf_symbol = YAHOO_CRYPTO_TICKERS.get(symbol.upper(), f"{symbol.upper()}-USD")
        else:
            yf_symbol = symbol

        for attempt in range(max_retries):
            try:
                ticker = yf.Ticker(yf_symbol)
                df = ticker.history(start=start_date, end=end_date, interval="1d", auto_adjust=True)
            except Exception as exc:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "yfinance failed for %s, retrying in %ds (attempt %d): %s",
                        yf_symbol, wait, attempt + 1, exc,
                    )
                    time.sleep(wait)
                    continue
                logger.warning("yfinance failed for %s after %d attempts: %s", yf_symbol, max_retries, exc)
                return []

            if df is not None and not df.empty:
                return self._df_to_bars(df)

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("No data from yfinance for %s, retrying in %ds (attempt %d)", yf_symbol, wait, attempt + 1)
                time.sleep(wait)
            else:
                logger.warning("No data from yfinance for %s after %d attempts", yf_symbol, max_retries)

        return []

    # ------------------------------------------------------------------
    # Provider 2: Twelve Data
    # ------------------------------------------------------------------

    def _fetch_twelve_data(
        self, symbol: str, asset_class: str, start_date: str, end_date: str, max_retries: int = 2
    ) -> List[OHLCVBar]:
        """Fetch daily OHLCV from Twelve Data free tier (no API key required)."""
        td_symbol = resolve_twelve_data(symbol, asset_class)
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": td_symbol,
            "interval": "1day",
            "start_date": start_date,
            "end_date": end_date,
            "outputsize": "5000",
            "format": "JSON",
        }

        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()

                if data.get("status") == "error":
                    logger.warning("Twelve Data error for %s: %s", td_symbol, data.get("message", ""))
                    return []

                values = data.get("values")
                if not values or not isinstance(values, list):
                    logger.warning("No values from Twelve Data for %s", td_symbol)
                    return []

                bars: List[OHLCVBar] = []
                for v in values:
                    bars.append(
                        OHLCVBar(
                            date=v["datetime"][:10],
                            open=round(float(v["open"]), 4),
                            high=round(float(v["high"]), 4),
                            low=round(float(v["low"]), 4),
                            close=round(float(v["close"]), 4),
                            volume=float(v.get("volume", 0)),
                        )
                    )
                # Twelve Data returns newest-first; reverse to chronological order
                bars.reverse()
                return bars

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning("Twelve Data rate limited for %s, retrying in %ds", td_symbol, wait)
                    time.sleep(wait)
                    continue
                logger.warning("Twelve Data HTTP error for %s: %s", td_symbol, exc)
                return []
            except Exception as exc:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                logger.warning("Twelve Data failed for %s: %s", td_symbol, exc)
                return []

        return []

    # ------------------------------------------------------------------
    # Provider 3a: CoinGecko (crypto only)
    # ------------------------------------------------------------------

    def _fetch_coingecko(
        self, symbol: str, asset_class: str, start_date: str, end_date: str, max_retries: int = 2
    ) -> List[OHLCVBar]:
        """Fetch crypto OHLCV from CoinGecko /market_chart (free, no key)."""
        if asset_class != "crypto":
            return []

        coin_id = COINGECKO_IDS.get(symbol.upper())
        if not coin_id:
            logger.warning("Unknown crypto symbol %s — no CoinGecko mapping", symbol)
            return []

        try:
            start_dt = date.fromisoformat(start_date)
            end_dt = date.fromisoformat(end_date)
        except ValueError:
            return []

        days = max(1, (end_dt - start_dt).days)
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        params = {"vs_currency": "usd", "days": str(days)}

        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)
                    resp.raise_for_status()
                    raw = resp.json()

                if not isinstance(raw, dict) or "prices" not in raw:
                    logger.warning("Unexpected CoinGecko response for %s", symbol)
                    return []

                daily: Dict[str, List[float]] = {}
                for ts_ms, price in raw.get("prices", []):
                    bar_date = date.fromtimestamp(ts_ms / 1000).isoformat()
                    if start_date <= bar_date <= end_date:
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

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning("CoinGecko rate limited for %s, retrying in %ds", symbol, wait)
                    time.sleep(wait)
                    continue
                logger.warning("CoinGecko HTTP error for %s: %s", symbol, exc)
                return []
            except Exception as exc:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                logger.warning("CoinGecko failed for %s: %s", symbol, exc)
                return []

        return []

    # ------------------------------------------------------------------
    # Provider 3b: Alpha Vantage (non-crypto, optional API key)
    # ------------------------------------------------------------------

    def _fetch_alphavantage(
        self, symbol: str, asset_class: str, start_date: str, end_date: str
    ) -> List[OHLCVBar]:
        """Fetch daily OHLCV from Alpha Vantage (requires ALPHA_VANTAGE_API_KEY)."""
        if not _ALPHA_VANTAGE_API_KEY:
            return []

        base_url = "https://www.alphavantage.co/query"

        if asset_class == "forex":
            from_sym, to_sym = resolve_alphavantage_forex(symbol)
            params = {
                "function": "FX_DAILY",
                "from_symbol": from_sym,
                "to_symbol": to_sym,
                "outputsize": "full",
                "apikey": _ALPHA_VANTAGE_API_KEY,
            }
            ts_key = "Time Series FX (Daily)"
        elif asset_class == "crypto":
            params = {
                "function": "DIGITAL_CURRENCY_DAILY",
                "symbol": symbol.upper(),
                "market": "USD",
                "apikey": _ALPHA_VANTAGE_API_KEY,
            }
            ts_key = "Time Series (Digital Currency Daily)"
        else:
            av_symbol = resolve_alphavantage_stock(symbol)
            params = {
                "function": "TIME_SERIES_DAILY",
                "symbol": av_symbol,
                "outputsize": "full",
                "apikey": _ALPHA_VANTAGE_API_KEY,
            }
            ts_key = "Time Series (Daily)"

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(base_url, params=params)
                resp.raise_for_status()
                data = resp.json()

            if "Error Message" in data or "Note" in data:
                msg = data.get("Error Message") or data.get("Note", "")
                logger.warning("Alpha Vantage error for %s: %s", symbol, msg[:200])
                return []

            ts = data.get(ts_key)
            if not ts or not isinstance(ts, dict):
                logger.warning("No time series from Alpha Vantage for %s (key=%s)", symbol, ts_key)
                return []

            bars: List[OHLCVBar] = []
            for bar_date in sorted(ts):
                if bar_date < start_date or bar_date > end_date:
                    continue
                entry = ts[bar_date]
                # Alpha Vantage key names vary by endpoint; try common patterns
                bars.append(
                    OHLCVBar(
                        date=bar_date,
                        open=round(float(entry.get("1. open", entry.get("1a. open (USD)", 0))), 4),
                        high=round(float(entry.get("2. high", entry.get("2a. high (USD)", 0))), 4),
                        low=round(float(entry.get("3. low", entry.get("3a. low (USD)", 0))), 4),
                        close=round(float(entry.get("4. close", entry.get("4a. close (USD)", 0))), 4),
                        volume=float(entry.get("5. volume", entry.get("5. market cap (USD)", 0))),
                    )
                )
            return bars

        except Exception as exc:
            logger.warning("Alpha Vantage failed for %s: %s", symbol, exc)
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _df_to_bars(df: object) -> List[OHLCVBar]:
        """Convert a yfinance DataFrame to a list of OHLCVBar."""
        bars: List[OHLCVBar] = []
        for idx, row in df.iterrows():  # type: ignore[union-attr]
            bar_date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
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
