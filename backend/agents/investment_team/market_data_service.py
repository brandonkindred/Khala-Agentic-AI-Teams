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
from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Tuple

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

if TYPE_CHECKING:  # pragma: no cover
    from .market_data_cache import MarketDataCache

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


def compute_adv_from_bars(
    bars: Sequence[OHLCVBar],
    *,
    lookback: int = 20,
) -> Optional[float]:
    """Trailing-N-bar mean of ``close * volume`` (USD) from an OHLCV list.

    Pure helper — kept at module scope so unit tests and the cost-stress
    harness can compute ADV from synthetic fixtures without instantiating
    a ``MarketDataService`` or hitting the network.  Returns ``None`` when
    the series is shorter than ``lookback`` or every bar has zero volume.
    """
    if not bars or lookback <= 0:
        return None
    window = list(bars)[-lookback:]
    if len(window) < lookback:
        return None
    dollar_volume = [b.close * b.volume for b in window if b.volume > 0 and b.close > 0]
    if not dollar_volume:
        return None
    return sum(dollar_volume) / len(dollar_volume)


class MarketDataService:
    """Fetches real market data with automatic multi-source fallback.

    For each symbol the service tries providers in priority order until one
    returns data.  The chain varies by asset class:

    - **crypto**: Yahoo Finance → Twelve Data → CoinGecko
    - **stocks / options / commodities**: Yahoo Finance → Twelve Data → Alpha Vantage
    - **forex / futures**: Yahoo Finance → Twelve Data → Alpha Vantage

    Alpha Vantage is only included when ``ALPHA_VANTAGE_API_KEY`` is set.
    """

    def __init__(
        self,
        http_timeout: float = 30.0,
        *,
        cache: Optional["MarketDataCache"] = None,
    ) -> None:
        self._timeout = http_timeout
        # Phase 5 (partial): records which provider supplied bars for each
        # symbol on the most recent fetch.  Read by
        # ``execution.intraday_guard.check_intraday_data_source`` to
        # hard-fail intraday runs that fell back to CoinGecko's synthesized
        # OHLCV.  Empty dict on init; populated lazily.
        self.provider_used: Dict[str, str] = {}
        # Issue #375 — populated by ``fetch_multi_symbol_range`` after
        # every successful multi-symbol fetch.  The service-level check
        # is non-blocking (``mode='warn'``); the per-mode entry points
        # (backtest / paper trade) re-run validation with the strictness
        # appropriate to their context.  None until the first fetch.
        self.last_quality_report: Optional["object"] = None
        # Issue #376 — durable, content-hashed cache. Lazily resolved so
        # tests can construct a MarketDataService without paying the
        # cache_root() side effects until they need them.
        self._injected_cache: Optional["MarketDataCache"] = cache

    @property
    def cache(self) -> "MarketDataCache":
        if self._injected_cache is not None:
            return self._injected_cache
        from .market_data_cache import get_default_cache

        return get_default_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_ohlcv(self, symbol: str, asset_class: str, days: int = 365) -> List[OHLCVBar]:
        """Route to best data source for the asset class (recent N days)."""
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=days)
        return self.fetch_ohlcv_range(symbol, asset_class, start_dt.isoformat(), end_dt.isoformat())

    def _fetch_with_providers(
        self, symbol: str, asset_class: str, start_date: str, end_date: str
    ) -> Tuple[List[OHLCVBar], str]:
        """Run the provider chain and return the winning provider's bars + slug.

        Used both as the cache miss handler and (indirectly) as the legacy
        fetch path.  Returns ``([], "")`` when every provider is exhausted.
        """
        for slug, fetch_fn in self._get_named_provider_chain(asset_class):
            try:
                bars = fetch_fn(symbol, asset_class, start_date, end_date)
                if bars:
                    return bars, slug
            except Exception as exc:
                logger.warning("Provider failed for %s (%s): %s", symbol, asset_class, exc)
        logger.warning("All providers exhausted for %s (%s)", symbol, asset_class)
        return [], ""

    def fetch_ohlcv_range(
        self,
        symbol: str,
        asset_class: str,
        start_date: str,
        end_date: str,
        *,
        as_of: Optional[str] = None,
        frequency: str = "1d",
    ) -> List[OHLCVBar]:
        """Fetch OHLCV data for an explicit date range with automatic fallback.

        Issue #376 — every fetch routes through the durable, content-hashed
        ``MarketDataCache``; identical ``(symbol, range, as_of)`` reads
        replay byte-equal bars on subsequent calls.  ``as_of=None`` means
        "latest"; pass an ISO date / datetime to pin to a historical
        snapshot.
        """
        ac = normalize_asset_class(asset_class)
        bars, meta = self.cache.get_or_fetch(
            symbol=symbol,
            asset_class=ac,
            frequency=frequency,
            start=start_date,
            end=end_date,
            fetch_fn=self._fetch_with_providers,
            as_of=as_of,
        )
        if meta is not None:
            # Phase 5 (partial): record the winning provider so the
            # intraday-safety guard can inspect it post-fetch.  Both the
            # cache-hit and miss paths populate this so downstream
            # consumers see consistent metadata.
            self.provider_used[symbol] = meta.provider
        return bars

    # ------------------------------------------------------------------
    # Liquidity (Phase 4): average daily dollar volume over the trailing
    # 20 bars.  Consumed by ``SpreadPlusImpactCostModel`` to size the
    # market-impact term per symbol.
    # ------------------------------------------------------------------

    def avg_dollar_volume_20d(
        self,
        symbol: str,
        asset_class: str,
        *,
        as_of: Optional[str] = None,
        lookback: int = 20,
    ) -> Optional[float]:
        """Return the trailing-N-bar mean of ``close * volume`` in USD.

        Issue #376 — backed by the cache's derived-ADV layer keyed on
        ``(per-symbol-snapshot fingerprint, lookback)``.  Within a
        snapshot the result is eternally valid (no TTL), and across
        processes byte-equal Parquet content yields the same key.
        Returns ``None`` when the provider chain is exhausted — the
        cost model's impact term collapses to the flat half-spread in
        that case.
        """
        as_of_str = as_of or date.today().isoformat()
        # Pull lookback * ~1.6 calendar days so weekends / holidays leave
        # enough trading bars after filtering.  Extra bars are truncated.
        end_dt = date.fromisoformat(as_of_str[:10])
        start_dt = end_dt - timedelta(days=max(lookback * 2, 30))
        bars = self.fetch_ohlcv_range(
            symbol,
            asset_class,
            start_dt.isoformat(),
            end_dt.isoformat(),
            as_of=as_of,
        )
        if not bars:
            return None
        return self.cache.adv_for_bars(bars=bars, lookback=lookback)

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
        self,
        symbols: List[str],
        asset_class: str,
        start_date: str,
        end_date: str,
        *,
        intraday_mode: bool = False,
        as_of: Optional[str] = None,
        frequency: str = "1d",
    ) -> Dict[str, List[OHLCVBar]]:
        """Fetch OHLCV data for multiple symbols over an explicit date range.

        Issue #376 — routes through the durable cache.  Worker count is
        controlled by ``MARKET_DATA_FETCH_WORKERS`` (default
        ``min(len(symbols), 16)``); the previous hard-coded cap of 5 is
        gone.  Cache-hit symbols don't dispatch to a provider at all.

        When ``intraday_mode=True`` runs the Phase 5 intraday-safety
        guard after all symbols resolve — raises ``IntradayDataError`` if
        any symbol's bars came from a provider that's unsafe at intraday
        granularity (CoinGecko today).
        """
        ac = normalize_asset_class(asset_class)
        cache_results = self.cache.get_or_fetch_multi(
            symbols=symbols,
            asset_class=ac,
            frequency=frequency,
            start=start_date,
            end=end_date,
            fetch_fn=self._fetch_with_providers,
            as_of=as_of,
        )
        result: Dict[str, List[OHLCVBar]] = {}
        for sym, (bars, meta) in cache_results.items():
            if not bars or meta is None:
                continue
            result[sym] = bars
            self.provider_used[sym] = meta.provider

        if intraday_mode:
            from .execution.intraday_guard import check_intraday_data_source

            check_intraday_data_source(
                intraday_mode=True,
                provider_used={
                    sym: self.provider_used[sym] for sym in result if sym in self.provider_used
                },
            )

        # Issue #375 — non-blocking preflight report for callers that
        # want a structured view of data quality without re-validating.
        # Backtest and paper-trade modes re-run validation themselves
        # with the appropriate strictness; we run ``warn`` here so the
        # service stays a generic data-fetcher.
        if result:
            from .execution.data_quality import validate_market_data

            self.last_quality_report = validate_market_data(
                bars_by_symbol=result,
                expected_frequency="1d" if not intraday_mode else "unknown",
                asset_class=normalize_asset_class(asset_class),
                mode="warn",
            )
        return result

    # ------------------------------------------------------------------
    # Provider chain
    # ------------------------------------------------------------------

    def _get_provider_chain(self, asset_class: str) -> list[_FetchFn]:
        """Return an ordered list of fetch functions for the given asset class."""
        return [fn for _, fn in self._get_named_provider_chain(asset_class)]

    def _get_named_provider_chain(self, asset_class: str) -> list[tuple[str, _FetchFn]]:
        """Ordered ``(slug, fetch_fn)`` pairs for the given asset class.

        The slug is stable across monkey-patching in tests — it's what the
        intraday-safety guard inspects when deciding whether to hard-fail
        a run that fell back to an unreliable OHLCV source.
        """
        if asset_class == "crypto":
            return [
                ("yahoo", self._fetch_yahoo),
                ("twelve_data", self._fetch_twelve_data),
                ("coingecko", self._fetch_coingecko),
            ]
        chain: list[tuple[str, _FetchFn]] = [
            ("yahoo", self._fetch_yahoo),
            ("twelve_data", self._fetch_twelve_data),
        ]
        if _ALPHA_VANTAGE_API_KEY:
            chain.append(("alphavantage", self._fetch_alphavantage))
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
                        yf_symbol,
                        wait,
                        attempt + 1,
                        exc,
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    "yfinance failed for %s after %d attempts: %s", yf_symbol, max_retries, exc
                )
                return []

            if df is not None and not df.empty:
                return self._df_to_bars(df)

            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "No data from yfinance for %s, retrying in %ds (attempt %d)",
                    yf_symbol,
                    wait,
                    attempt + 1,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    "No data from yfinance for %s after %d attempts", yf_symbol, max_retries
                )

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
                    logger.warning(
                        "Twelve Data error for %s: %s", td_symbol, data.get("message", "")
                    )
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
                    logger.warning(
                        "Twelve Data rate limited for %s, retrying in %ds", td_symbol, wait
                    )
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
                        close=round(
                            float(entry.get("4. close", entry.get("4a. close (USD)", 0))), 4
                        ),
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
