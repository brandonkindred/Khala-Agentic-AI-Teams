"""Unit tests for the batch-scoped :class:`BatchIndicatorCache`.

The cache memoizes indicator computations on ``(indicator_name,
canonical_params, symbol, timeframe, data_fingerprint)`` per
``system_design/adr/ADR-012-batch-indicator-cache-key-and-invalidation.md``.
These tests pin the key sensitivity to each of the five components, the
hit/miss accounting, content- (not identity-) addressing, and freedom from
torn reads under concurrent access.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.strategy_lab.batch_indicator_cache import BatchIndicatorCache


def _bars(close: float = 100.0, symbol: str = "AAPL") -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date="2023-01-02",
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000_000,
        )
    ]


def _counting_compute(result: Any = "computed"):
    """A ``compute``-shaped stub that records each invocation."""
    calls: List[int] = []

    def _compute() -> Any:
        calls.append(1)
        return result

    return calls, _compute


def test_identical_key_hits_after_first_compute() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    first, hit1 = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute)
    second, hit2 = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute)

    assert hit1 is False and hit2 is True
    assert len(calls) == 1  # compute invoked exactly once
    assert second == first
    assert cache.hits == 1 and cache.misses == 1


def test_different_indicator_name_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute)
    _, hit = cache.get_or_compute("ema", {"period": 50}, "AAPL", "1d", bars, compute)

    assert hit is False
    assert len(calls) == 2


def test_different_params_is_a_miss() -> None:
    """Includes a float param (``num_std``), pinning the ADR's requirement
    that float params be canonicalized so equal values collide."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute(
        "bollinger_bands", {"period": 20, "num_std": 2.0}, "AAPL", "1d", bars, compute
    )
    _, hit = cache.get_or_compute(
        "bollinger_bands", {"period": 20, "num_std": 2.5}, "AAPL", "1d", bars, compute
    )

    assert hit is False
    assert len(calls) == 2


def test_param_key_order_is_irrelevant() -> None:
    """Dict insertion order must not change the composed key."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    params_a: Dict[str, Any] = {"fast": 12, "slow": 26, "signal": 9}
    params_b: Dict[str, Any] = {"signal": 9, "fast": 12, "slow": 26}

    cache.get_or_compute("macd", params_a, "AAPL", "1d", bars, compute)
    _, hit = cache.get_or_compute("macd", params_b, "AAPL", "1d", bars, compute)

    assert hit is True
    assert len(calls) == 1


def test_different_symbol_is_a_miss() -> None:
    """Symbol is always part of the key (unconditionally, per ADR-012's
    reconciliation with IndicatorRegistry's inconsistent per-method keying)."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(symbol="AAPL"), compute)
    _, hit = cache.get_or_compute(
        "sma", {"period": 50}, "MSFT", "1d", _bars(symbol="MSFT"), compute
    )

    assert hit is False
    assert len(calls) == 2


def test_different_timeframe_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute)
    _, hit = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1h", bars, compute)

    assert hit is False
    assert len(calls) == 2


def test_different_bar_content_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(close=100.0), compute)
    _, hit = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(close=200.0), compute)

    assert hit is False
    assert len(calls) == 2


def test_same_bar_content_different_object_still_hits() -> None:
    """Equal-by-value bars fingerprint identically even as a fresh list of
    fresh objects, so a second call with equivalent data hits without
    recomputing — content-addressed, not identity-addressed."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(close=123.0), compute)
    _, hit = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(close=123.0), compute)

    assert hit is True
    assert len(calls) == 1


def test_none_result_is_cached_and_hits_on_second_call() -> None:
    """A ``compute`` that legitimately returns ``None`` (e.g. an indicator
    during warm-up) must still be cached — a second identical call is a hit,
    not a repeated miss that defeats the cache for warm-up-period lookups."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute(result=None)
    bars = _bars()

    first, hit1 = cache.get_or_compute("ema", {"period": 50}, "AAPL", "1d", bars, compute)
    second, hit2 = cache.get_or_compute("ema", {"period": 50}, "AAPL", "1d", bars, compute)

    assert first is None and second is None
    assert hit1 is False and hit2 is True
    assert len(calls) == 1  # compute invoked exactly once, not on every lookup
    assert cache.hits == 1 and cache.misses == 1


def test_source_must_be_folded_into_params_to_distinguish_series() -> None:
    """``params`` is the complete parameter identity — this cache has no
    dedicated ``source`` argument, so two calls that compute different series
    (same period, different source) only produce different keys when the
    caller folds ``source`` into ``params`` itself."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    # Correct usage: source is part of params, so differing sources miss.
    cache.get_or_compute("sma", {"period": 20, "source": "close"}, "AAPL", "1d", bars, compute)
    _, hit = cache.get_or_compute(
        "sma", {"period": 20, "source": "high"}, "AAPL", "1d", bars, compute
    )

    assert hit is False
    assert len(calls) == 2


def test_concurrent_get_or_compute_has_no_torn_reads() -> None:
    """Many threads racing on the same key may redundantly compute (allowed
    per ADR-012's concurrency requirement), but every observed value must be
    a complete, correctly-shaped result — never partial/corrupt — and the
    cache converges to a single stored value."""
    cache = BatchIndicatorCache()
    bars = _bars()
    n_threads = 16
    results: List[Any] = [None] * n_threads
    call_count = {"n": 0}
    call_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _compute() -> Dict[str, Any]:
        with call_lock:
            call_count["n"] += 1
            local = call_count["n"]
        return {"value": local}  # distinguishable per-call result

    def _worker(i: int) -> None:
        barrier.wait()
        value, _ = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, _compute)
        results[i] = value

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No torn/partial entries: every observed result is one of the fully-
    # formed dicts a compute() call returned.
    assert all(isinstance(r, dict) and "value" in r for r in results)
    # The cache converged: every thread ultimately observed the same value.
    assert len(set(r["value"] for r in results)) == 1
    assert cache.hits + cache.misses == n_threads
    assert cache.misses >= 1


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(indicator_name=""),
        dict(symbol=""),
        dict(timeframe=""),
        dict(bars=[]),
    ],
)
def test_empty_required_field_violates_precondition(kwargs) -> None:
    cache = BatchIndicatorCache()
    _, compute = _counting_compute()
    call_kwargs = dict(
        indicator_name="sma",
        params={"period": 50},
        symbol="AAPL",
        timeframe="1d",
        bars=_bars(),
        compute=compute,
    )
    call_kwargs.update(kwargs)

    with pytest.raises(AssertionError):
        cache.get_or_compute(**call_kwargs)
