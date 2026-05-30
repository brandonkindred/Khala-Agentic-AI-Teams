"""More coverage for ``market_data_service.MarketDataService``.

Targets the small helpers that the first round of tests didn't reach:
* ``avg_dollar_volume_20d`` — happy + empty branches.
* ``fetch_ohlcv`` → routes to ``fetch_ohlcv_range``.
* ``fetch_multi_symbol`` → wraps ``fetch_multi_symbol_range``.
* ``fetch_multi_symbol_range`` with intraday + quality-report side-effects.
* The cache property returning the injected cache.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from investment_team.market_data_service import MarketDataService, OHLCVBar


class _StubCache:
    """Stand-in for ``MarketDataCache`` with controllable returns."""

    def __init__(self) -> None:
        self.get_or_fetch_calls: List[Dict[str, Any]] = []
        self.get_or_fetch_multi_calls: List[Dict[str, Any]] = []
        self.adv_calls: List[Dict[str, Any]] = []
        self._single_return: Tuple[List[OHLCVBar], Any] = ([], None)
        self._multi_return: Dict[str, Tuple[List[OHLCVBar], Any]] = {}
        self._adv: Optional[float] = 1_000_000.0

    def set_single(self, bars: List[OHLCVBar], meta: Any = None) -> None:
        self._single_return = (bars, meta)

    def set_multi(self, mapping: Dict[str, Tuple[List[OHLCVBar], Any]]) -> None:
        self._multi_return = dict(mapping)

    def get_or_fetch(self, *, symbol, asset_class, frequency, start, end, fetch_fn, as_of=None):
        self.get_or_fetch_calls.append(
            {"symbol": symbol, "asset_class": asset_class, "frequency": frequency,
             "start": start, "end": end, "as_of": as_of}
        )
        return self._single_return

    def get_or_fetch_multi(self, *, symbols, asset_class, frequency, start, end, fetch_fn, as_of=None):
        self.get_or_fetch_multi_calls.append({"symbols": symbols, "as_of": as_of})
        return {s: self._multi_return.get(s, ([], None)) for s in symbols}

    def adv_for_bars(self, *, bars, lookback):
        self.adv_calls.append({"bars": bars, "lookback": lookback})
        return self._adv


class _StubMeta:
    def __init__(self, provider: str = "yahoo") -> None:
        self.provider = provider


@pytest.fixture
def svc_with_stub_cache() -> tuple[MarketDataService, _StubCache]:
    cache = _StubCache()
    svc = MarketDataService(cache=cache)
    return svc, cache


def _bar(date_str: str, *, close: float = 100.0, volume: float = 1_000_000.0) -> OHLCVBar:
    return OHLCVBar(date=date_str, open=close, high=close + 0.5, low=close - 0.5, close=close, volume=volume)


# ---------------------------------------------------------------------------
# Cache property
# ---------------------------------------------------------------------------


def test_service_returns_injected_cache(svc_with_stub_cache) -> None:
    svc, cache = svc_with_stub_cache
    assert svc.cache is cache


# ---------------------------------------------------------------------------
# avg_dollar_volume_20d
# ---------------------------------------------------------------------------


def test_avg_dollar_volume_20d_returns_none_when_no_bars(svc_with_stub_cache) -> None:
    svc, _cache = svc_with_stub_cache
    assert svc.avg_dollar_volume_20d("AAA", "stocks", as_of="2024-06-01") is None


def test_avg_dollar_volume_20d_dispatches_to_cache_adv(svc_with_stub_cache) -> None:
    svc, cache = svc_with_stub_cache
    cache.set_single([_bar("2024-06-01")], meta=_StubMeta("yahoo"))
    out = svc.avg_dollar_volume_20d("AAA", "stocks", as_of="2024-06-01", lookback=10)
    assert out == 1_000_000.0
    # ADV call routes through the cache.
    assert cache.adv_calls and cache.adv_calls[-1]["lookback"] == 10


# ---------------------------------------------------------------------------
# fetch_ohlcv / fetch_multi_symbol delegation
# ---------------------------------------------------------------------------


def test_fetch_ohlcv_delegates_to_ohlcv_range(svc_with_stub_cache) -> None:
    svc, cache = svc_with_stub_cache
    cache.set_single([_bar("2024-06-01")], meta=_StubMeta("yahoo"))
    bars = svc.fetch_ohlcv("AAA", "stocks", days=30)
    assert len(bars) == 1
    assert cache.get_or_fetch_calls
    # provider_used populated via the meta path.
    assert svc.provider_used.get("AAA") == "yahoo"


def test_fetch_multi_symbol_delegates_to_multi_range(svc_with_stub_cache) -> None:
    svc, cache = svc_with_stub_cache
    cache.set_multi({
        "AAA": ([_bar("2024-06-01")], _StubMeta("yahoo")),
        "BBB": ([], None),  # filtered out
    })
    out = svc.fetch_multi_symbol(["AAA", "BBB"], "stocks", days=30)
    assert "AAA" in out
    assert "BBB" not in out


# ---------------------------------------------------------------------------
# fetch_multi_symbol_range intraday-mode side effect
# ---------------------------------------------------------------------------


def test_fetch_multi_symbol_range_runs_intraday_guard_when_enabled(
    monkeypatch: pytest.MonkeyPatch, svc_with_stub_cache
) -> None:
    svc, cache = svc_with_stub_cache
    cache.set_multi({"AAA": ([_bar("2024-06-01")], _StubMeta("yahoo"))})

    # Stub the intraday guard to record invocation rather than raise.
    calls = {}

    def _fake_check(*, intraday_mode, provider_used):
        calls["intraday_mode"] = intraday_mode
        calls["provider_used"] = dict(provider_used)

    import investment_team.execution.intraday_guard as guard_mod

    monkeypatch.setattr(guard_mod, "check_intraday_data_source", _fake_check)

    out = svc.fetch_multi_symbol_range(
        ["AAA"], "stocks", "2024-05-01", "2024-06-01", intraday_mode=True
    )
    assert "AAA" in out
    assert calls["intraday_mode"] is True
    assert calls["provider_used"]["AAA"] == "yahoo"


def test_fetch_multi_symbol_range_populates_last_quality_report(
    monkeypatch: pytest.MonkeyPatch, svc_with_stub_cache
) -> None:
    svc, cache = svc_with_stub_cache
    cache.set_multi({"AAA": ([_bar("2024-06-01")], _StubMeta("yahoo"))})

    # Stub validate_market_data so the test doesn't depend on its real logic.
    import investment_team.execution.data_quality as dq

    sentinel = object()
    monkeypatch.setattr(
        dq, "validate_market_data", lambda **kwargs: sentinel
    )

    out = svc.fetch_multi_symbol_range(["AAA"], "stocks", "2024-05-01", "2024-06-01")
    assert "AAA" in out
    assert svc.last_quality_report is sentinel


# ---------------------------------------------------------------------------
# Crypto -USD cache-key normalization (entry points)
# ---------------------------------------------------------------------------


def test_fetch_ohlcv_range_normalizes_cache_key_but_keeps_caller_symbol(
    svc_with_stub_cache,
) -> None:
    """``BTC-USD`` keys the cache as canonical ``BTC`` while ``provider_used``
    still answers to the caller's original spelling."""
    svc, cache = svc_with_stub_cache
    cache.set_single([_bar("2024-06-01")], meta=_StubMeta("yahoo"))

    bars = svc.fetch_ohlcv_range("BTC-USD", "crypto", "2024-05-01", "2024-06-01")
    assert len(bars) == 1
    # Cache saw the canonical bare alias, not the suffixed form.
    assert cache.get_or_fetch_calls[-1]["symbol"] == "BTC"
    # provider_used is keyed by the caller's original symbol.
    assert svc.provider_used.get("BTC-USD") == "yahoo"


def test_fetch_multi_symbol_range_dedups_crypto_aliases_to_single_fetch(
    svc_with_stub_cache,
) -> None:
    """``['BTC', 'BTC-USD']`` collapses to one canonical cache request; both
    original keys are returned, mapped to the same bars."""
    svc, cache = svc_with_stub_cache
    shared_bar = _bar("2024-06-01")
    cache.set_multi({"BTC": ([shared_bar], _StubMeta("yahoo"))})

    out = svc.fetch_multi_symbol_range(
        ["BTC", "BTC-USD"], "crypto", "2024-05-01", "2024-06-01"
    )
    # Cache requested the single deduped canonical symbol.
    assert cache.get_or_fetch_multi_calls[-1]["symbols"] == ["BTC"]
    # Both caller spellings come back, mapped to the same bars.
    assert set(out) == {"BTC", "BTC-USD"}
    assert out["BTC"] is out["BTC-USD"] is not None
    assert svc.provider_used["BTC"] == svc.provider_used["BTC-USD"] == "yahoo"
