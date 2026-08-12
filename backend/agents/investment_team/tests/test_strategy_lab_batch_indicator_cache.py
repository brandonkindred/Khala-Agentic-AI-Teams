"""Unit tests for the batch-scoped :class:`BatchIndicatorCache`.

The cache memoizes indicator computations on ``(indicator_name, params,
symbol, timeframe, bars)`` per the cache-key/invalidation contract in
``system_design/adr/ADR-012-batch-indicator-cache-key-and-invalidation.md``.
These tests pin key sensitivity to every component, hit/miss accounting, the
precondition guards, and (per the ADR's explicit concurrency requirement)
that concurrent access on shared and distinct keys never tears cache state.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Tuple

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.strategy_lab.batch_indicator_cache import BatchIndicatorCache


def _bars(close: float = 100.0) -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date="2023-01-02",
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000_000,
        ),
        OHLCVBar(
            date="2023-01-03",
            open=close + 1,
            high=close + 2,
            low=close,
            close=close + 1,
            volume=1_100_000,
        ),
    ]


def _counting_compute(value: Any = "computed") -> Tuple[List[int], Callable[[], Any]]:
    """A ``compute``-shaped stub that records each invocation."""
    calls: List[int] = []

    def _compute() -> Any:
        calls.append(len(calls) + 1)
        return value

    return calls, _compute


def test_identical_inputs_hit_after_first_call() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    first, hit1 = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)
    second, hit2 = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)

    assert hit1 is False and hit2 is True
    assert len(calls) == 1  # compute invoked exactly once
    assert second == first
    assert cache.hits == 1 and cache.misses == 1


def test_different_indicator_name_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)
    _, hit = cache.get_or_compute("ema", {"period": 50}, "AAPL", "1d", bars, compute=compute)

    assert hit is False
    assert len(calls) == 2


def test_different_params_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)
    _, hit = cache.get_or_compute("sma", {"period": 20}, "AAPL", "1d", bars, compute=compute)

    assert hit is False
    assert len(calls) == 2


def test_identical_float_params_still_hit() -> None:
    """A float param like Bollinger's ``num_std`` must hash stably across
    calls so the same value doesn't spuriously miss."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute(
        "bollinger_bands", {"period": 20, "num_std": 2.0}, "AAPL", "1d", bars, compute=compute
    )
    _, hit = cache.get_or_compute(
        "bollinger_bands", {"period": 20, "num_std": 2.0}, "AAPL", "1d", bars, compute=compute
    )

    assert hit is True
    assert len(calls) == 1


def test_different_symbol_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)
    _, hit = cache.get_or_compute("sma", {"period": 50}, "MSFT", "1d", bars, compute=compute)

    assert hit is False
    assert len(calls) == 2


def test_different_timeframe_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()
    bars = _bars()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)
    _, hit = cache.get_or_compute("sma", {"period": 50}, "AAPL", "1h", bars, compute=compute)

    assert hit is False
    assert len(calls) == 2


def test_different_bar_content_is_a_miss() -> None:
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(close=100.0), compute=compute)
    _, hit = cache.get_or_compute(
        "sma", {"period": 50}, "AAPL", "1d", _bars(close=200.0), compute=compute
    )

    assert hit is False
    assert len(calls) == 2


def test_same_content_different_bars_object_still_hits() -> None:
    """Equal-by-value bars fingerprint identically even as a fresh list
    object, so the second call hits without recomputing."""
    cache = BatchIndicatorCache()
    calls, compute = _counting_compute()

    cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", _bars(close=123.0), compute=compute)
    _, hit = cache.get_or_compute(
        "sma", {"period": 50}, "AAPL", "1d", _bars(close=123.0), compute=compute
    )

    assert hit is True
    assert len(calls) == 1


def test_empty_indicator_name_violates_precondition() -> None:
    cache = BatchIndicatorCache()
    _, compute = _counting_compute()
    with pytest.raises(AssertionError):
        cache.get_or_compute("", {"period": 50}, "AAPL", "1d", _bars(), compute=compute)


def test_empty_symbol_violates_precondition() -> None:
    cache = BatchIndicatorCache()
    _, compute = _counting_compute()
    with pytest.raises(AssertionError):
        cache.get_or_compute("sma", {"period": 50}, "", "1d", _bars(), compute=compute)


def test_empty_timeframe_violates_precondition() -> None:
    cache = BatchIndicatorCache()
    _, compute = _counting_compute()
    with pytest.raises(AssertionError):
        cache.get_or_compute("sma", {"period": 50}, "AAPL", "", _bars(), compute=compute)


def test_empty_bars_violates_precondition() -> None:
    cache = BatchIndicatorCache()
    _, compute = _counting_compute()
    with pytest.raises(AssertionError):
        cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", [], compute=compute)


def test_concurrent_gets_on_same_key_never_tear_state() -> None:
    """Many threads racing on the identical key must all observe the same
    stored value (no torn read), and every call is accounted as a hit or a
    miss — a redundant concurrent compute is tolerated, but the cache must
    still converge on one canonical value per key."""
    cache = BatchIndicatorCache()
    bars = _bars()
    lock = threading.Lock()
    compute_count = [0]

    def compute() -> Any:
        with lock:
            compute_count[0] += 1
            n = compute_count[0]
        time.sleep(0.01)  # widen the race window
        return f"value-{n}"

    def call() -> Tuple[Any, bool]:
        return cache.get_or_compute("sma", {"period": 50}, "AAPL", "1d", bars, compute=compute)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: call(), range(32)))

    values = {value for value, _ in results}
    assert len(values) == 1  # every caller observed the same canonical value
    assert cache.hits + cache.misses == 32
    assert cache.misses >= 1
    assert any(hit for _, hit in results)  # at least one caller saw a hit


def test_concurrent_misses_on_distinct_keys_all_execute() -> None:
    """Distinct keys accessed concurrently do not interfere with each other:
    every key gets its own computed value and the cache ends up with one
    entry per key."""
    cache = BatchIndicatorCache()
    bars = _bars()

    def call(period: int) -> Tuple[Any, bool]:
        return cache.get_or_compute(
            "sma", {"period": period}, "AAPL", "1d", bars, compute=lambda: period
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(call, range(32)))

    assert all(hit is False for _, hit in results)
    assert sorted(value for value, _ in results) == list(range(32))
    assert cache.misses == 32 and cache.hits == 0
