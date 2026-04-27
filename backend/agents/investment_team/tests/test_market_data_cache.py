"""Issue #376 — point-in-time, content-hashed market-data cache.

Covers:

* ``MarketDataCache.get_or_fetch`` — populates Parquet + index, hits on
  rerun, range expansion, ``as_of`` cutoff.
* ``MarketDataCache.get_or_fetch_multi`` — parallel cache + provider
  routing, ``MARKET_DATA_FETCH_WORKERS`` env honored.
* ``MarketDataService.fetch_*`` — routes through the cache, populates
  ``provider_used`` on both hit and miss paths.
* ``CachingProviderHistoricalStream`` — second iteration replays from
  cache without invoking the provider.
* No-Postgres path: cache and service operate cleanly when
  ``POSTGRES_HOST`` is unset (in-memory index fallback).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

from investment_team.market_data_cache import MarketDataCache, SnapshotMeta
from investment_team.market_data_cache.streaming import CachingProviderHistoricalStream
from investment_team.market_data_service import MarketDataService, OHLCVBar
from investment_team.trading_service.data_stream.protocol import BarEvent, EndOfStreamEvent
from investment_team.trading_service.strategy.contract import Bar

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _bars(n: int, *, start_day: int = 1, base_close: float = 100.0) -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date=f"2024-01-{start_day + i:02d}",
            open=base_close + i,
            high=base_close + i + 1,
            low=base_close + i - 1,
            close=base_close + i + 0.5,
            volume=1_000_000.0 + 1_000.0 * i,
        )
        for i in range(n)
    ]


@pytest.fixture
def cache(tmp_path: Path) -> MarketDataCache:
    return MarketDataCache(cache_root=tmp_path)


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the in-memory index path so unit tests don't need Postgres."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)


# ---------------------------------------------------------------------------
# get_or_fetch
# ---------------------------------------------------------------------------


def test_first_fetch_populates_parquet_and_index(cache: MarketDataCache, tmp_path: Path) -> None:
    bars = _bars(5)
    calls = {"n": 0}

    def fetch(symbol, ac, start, end):
        calls["n"] += 1
        return list(bars), "yahoo"

    out, meta = cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch,
    )
    assert calls["n"] == 1
    assert len(out) == 5
    assert isinstance(meta, SnapshotMeta)
    assert meta.symbol == "AAA"
    assert meta.provider == "yahoo"
    assert meta.row_count == 5
    assert Path(meta.parquet_path).exists()
    assert meta.parquet_path.startswith(str(tmp_path))


def test_second_fetch_is_pure_cache_hit(cache: MarketDataCache) -> None:
    bars = _bars(5)
    calls = {"n": 0}

    def fetch(symbol, ac, start, end):
        calls["n"] += 1
        return list(bars), "yahoo"

    cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch,
    )
    assert calls["n"] == 1

    # A re-fetch with the same key uses a fetch_fn that would crash if
    # called — proving the provider is not invoked on a hit.
    def assert_no_call(symbol, ac, start, end):  # pragma: no cover
        raise AssertionError("fetch_fn must not be called on cache hit")

    out, meta = cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=assert_no_call,
    )
    assert len(out) == 5
    assert meta is not None


def test_as_of_cutoff_skips_newer_snapshots(cache: MarketDataCache) -> None:
    """A request with ``as_of`` strictly before any snapshot must miss."""
    bars = _bars(5)

    def fetch(symbol, ac, start, end):
        return list(bars), "yahoo"

    cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch,
    )

    # Request as of a date before any snapshot existed — must miss.
    calls = {"n": 0}

    def fetch2(symbol, ac, start, end):
        calls["n"] += 1
        return list(bars), "yahoo"

    cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch2,
        as_of="1990-01-01",
    )
    assert calls["n"] == 1, "as_of cutoff in the past must trigger a refetch"


def test_range_expansion_triggers_refetch(cache: MarketDataCache) -> None:
    """A snapshot covering Jan 1–5 cannot satisfy a request for Jan 1–10."""
    narrow = _bars(5)
    wide = _bars(10)

    def fetch_narrow(symbol, ac, start, end):
        return list(narrow), "yahoo"

    cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch_narrow,
    )

    calls = {"n": 0}

    def fetch_wide(symbol, ac, start, end):
        calls["n"] += 1
        return list(wide), "yahoo"

    out, meta = cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-10",
        fetch_fn=fetch_wide,
    )
    assert calls["n"] == 1
    assert len(out) == 10
    # Two snapshots now indexed; both files exist on disk.
    assert meta is not None
    assert meta.row_count == 10


def test_empty_fetch_is_not_recorded(cache: MarketDataCache) -> None:
    def fetch(symbol, ac, start, end):
        return [], ""

    out, meta = cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch,
    )
    assert out == []
    assert meta is None


# ---------------------------------------------------------------------------
# get_or_fetch_multi + MARKET_DATA_FETCH_WORKERS
# ---------------------------------------------------------------------------


def test_multi_fetch_parallel_and_returns_per_symbol(
    cache: MarketDataCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKET_DATA_FETCH_WORKERS", "4")
    bars = _bars(5)

    def fetch(symbol, ac, start, end):
        return list(bars), "yahoo"

    res = cache.get_or_fetch_multi(
        symbols=["AAA", "BBB", "CCC"],
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-05",
        fetch_fn=fetch,
    )
    assert sorted(res.keys()) == ["AAA", "BBB", "CCC"]
    for _, (sym_bars, meta) in res.items():
        assert len(sym_bars) == 5
        assert meta is not None and meta.provider == "yahoo"


def test_multi_fetch_workers_env_caps_at_one(
    cache: MarketDataCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MARKET_DATA_FETCH_WORKERS=1 must collapse parallelism to a single thread."""
    monkeypatch.setenv("MARKET_DATA_FETCH_WORKERS", "1")
    seen_threads: List[str] = []

    import threading

    def fetch(symbol, ac, start, end):
        seen_threads.append(threading.current_thread().name)
        return _bars(3), "yahoo"

    cache.get_or_fetch_multi(
        symbols=["AAA", "BBB", "CCC", "DDD"],
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-03",
        fetch_fn=fetch,
    )
    # Single-thread executor reuses one worker.
    assert len(set(seen_threads)) == 1


# ---------------------------------------------------------------------------
# MarketDataService integration
# ---------------------------------------------------------------------------


def test_service_routes_through_cache(
    cache: MarketDataCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first fetch hits providers; the second is byte-equal cache replay."""
    bars = _bars(5)

    def _fail(self, symbol, ac, start, end, max_retries=3):
        return []

    def _succeed(self, symbol, ac, start, end):
        return list(bars)

    monkeypatch.setattr(MarketDataService, "_fetch_yahoo", _succeed)
    monkeypatch.setattr(MarketDataService, "_fetch_twelve_data", _fail)
    monkeypatch.setattr(MarketDataService, "_fetch_coingecko", _fail)

    svc = MarketDataService(cache=cache)

    out1 = svc.fetch_ohlcv_range("AAA", "stocks", "2024-01-01", "2024-01-05")
    assert len(out1) == 5
    assert svc.provider_used["AAA"] == "yahoo"

    # Second call: provider monkeypatched to crash if invoked.
    def _crash(self, symbol, ac, start, end):  # pragma: no cover
        raise AssertionError("provider must not be called on cache hit")

    monkeypatch.setattr(MarketDataService, "_fetch_yahoo", _crash)
    out2 = svc.fetch_ohlcv_range("AAA", "stocks", "2024-01-01", "2024-01-05")
    assert [b.close for b in out2] == [b.close for b in out1]


def test_service_multi_populates_provider_used_on_cache_hit(
    cache: MarketDataCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = _bars(3)

    def _fail(self, symbol, ac, start, end, max_retries=3):
        return []

    def _ok(self, symbol, ac, start, end):
        return list(bars)

    monkeypatch.setattr(MarketDataService, "_fetch_yahoo", _ok)
    monkeypatch.setattr(MarketDataService, "_fetch_twelve_data", _fail)
    monkeypatch.setattr(MarketDataService, "_fetch_coingecko", _fail)

    svc = MarketDataService(cache=cache)
    svc.fetch_multi_symbol_range(["AAA"], "stocks", "2024-01-01", "2024-01-03")
    assert svc.provider_used["AAA"] == "yahoo"

    # Fresh service instance, same cache — provider_used must still
    # populate from the cache hit path.
    svc2 = MarketDataService(cache=cache)
    out = svc2.fetch_multi_symbol_range(["AAA"], "stocks", "2024-01-01", "2024-01-03")
    assert "AAA" in out
    assert svc2.provider_used["AAA"] == "yahoo"


# ---------------------------------------------------------------------------
# CachingProviderHistoricalStream
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Minimal historical-only provider for stream tests."""

    def __init__(self, name: str = "fakeprov") -> None:
        self.capabilities = type("C", (), {"name": name})()
        self.calls = 0

    def historical(
        self,
        *,
        symbols,
        asset_class,
        start,
        end,
        timeframe,
    ) -> Iterator[BarEvent]:
        self.calls += 1
        for sym in symbols:
            for d in range(3):
                yield BarEvent(
                    bar=Bar(
                        symbol=sym,
                        timestamp=f"2024-01-0{d + 1}",
                        timeframe=timeframe,
                        open=100.0 + d,
                        high=101.0 + d,
                        low=99.0 + d,
                        close=100.5 + d,
                        volume=1000.0,
                    )
                )


def test_caching_stream_replays_on_second_iteration(cache: MarketDataCache) -> None:
    provider = _FakeProvider()
    stream1 = CachingProviderHistoricalStream(
        provider=provider,
        symbols=["AAA", "BBB"],
        asset_class="stocks",
        start="2024-01-01",
        end="2024-01-03",
        timeframe="1d",
        cache=cache,
    )
    events1 = list(stream1)
    assert provider.calls == 1
    assert isinstance(events1[-1], EndOfStreamEvent)
    bar_count_1 = sum(1 for e in events1 if isinstance(e, BarEvent))
    fp1 = stream1.dataset_fingerprint
    assert fp1 is not None

    stream2 = CachingProviderHistoricalStream(
        provider=provider,
        symbols=["AAA", "BBB"],
        asset_class="stocks",
        start="2024-01-01",
        end="2024-01-03",
        timeframe="1d",
        cache=cache,
    )
    events2 = list(stream2)
    # Provider not invoked again — call counter unchanged.
    assert provider.calls == 1
    assert stream2.cache_hit is True
    bar_count_2 = sum(1 for e in events2 if isinstance(e, BarEvent))
    assert bar_count_2 == bar_count_1
    assert stream2.dataset_fingerprint == fp1


def test_caching_stream_partial_cache_falls_through_to_provider(
    cache: MarketDataCache,
) -> None:
    provider = _FakeProvider()
    # Warm the cache for AAA only.
    list(
        CachingProviderHistoricalStream(
            provider=provider,
            symbols=["AAA"],
            asset_class="stocks",
            start="2024-01-01",
            end="2024-01-03",
            timeframe="1d",
            cache=cache,
        )
    )
    assert provider.calls == 1

    # Request two symbols — partial coverage means the wrapper falls
    # through to the provider for the full set rather than risk merging
    # bars from inconsistent fetch dates.
    stream = CachingProviderHistoricalStream(
        provider=provider,
        symbols=["AAA", "BBB"],
        asset_class="stocks",
        start="2024-01-01",
        end="2024-01-03",
        timeframe="1d",
        cache=cache,
    )
    list(stream)
    assert provider.calls == 2
    assert stream.cache_hit is False
    assert stream.dataset_fingerprint is not None


# ---------------------------------------------------------------------------
# Postgres-disabled path: SnapshotMeta still round-trips in memory.
# ---------------------------------------------------------------------------


def test_snapshot_meta_round_trip_in_memory_index(cache: MarketDataCache) -> None:
    bars = _bars(2)

    def fetch(symbol, ac, start, end):
        return list(bars), "yahoo"

    cache.get_or_fetch(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-02",
        fetch_fn=fetch,
    )
    meta = cache.lookup_snapshot(
        symbol="AAA",
        asset_class="stocks",
        frequency="1d",
        start="2024-01-01",
        end="2024-01-02",
    )
    assert meta is not None
    assert meta.symbol == "AAA"
    assert meta.fetch_ts.tzinfo is not None
    # Comparable as a UTC datetime.
    assert meta.fetch_ts <= datetime.now(tz=timezone.utc)
