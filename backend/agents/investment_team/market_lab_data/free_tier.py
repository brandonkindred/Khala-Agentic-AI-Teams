"""
Free-tier market data: Frankfurter (FX), optional FRED macro, CoinGecko (crypto).

Uses httpx with short TTL cache and a wall-clock budget per fetch.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from .models import MarketLabContext, StrategyLabDataRequest

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = float(os.environ.get("STRATEGY_LAB_MARKET_DATA_FETCH_TIMEOUT_SEC", "8.0"))
_CACHE_TTL_SEC = float(os.environ.get("STRATEGY_LAB_MARKET_DATA_CACHE_TTL_SEC", "120.0"))
_FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()


class _TTLCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: tuple[float, MarketLabContext] | None = None

    def get(self) -> Optional[MarketLabContext]:
        with self._lock:
            if self._data is None:
                return None
            ts, ctx = self._data
            if time.monotonic() - ts > _CACHE_TTL_SEC:
                return None
            return ctx

    def set(self, ctx: MarketLabContext) -> None:
        with self._lock:
            self._data = (time.monotonic(), ctx)


_global_cache = _TTLCache()


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class FreeTierMarketDataProvider:
    """Composite free-tier REST provider."""

    def __init__(
        self,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._timeout = timeout_sec
        self._own_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_sec)

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def fetch_context(self, request: StrategyLabDataRequest) -> MarketLabContext:
        cached = _global_cache.get()
        if cached is not None:
            return cached

        logger.debug("Free-tier market fetch (benchmark=%s)", request.benchmark_symbol)

        sources: list[str] = []
        degraded = False
        reasons: list[str] = []
        fx_rates: dict[str, float] = {}
        macro_snippets: list[str] = []
        crypto_snapshot: Optional[str] = None

        deadline = time.monotonic() + self._timeout

        # 1) Frankfurter — no API key
        try:
            r = self._client.get(
                "https://api.frankfurter.app/latest",
                params={"from": "USD", "to": "EUR,GBP,JPY,CHF,CAD,AUD"},
            )
            r.raise_for_status()
            data = r.json()
            rates = data.get("rates") or {}
            for k, v in rates.items():
                if isinstance(v, (int, float)):
                    fx_rates[k] = float(v)
            sources.append("frankfurter")
        except Exception as exc:
            logger.warning("Frankfurter fetch failed: %s", exc)
            degraded = True
            reasons.append("frankfurter_failed")

        # 2) FRED DGS10 — optional free API key
        if time.monotonic() < deadline and _FRED_API_KEY:
            try:
                r2 = self._client.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": "DGS10",
                        "api_key": _FRED_API_KEY,
                        "file_type": "json",
                        "limit": 1,
                        "sort_order": "desc",
                    },
                )
                r2.raise_for_status()
                obs = (r2.json().get("observations") or [{}])[0]
                val = obs.get("value")
                if val and val != ".":
                    macro_snippets.append(f"US 10Y Treasury (DGS10) latest: {val}%")
                sources.append("fred_dgs10")
            except Exception as exc:
                logger.warning("FRED fetch failed: %s", exc)
                degraded = True
                reasons.append("fred_failed")

        # 3) CoinGecko simple price — no key, strict rate limits
        if time.monotonic() < deadline:
            try:
                r3 = self._client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin,ethereum", "vs_currencies": "usd"},
                )
                r3.raise_for_status()
                cg = r3.json()
                parts = []
                if isinstance(cg, dict):
                    btc_obj = cg.get("bitcoin")
                    eth_obj = cg.get("ethereum")
                    btc = btc_obj.get("usd") if isinstance(btc_obj, dict) else None
                    eth = eth_obj.get("usd") if isinstance(eth_obj, dict) else None
                    if btc is not None:
                        parts.append(f"BTC/USD ~ {btc:,.0f}")
                    if eth is not None:
                        parts.append(f"ETH/USD ~ {eth:,.0f}")
                if parts:
                    crypto_snapshot = " | ".join(parts)
                sources.append("coingecko_simple")
            except Exception as exc:
                logger.warning("CoinGecko fetch failed: %s", exc)
                degraded = True
                reasons.append("coingecko_failed")

        ctx = MarketLabContext(
            fetched_at=_utc_now_iso(),
            degraded=degraded,
            degraded_reason=", ".join(reasons) if reasons else None,
            sources_used=sources,
            fx_rates=fx_rates,
            macro_snippets=macro_snippets,
            crypto_snapshot=crypto_snapshot,
            social_sentiment=None,
        )
        _global_cache.set(ctx)
        return ctx


def get_market_data_provider_for_env() -> FreeTierMarketDataProvider:
    """Factory used by the investment API (default: free_tier)."""
    name = (os.environ.get("STRATEGY_LAB_MARKET_DATA_PROVIDER") or "free_tier").strip().lower()
    if name != "free_tier":
        logger.warning("Unknown STRATEGY_LAB_MARKET_DATA_PROVIDER=%r, using free_tier", name)
    return FreeTierMarketDataProvider()
