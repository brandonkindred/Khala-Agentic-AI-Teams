"""Concurrency-safety and cross-strategy-correctness tests for the batch cache.

The unit suite in ``test_strategy_lab_batch_indicator_cache.py`` already pins
the ``BatchIndicatorCache`` primitive's single-key concurrency (torn-read
freedom, race-loser accounting) and its per-component key sensitivity
*sequentially*; ``test_indicator_registry_batch_cache.py`` pins the
``IndicatorRegistry``/``resolve_indicator`` integration, also sequentially.

This module covers the combination neither does: many *parallel strategies*
within one batch — each its own ``IndicatorRegistry`` sharing a single cache
instance — reading and writing that cache **concurrently**, and confirming
that two strategies with differing specs never share a cache hit incorrectly
even under contention. It exercises three surfaces:

1. The primitive under a *many-distinct-key* write storm (not just one key),
   asserting every key resolves to its own value with no cross-key corruption.
2. Parallel registries over an overlapping+distinct spec mix, asserting each
   thread receives the value its own spec computes (no contamination) while
   overlapping specs actually share (``hits > 0``).
3. A pairwise-distinct-spec stress run, asserting no accidental sharing
   (``hits == 0``, one miss per spec) and per-spec value parity.

Correctness rests on the cache being content-addressed: a value is only ever
returned for the exact ``(indicator, params, symbol, timeframe, bars)`` key
that produced it, so a returned value matching the uncached baseline for the
caller's *own* spec is proof no other strategy's series leaked into it.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from investment_team.strategy_lab.batch_indicator_cache import BatchIndicatorCache
from investment_team.strategy_lab.indicators.streaming import (
    IndicatorRegistry,
    resolve_indicator,
)

_ENV_VAR = "STRATEGY_LAB_BATCH_INDICATOR_CACHE_ENABLED"


@dataclass
class _SymBar:
    """Bar carrying both ``symbol`` and ``date`` — the shape the batch cache
    can fingerprint (OHLCV hashed in supplied order) and ``resolve_indicator``
    will consult for. Mirrors the fixture in
    ``test_indicator_registry_batch_cache.py``."""

    symbol: str
    timestamp: str
    date: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1.0


def _series(symbol: str, n: int, seed: int = 0) -> List[_SymBar]:
    """Deterministic OHLCV series for ``symbol`` (seeded, so a given
    ``(symbol, n, seed)`` always fingerprints identically)."""
    rng = random.Random(seed)
    bars: List[_SymBar] = []
    for i in range(n):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.3
        day = f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
        bars.append(
            _SymBar(
                symbol=symbol,
                timestamp=day,
                date=day,
                open=close - 0.1,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                volume=1_000.0 + i,
            )
        )
    return bars


def _run_in_threads(targets: List[Callable[[], None]]) -> None:
    """Run each ``target`` on its own thread, started behind a common barrier
    so they enter their critical section together, and re-raise the first
    exception any of them hit (so a thread failure fails the test rather than
    being silently swallowed)."""
    barrier = threading.Barrier(len(targets))
    errors: List[BaseException] = []
    errors_lock = threading.Lock()

    def _wrapped(fn: Callable[[], None]) -> Callable[[], None]:
        def _inner() -> None:
            barrier.wait()
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                with errors_lock:
                    errors.append(exc)

        return _inner

    threads = [threading.Thread(target=_wrapped(fn)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise errors[0]


# ---------------------------------------------------------------------------
# 1. Primitive: many distinct keys written concurrently, no cross-key corruption
# ---------------------------------------------------------------------------


def test_primitive_many_distinct_keys_concurrent_no_corruption() -> None:
    """A single cache hammered from many threads across many *distinct* keys
    (distinct params) must return, for every key, exactly the value that key's
    own ``compute`` produced — never another key's — and the hit/miss ledger
    must exactly account for every call.

    The existing torn-read test races on one key; this races the write path
    across many keys simultaneously, the scenario a batch of parallel
    strategies computing different indicator params actually creates.
    """
    cache = BatchIndicatorCache()
    bars = _series("AAPL", 5)
    n_threads = 16
    periods = list(range(2, 14))  # 12 distinct keys

    def _expected(period: int) -> Dict[str, int]:
        return {"period": period, "value": period * 1000}

    # Each thread computes every key; a compute for period p always returns
    # _expected(p), so any key returning a different key's payload is corruption.
    per_thread_results: List[Dict[int, Dict[str, int]]] = [dict() for _ in range(n_threads)]

    def _worker(idx: int) -> None:
        local = per_thread_results[idx]
        for period in periods:
            value, _ = cache.get_or_compute(
                "sma",
                {"period": period},
                "AAPL",
                "1d",
                bars,
                lambda p=period: _expected(p),
            )
            local[period] = value

    _run_in_threads([lambda i=i: _worker(i) for i in range(n_threads)])

    # Every thread saw the correct, uncorrupted value for every key.
    for local in per_thread_results:
        for period in periods:
            assert local[period] == _expected(period)

    # Ledger accounting. Under contention the exact hit/miss split is not
    # deterministic: ``get_or_compute`` computes *outside* its lock and counts
    # a same-key race-loser (a call that computed but lost the store race) as a
    # miss, never a hit (the cache's documented concurrency invariant). So the
    # only interleaving-independent guarantees are that every distinct key is
    # computed at least once (>= one miss per key), that further same-key races
    # only add more misses (never hits), and that the ledger balances against
    # the call count.
    total_calls = n_threads * len(periods)
    assert cache.hits + cache.misses == total_calls
    assert len(periods) <= cache.misses <= total_calls
    assert 0 <= cache.hits <= total_calls - len(periods)

    # Sharing is still pinned deterministically: every key is now resident, so
    # a fresh *sequential* read of each must be served from the cache without
    # recomputing (a compute here would be a bug), and returns the right value.
    def _must_not_recompute() -> Dict[str, int]:
        raise AssertionError("resident key was recomputed")

    hits_before = cache.hits
    for period in periods:
        value, was_hit = cache.get_or_compute(
            "sma", {"period": period}, "AAPL", "1d", bars, _must_not_recompute
        )
        assert was_hit
        assert value == _expected(period)
    assert cache.hits == hits_before + len(periods)


# ---------------------------------------------------------------------------
# 2. Parallel strategies, overlapping + distinct specs: sharing + no contamination
# ---------------------------------------------------------------------------


def test_parallel_strategies_share_cache_without_contamination(monkeypatch) -> None:
    """Many strategies (each its own registry) running concurrently over one
    shared cache: overlapping specs must actually share (producing hits) while
    every strategy still receives the value *its own* spec computes, verified
    against an uncached baseline. Under concurrency, no strategy's series may
    leak into another's."""
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()

    aapl = _series("AAPL", 60, seed=1)
    msft = _series("MSFT", 60, seed=2)

    # (name, params, bars) — several specs are assigned to more than one
    # strategy below, so the cache should serve later duplicates as hits.
    Spec = Tuple[str, Dict[str, Any], List[_SymBar]]
    spec_sma_aapl: Spec = ("sma", {"period": 10}, aapl)
    spec_rsi_aapl: Spec = ("rsi", {"period": 14}, aapl)
    spec_macd_aapl: Spec = ("macd", {"fast": 12, "slow": 26, "signal": 9, "output": "signal"}, aapl)
    spec_sma_msft: Spec = ("sma", {"period": 10}, msft)

    distinct_specs = [spec_sma_aapl, spec_rsi_aapl, spec_macd_aapl, spec_sma_msft]

    # Two strategies per spec => overlap => the second call for each is a hit.
    strategy_specs: List[Spec] = distinct_specs + distinct_specs

    # Uncached baseline value per distinct spec (no batch cache involved).
    baseline: Dict[int, Any] = {}
    for i, (name, params, bars) in enumerate(distinct_specs):
        baseline[i] = resolve_indicator(IndicatorRegistry(), name, bars, **params)

    def _spec_index(spec: Spec) -> int:
        return distinct_specs.index(spec)

    results: List[Any] = [None] * len(strategy_specs)

    def _worker(idx: int, spec: Spec) -> None:
        name, params, bars = spec
        reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
        results[idx] = resolve_indicator(reg, name, bars, **params)

    _run_in_threads([lambda i=i, s=s: _worker(i, s) for i, s in enumerate(strategy_specs)])

    # Every strategy got exactly its own spec's value — no contamination.
    for idx, spec in enumerate(strategy_specs):
        assert results[idx] == baseline[_spec_index(spec)], (
            f"strategy {idx} for spec {spec[0]}{spec[1]} got {results[idx]!r}, "
            f"expected {baseline[_spec_index(spec)]!r}"
        )

    # Ledger accounting. ``get_or_compute`` computes *outside* its lock and
    # counts a same-key race-loser as a miss (never a hit), so when both
    # strategies for one spec are released together they may both miss -- the
    # exact hit/miss split is therefore not deterministic under contention.
    # Assert the interleaving-independent bounds instead: at least one miss per
    # distinct spec, further same-key races only add misses, and the ledger
    # balances against the call count.
    total_calls = len(strategy_specs)
    assert cache.hits + cache.misses == total_calls
    assert len(distinct_specs) <= cache.misses <= total_calls
    assert 0 <= cache.hits <= total_calls - len(distinct_specs)

    # Overlap sharing is pinned deterministically rather than relying on a race
    # outcome: every distinct spec is now resident, so a fresh *sequential*
    # resolve of each is served from the cache as a hit.
    hits_before = cache.hits
    for name, params, bars in distinct_specs:
        reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
        resolve_indicator(reg, name, bars, **params)
    assert cache.hits == hits_before + len(distinct_specs)


# ---------------------------------------------------------------------------
# 3. Pairwise-distinct specs under load: no accidental sharing
# ---------------------------------------------------------------------------


def test_parallel_distinct_specs_never_share_a_hit(monkeypatch) -> None:
    """Strategies whose specs differ (by symbol, param, or timeframe) run
    concurrently over one shared cache and must never collide: every call is a
    miss (nothing was shared), and each strategy receives precisely its own
    spec's uncached value.

    This is the direct concurrent analogue of the sequential
    distinct-symbol/distinct-param tests — it pins that differing specs stay
    isolated even when they race to write the same cache instance.
    """
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()

    Spec = Tuple[str, Dict[str, Any], List[_SymBar], str]
    specs: List[Spec] = [
        # Differ by param.
        ("sma", {"period": 10}, _series("AAPL", 60, seed=1), "1d"),
        ("sma", {"period": 20}, _series("AAPL", 60, seed=1), "1d"),
        ("sma", {"period": 30}, _series("AAPL", 60, seed=1), "1d"),
        # Differ by symbol (same param).
        ("sma", {"period": 10}, _series("MSFT", 60, seed=2), "1d"),
        ("sma", {"period": 10}, _series("GOOG", 60, seed=3), "1d"),
        # Differ by timeframe (same param, same symbol/bars object as row 0).
        ("rsi", {"period": 14}, _series("AAPL", 60, seed=1), "1h"),
        # Differ by indicator.
        (
            "macd",
            {"fast": 12, "slow": 26, "signal": 9, "output": "signal"},
            _series("AAPL", 60, seed=1),
            "1d",
        ),
    ]

    baseline: List[Any] = [
        resolve_indicator(IndicatorRegistry(), name, bars, **params)
        for (name, params, bars, _tf) in specs
    ]

    results: List[Any] = [None] * len(specs)

    def _worker(idx: int, spec: Spec) -> None:
        name, params, bars, timeframe = spec
        reg = IndicatorRegistry(batch_cache=cache, timeframe=timeframe)
        results[idx] = resolve_indicator(reg, name, bars, **params)

    _run_in_threads([lambda i=i, s=s: _worker(i, s) for i, s in enumerate(specs)])

    # Each strategy received exactly its own spec's uncached value.
    for idx in range(len(specs)):
        assert results[idx] == baseline[idx]

    # No two distinct specs collided onto one entry: every call missed, none hit.
    assert cache.misses == len(specs)
    assert cache.hits == 0
    assert cache.hits + cache.misses == len(specs)
