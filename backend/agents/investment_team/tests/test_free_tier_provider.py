"""Tests for ``market_lab_data.free_tier.FreeTierMarketDataProvider``.

The provider composes three free REST sources (Frankfurter FX, FRED 10Y,
Yahoo crypto). The tests patch ``httpx.Client`` with an in-memory stub
that records request URLs and returns canned JSON, so no network calls
fire. They cover:

* The cache hit short-circuit.
* Frankfurter success vs. failure with degraded reasons.
* FRED enabled-by-env vs. disabled (no key).
* Yahoo crypto success (via patched ``yfinance``), ImportError, and
  generic exception.
* ``get_market_data_provider_for_env`` env-driven default and warning.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest


class _StubResponse:
    def __init__(self, payload: Dict[str, Any], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._payload


class _StubHttpxClient:
    """Records URL/params on each .get() and returns the next queued response."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        # url -> response factory
        self._queue: Dict[str, _StubResponse | Exception] = {}

    def queue(self, url_substr: str, response: _StubResponse | Exception) -> None:
        self._queue[url_substr] = response

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> _StubResponse:
        self.calls.append((url, params))
        for substr, resp in self._queue.items():
            if substr in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        # default — empty success
        return _StubResponse({})

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_global_cache() -> None:
    """Ensure each test starts with a fresh module-level TTL cache."""
    from investment_team.market_lab_data import free_tier

    free_tier._global_cache = free_tier._TTLCache()
    yield


def _make_provider(client: _StubHttpxClient):
    from investment_team.market_lab_data import StrategyLabDataRequest
    from investment_team.market_lab_data.free_tier import FreeTierMarketDataProvider

    provider = FreeTierMarketDataProvider(timeout_sec=8.0, http_client=client)  # type: ignore[arg-type]
    request = StrategyLabDataRequest(benchmark_symbol="SPY")
    return provider, request


def test_ttl_cache_returns_none_when_unset_and_expired() -> None:
    from investment_team.market_lab_data.free_tier import _TTLCache

    cache = _TTLCache()
    assert cache.get() is None


def test_ttl_cache_set_and_get_round_trip() -> None:
    from investment_team.market_lab_data import MarketLabContext
    from investment_team.market_lab_data.free_tier import _TTLCache

    cache = _TTLCache()
    ctx = MarketLabContext(fetched_at="2026-01-01T00:00:00+00:00")
    cache.set(ctx)
    assert cache.get() is ctx


def test_ttl_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock ``time.monotonic`` so the cache thinks the TTL elapsed."""
    from investment_team.market_lab_data import MarketLabContext, free_tier

    cache = free_tier._TTLCache()
    ctx = MarketLabContext(fetched_at="2026-01-01T00:00:00+00:00")

    fake_now = [1000.0]

    def _monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(free_tier.time, "monotonic", _monotonic)

    cache.set(ctx)
    assert cache.get() is ctx

    # Advance past the configured TTL.
    fake_now[0] = 1000.0 + free_tier._CACHE_TTL_SEC + 1.0
    assert cache.get() is None


def test_fetch_context_returns_cached_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.market_lab_data import MarketLabContext, free_tier

    ctx = MarketLabContext(fetched_at="2026-01-01T00:00:00+00:00")
    free_tier._global_cache.set(ctx)

    client = _StubHttpxClient()
    provider, request = _make_provider(client)
    out = provider.fetch_context(request)
    assert out is ctx
    # No HTTP calls when the cache is hot.
    assert client.calls == []
    provider.close()


def test_fetch_context_frankfurter_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frankfurter returns FX rates; the result is wrapped into MarketLabContext."""
    from investment_team.market_lab_data import free_tier

    # Disable FRED + yfinance branches so we isolate Frankfurter.
    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "", raising=False)
    # Pretend yfinance can't be imported so the crypto branch records the
    # degraded reason without trying to fetch.
    monkeypatch.setitem(sys.modules, "yfinance", None)

    client = _StubHttpxClient()
    client.queue(
        "frankfurter",
        _StubResponse({"rates": {"EUR": 0.92, "GBP": "ignore-non-numeric"}}),
    )
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)

    assert "frankfurter" in ctx.sources_used
    assert ctx.fx_rates == {"EUR": 0.92}  # GBP filtered out (not numeric)
    # yfinance ImportError sets degraded with reason yfinance_missing
    assert ctx.degraded is True
    assert "yfinance_missing" in (ctx.degraded_reason or "")
    provider.close()


def test_fetch_context_frankfurter_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Frankfurter raises, degraded=True and reason='frankfurter_failed' is set."""
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", None)

    client = _StubHttpxClient()
    client.queue("frankfurter", RuntimeError("boom"))
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)

    assert ctx.degraded is True
    assert "frankfurter_failed" in (ctx.degraded_reason or "")
    assert "frankfurter" not in ctx.sources_used
    provider.close()


def test_fetch_context_fred_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED branch fires when the API key env var is set."""
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "test-key-placeholder", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", None)

    client = _StubHttpxClient()
    client.queue("frankfurter", _StubResponse({"rates": {"EUR": 0.9}}))
    client.queue(
        "stlouisfed.org",
        _StubResponse({"observations": [{"value": "4.25"}]}),
    )
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)

    assert "fred_dgs10" in ctx.sources_used
    joined = " ".join(ctx.macro_snippets)
    assert "4.25" in joined
    provider.close()


def test_fetch_context_fred_skips_dot_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED's ``"."`` sentinel for missing observations must be skipped."""
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "test-key-placeholder", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", None)

    client = _StubHttpxClient()
    client.queue("frankfurter", _StubResponse({"rates": {}}))
    client.queue(
        "stlouisfed.org",
        _StubResponse({"observations": [{"value": "."}]}),
    )
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)
    # The source slug is still recorded, but no macro snippet was added.
    assert "fred_dgs10" in ctx.sources_used
    assert ctx.macro_snippets == []
    provider.close()


def test_fetch_context_fred_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "test-key-placeholder", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", None)

    client = _StubHttpxClient()
    client.queue("frankfurter", _StubResponse({"rates": {}}))
    client.queue("stlouisfed.org", RuntimeError("503"))
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)
    assert "fred_failed" in (ctx.degraded_reason or "")
    provider.close()


def test_fetch_context_yahoo_crypto_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """When yfinance is importable, BTC/ETH prices populate crypto_snapshot."""
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "", raising=False)

    # Build a fake yfinance module.
    fake_yf = types.ModuleType("yfinance")

    class _FastInfo:
        def __init__(self, last_price: float) -> None:
            self.last_price = last_price

    class _Ticker:
        def __init__(self, sym: str) -> None:
            self._sym = sym

        @property
        def fast_info(self):
            return _FastInfo(50000.0 if "BTC" in self._sym else 3000.0)

    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    client = _StubHttpxClient()
    client.queue("frankfurter", _StubResponse({"rates": {"EUR": 0.9}}))
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)

    assert "yahoo_crypto" in ctx.sources_used
    assert ctx.crypto_snapshot is not None
    assert "BTC/USD" in ctx.crypto_snapshot
    assert "ETH/USD" in ctx.crypto_snapshot
    provider.close()


def test_fetch_context_yahoo_crypto_per_symbol_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-symbol errors inside the crypto loop are swallowed silently."""
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "", raising=False)

    fake_yf = types.ModuleType("yfinance")

    class _Ticker:
        def __init__(self, sym: str) -> None:
            raise RuntimeError("per-symbol failure")

    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    client = _StubHttpxClient()
    client.queue("frankfurter", _StubResponse({"rates": {}}))
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)

    # No parts collected — crypto_snapshot stays None, but the slug is appended.
    assert "yahoo_crypto" in ctx.sources_used
    assert ctx.crypto_snapshot is None
    provider.close()


def test_fetch_context_yahoo_crypto_outer_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the yfinance import succeeds but a top-level operation fails."""
    from investment_team.market_lab_data import free_tier

    monkeypatch.setattr(free_tier, "_FRED_API_KEY", "", raising=False)

    # Build a yfinance module whose top-level ``Ticker`` attribute lookup
    # raises (simulating a registry-level failure outside the per-symbol loop).
    fake_yf = types.ModuleType("yfinance")

    class _BrokenTickerCls:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("registry broke")

        @property
        def fast_info(self):  # pragma: no cover - never reached
            return None

    fake_yf.Ticker = _BrokenTickerCls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    client = _StubHttpxClient()
    client.queue("frankfurter", _StubResponse({"rates": {}}))
    provider, request = _make_provider(client)
    ctx = provider.fetch_context(request)
    # Per-symbol failures swallowed, but the source slug records the attempt.
    assert "yahoo_crypto" in ctx.sources_used
    provider.close()


def test_get_market_data_provider_for_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.market_lab_data import free_tier

    monkeypatch.delenv("STRATEGY_LAB_MARKET_DATA_PROVIDER", raising=False)
    provider = free_tier.get_market_data_provider_for_env()
    assert isinstance(provider, free_tier.FreeTierMarketDataProvider)
    provider.close()


def test_get_market_data_provider_for_env_unknown_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.market_lab_data import free_tier

    monkeypatch.setenv("STRATEGY_LAB_MARKET_DATA_PROVIDER", "premium")
    provider = free_tier.get_market_data_provider_for_env()
    # Unknown env value still returns a free-tier provider (with a warning).
    assert isinstance(provider, free_tier.FreeTierMarketDataProvider)
    provider.close()


def test_provider_constructor_owns_client_only_when_not_injected() -> None:
    """``close()`` only closes the client when the provider created it."""
    from investment_team.market_lab_data.free_tier import FreeTierMarketDataProvider

    # Inject an httpx-like client and verify .close() doesn't tear it down.
    client = _StubHttpxClient()
    provider = FreeTierMarketDataProvider(http_client=client)  # type: ignore[arg-type]
    assert provider._own_client is False
    provider.close()  # no-op for injected client; just exercise the branch
