"""Market data service — fetches real OHLCV price data via Yahoo Finance."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Dict, List

from pydantic import BaseModel

from .models import StrategySpec
from .strategy_lab_context import normalize_asset_class
from .symbols import (
    COMMODITY_SYMBOLS,
    CRYPTO_SYMBOLS,
    FOREX_SYMBOLS,
    FUTURES_SYMBOLS,
    STOCK_SYMBOLS,
    YAHOO_CRYPTO_TICKERS,
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
    """Fetches real market data from Yahoo Finance for all asset classes.

    Crypto symbols are mapped to their Yahoo Finance ``-USD`` tickers
    (e.g. BTC → BTC-USD) via :data:`YAHOO_CRYPTO_TICKERS`.
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
            yf_ticker = YAHOO_CRYPTO_TICKERS.get(symbol.upper(), f"{symbol.upper()}-USD")
            return self._fetch_yahoo(yf_ticker, start_date, end_date)
        return self._fetch_yahoo(symbol, start_date, end_date)

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
    # Internal: Yahoo Finance (all asset classes)
    # ------------------------------------------------------------------

    def _fetch_yahoo(
        self, symbol: str, start_date: str, end_date: str, max_retries: int = 3
    ) -> List[OHLCVBar]:
        """Fetch OHLCV data via yfinance for an arbitrary date range.

        Handles stocks, ETFs, forex (=X suffix), futures (=F suffix),
        and crypto (-USD suffix). Retries with exponential backoff on
        transient failures.
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
