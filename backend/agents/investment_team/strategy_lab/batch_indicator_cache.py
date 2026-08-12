"""Batch-scoped memoization of indicator computations across strategy candidates.

A Strategy Lab batch backtest evaluates many candidate strategies ("cycles")
against the same asset-class universe, timeframe, and date range in one batch
run. Each cycle computes its own indicator series from scratch even when two
different candidates both use, say, ``SMA(50)`` on ``AAPL``/``1d`` over the
same date range — nothing today shares that computation across cycles in the
same batch, so structurally identical indicator work is repeated once per
candidate.

:class:`BatchIndicatorCache` closes that gap: a batch-scoped, thread-safe
get-or-compute cache keyed on ``(indicator_name, canonical_params, symbol,
timeframe, data_fingerprint)``, per the cache-key and invalidation contract
documented in ``system_design/adr/ADR-012-batch-indicator-cache-key-and-invalidation.md``.

This module is purely additive: it is not yet consulted by ``IndicatorRegistry``
or constructed/shared by the batch Temporal workflow. Wiring it into either of
those call sites is separate follow-on work; this module only needs to exist,
behave correctly, and be unit-tested against the ADR's contract.

Reused building blocks:
  * :func:`..market_data_cache.store._hash_bars` — the per-symbol content
    fingerprint this cache's ``data_fingerprint`` component reuses directly
    (not the multi-symbol :func:`..market_data_cache.store.compute_dataset_fingerprint`
    wrapper — the ADR calls out that a batch cache entry is scoped to one
    symbol's bars, so the narrower per-symbol leg is the correct primitive).
  * :class:`..backtest_cache.BacktestCache` — the existing per-design-attempt
    whole-result cache this module's key-hashing and get-or-compute shape is
    modeled on (single running ``sha256()`` digest over ``\\x00``-separated
    canonicalized components; ``(value, hit)`` return shape).
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from ..market_data_cache.store import _hash_bars
from ..market_data_service import OHLCVBar


def _canonical_params(params: Mapping[str, Any]) -> str:
    """Canonical-JSON serialization of indicator params for stable hashing.

    Mirrors ``BacktestCache._config_hash``'s float-safe canonicalization
    (fixed key order, no whitespace) so that, e.g., a Bollinger Bands
    ``num_std`` or Keltner ``multiplier`` float hashes identically across
    calls regardless of dict construction order.

    Preconditions:
      - ``params`` is a mapping of JSON-serializable values (the indicator's
        tunable arguments, e.g. ``period``, ``source``, ``fast``/``slow``).
    Postconditions:
      - Returns a string that is identical for any two mappings with the same
        key/value pairs, and different whenever a key or value differs.
    Invariants:
      - Pure: no side effects, no I/O.
    """
    return json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str)


class BatchIndicatorCache:
    """Memoize indicator-series computations shared across one batch's cycles.

    Not yet wired into any production code path: no indicator computation or
    workflow code constructs or consults this cache today. It is additive,
    standalone, and safe to add without changing any existing behavior.

    Invariants:
      - Batch-scoped: callers must construct a fresh instance per batch and
        discard it when the batch ends — an instance must never be reused
        across batches or across unrelated runs. Entries are never actively
        evicted within the cache's lifetime; because every key is
        content-addressed, a batch-scoped instance can only be invalidated by
        ceasing to exist.
      - Thread-safe: concurrent :meth:`get_or_compute` calls (from concurrent
        cycle execution within a batch) never observe a torn or half-written
        entry. Two callers racing on the same key may both invoke ``compute``
        (a redundant computation, not a correctness issue), but only one
        computed value is ever stored and returned for a given key afterward.
      - ``hits + misses`` equals the number of :meth:`get_or_compute` calls.
    """

    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}
        # ``id(bars) -> fingerprint`` so the O(len(bars)) hash is paid once
        # per distinct bars object instead of on every lookup.
        self._fingerprint_by_id: Dict[int, str] = {}
        # Hold a reference to each fingerprinted object so its ``id()`` cannot
        # be recycled by the allocator mid-batch (which would alias a stale
        # fingerprint onto a different bars object).
        self._fingerprinted_refs: List[Any] = []
        self._lock = threading.Lock()
        self.hits: int = 0
        self.misses: int = 0

    def _bars_fingerprint(self, bars: Sequence[OHLCVBar]) -> str:
        """Content fingerprint of ``bars``, memoized by object identity.

        Preconditions:
          - ``bars`` is a non-empty sequence of ``OHLCVBar`` for a single
            symbol.
        Postconditions:
          - Returns the same fingerprint for any two bars objects with equal
            content, and a different fingerprint whenever the content differs.
        """
        key = id(bars)
        with self._lock:
            fingerprint = self._fingerprint_by_id.get(key)
        if fingerprint is not None:
            return fingerprint
        fingerprint = _hash_bars(bars)
        with self._lock:
            self._fingerprint_by_id[key] = fingerprint
            self._fingerprinted_refs.append(bars)
        return fingerprint

    def _key(
        self,
        indicator_name: str,
        canonical_params: str,
        symbol: str,
        timeframe: str,
        data_fingerprint: str,
    ) -> str:
        """Compose the five key components into one SHA-256 digest.

        Mirrors ``BacktestCache._key``'s pattern: each component is UTF-8
        encoded and fed into a single running digest, separated by ``\\x00``
        bytes, then hex-digested once at the end.
        """
        digest = hashlib.sha256()
        digest.update(indicator_name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(canonical_params.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(symbol.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(timeframe.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data_fingerprint.encode("utf-8"))
        return digest.hexdigest()

    def get_or_compute(
        self,
        indicator_name: str,
        params: Mapping[str, Any],
        symbol: str,
        timeframe: str,
        bars: Sequence[OHLCVBar],
        *,
        compute: Callable[[], Any],
    ) -> Tuple[Any, bool]:
        """Return a cached indicator value for this key, computing it only on a miss.

        Unlike ``BacktestCache.get_or_run``'s ``runner`` (which defaults to
        ``run_strategy_code``), ``compute`` has no default: there is no single
        computation function common to every indicator kind, so the caller
        always supplies the specific computation to run on a miss.

        Preconditions:
          - ``indicator_name``, ``symbol``, and ``timeframe`` are non-empty
            strings.
          - ``bars`` is a non-empty sequence of ``OHLCVBar`` for ``symbol``.
          - ``compute`` is a zero-argument callable that deterministically
            returns the indicator value for ``(indicator_name, params, symbol,
            timeframe, bars)`` when invoked.
        Postconditions:
          - Returns ``(value, hit)``. On a hit, ``value`` is the object stored
            by the first call with the same key and ``compute`` was not
            invoked. On a miss, ``compute`` was invoked at least once (exactly
            once absent a concurrent race on the same key) and its result is
            both stored and returned.
          - ``hits``/``misses`` are incremented to reflect the outcome; a
            racer that loses a concurrent store still counts as a hit, since
            the value it observes came from the winning ``compute`` call.
        """
        assert isinstance(indicator_name, str) and indicator_name, (
            "indicator_name must be a non-empty string"
        )
        assert isinstance(symbol, str) and symbol, "symbol must be a non-empty string"
        assert isinstance(timeframe, str) and timeframe, "timeframe must be a non-empty string"
        assert bars, "bars must be non-empty"

        key = self._key(
            indicator_name,
            _canonical_params(params),
            symbol,
            timeframe,
            self._bars_fingerprint(bars),
        )

        with self._lock:
            if key in self._values:
                self.hits += 1
                return self._values[key], True

        # Invoked outside the lock so one slow computation never blocks
        # lookups/stores for unrelated keys from concurrent callers.
        value = compute()

        with self._lock:
            if key in self._values:
                # A concurrent caller already stored a value for this key
                # while we were computing ours; serve the winner's value so
                # every caller observes a single canonical result per key.
                self.hits += 1
                return self._values[key], True
            self._values[key] = value
            self.misses += 1
            return value, False
