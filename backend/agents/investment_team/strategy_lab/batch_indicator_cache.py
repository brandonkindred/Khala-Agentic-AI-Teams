"""A batch-scoped, cross-strategy indicator-value cache.

A Strategy Lab batch backtest evaluates many candidate strategies ("cycles")
against the same symbol/timeframe/date-range universe in one batch run, and
distinct candidates commonly compute structurally identical indicator series
(e.g. ``SMA(50)`` on ``AAPL``/``1d``). :class:`BatchIndicatorCache` memoizes
one indicator computation's result across every cycle sharing an instance, so
that repeated work within a batch is computed once.

This module is purely additive: it is not yet consulted by
``strategy_lab/indicators/streaming.py``'s ``IndicatorRegistry`` or
constructed anywhere in the batch Temporal workflow. Wiring it in is tracked
by sibling issues; this module only provides the primitive.

Key composition and invalidation follow
``system_design/adr/ADR-012-batch-indicator-cache-key-and-invalidation.md``:
a cache entry is keyed on ``(indicator_name, canonical_params, symbol,
timeframe, data_fingerprint)``, digested with SHA-256 in that fixed order —
mirroring :class:`..backtest_cache.BacktestCache`'s ``_key`` pattern of
digesting canonicalized components in a fixed order. ``data_fingerprint``
reuses :func:`..market_data_cache.store.compute_dataset_fingerprint`, scoped
to a single symbol's bar slice, per the ADR's guidance to reuse that public
function rather than a bespoke date-range key or the module-private
``_hash_bars`` helper. Because the key is fully content-addressed, the
invalidation boundary is the key itself: any difference in the five
components produces a different key, so a stale read is structurally
impossible as long as every input that affects the computed series is
represented in the key (see the ADR for the full invalidation contract,
including the custom-code-bypass and batch-only-scope rules that govern how
callers must use this cache — not enforced by this module itself).

Reused building blocks:
  * :func:`..market_data_cache.store.compute_dataset_fingerprint` — canonical
    SHA-256 over a symbol's OHLCV bars.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Callable, Dict, Mapping, Sequence, Tuple

from ..market_data_cache.store import compute_dataset_fingerprint

# Sentinel distinguishing "no entry for this key" from "entry present with
# value None" — some indicators (e.g. IndicatorRegistry's EMA/SMA methods)
# legitimately return None during warm-up (len(bars) < period), and that
# None must be cacheable like any other value rather than read back as a
# miss on every subsequent lookup.
_ABSENT = object()


class BatchIndicatorCache:
    """Memoize indicator computations across the cycles of one batch.

    Invariants:
      - The cache is batch-scoped: callers must construct a fresh instance
        per batch and discard it when the batch ends, never reusing one
        across batches (ADR-012's scope/lifetime rule) — not enforced by this
        class, which has no notion of "batch" beyond its own lifetime.
      - ``hits + misses`` equals the number of :meth:`get_or_compute` calls.
      - Concurrent :meth:`get_or_compute` calls never observe a torn/partial
        entry: a read sees either no entry or a fully-computed one. Two
        calls racing on the same key may both invoke ``compute`` (redundant
        work, not a correctness issue per the ADR's concurrency requirement).
    """

    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self.hits: int = 0
        self.misses: int = 0

    @staticmethod
    def _canonical_params(params: Mapping[str, Any]) -> str:
        """Stable JSON serialization of ``params``, independent of key order.

        Matches ``backtest_cache.py``'s ``_config_hash`` convention (sorted
        keys, no whitespace) so float params (e.g. ``num_std``,
        ``multiplier``) always serialize identically for equal values.
        """
        return json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _data_fingerprint(symbol: str, bars: Sequence[Any]) -> str:
        """Content fingerprint of one symbol's bar slice.

        Reuses :func:`compute_dataset_fingerprint`, scoped to a single-symbol
        dict, per ADR-012 — this is strictly more precise than a
        ``(start_date, end_date)`` pair since it also distinguishes a data
        restatement or a backfilled gap within the same nominal range.
        """
        return compute_dataset_fingerprint({symbol: bars})

    def _key(
        self,
        indicator_name: str,
        params: Mapping[str, Any],
        symbol: str,
        timeframe: str,
        bars: Sequence[Any],
    ) -> str:
        digest = hashlib.sha256()
        digest.update(indicator_name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self._canonical_params(params).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(symbol.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(timeframe.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self._data_fingerprint(symbol, bars).encode("utf-8"))
        return digest.hexdigest()

    def get_or_compute(
        self,
        indicator_name: str,
        params: Mapping[str, Any],
        symbol: str,
        timeframe: str,
        bars: Sequence[Any],
        compute: Callable[[], Any],
    ) -> Tuple[Any, bool]:
        """Return an indicator result for the given key, computing only on a miss.

        Preconditions:
          - ``indicator_name``, ``symbol``, and ``timeframe`` are non-empty
            strings.
          - ``params`` includes every value that steers ``compute``'s math —
            not just ``period``, but also e.g. ``source`` when the indicator
            is source-sensitive. The DSL's ``IndicatorRef`` (``spec_dsl.py``)
            carries ``source`` as a field separate from ``params``, so a
            caller forwarding ``IndicatorRef.params`` verbatim MUST fold
            ``source`` (and any other out-of-band steering value) into the
            mapping passed here — this cache has no dedicated ``source``
            argument and treats ``params`` as the complete parameter
            identity. Omitting a steering value lets two calls that compute
            different series (e.g. the same period over ``close`` vs.
            ``high``) collide on the same key.
          - ``bars`` is a non-empty sequence of the OHLCV bars ``compute``
            would compute the indicator over.
          - ``compute`` is a zero-arg callable that returns the indicator's
            result for ``(indicator_name, params, symbol, timeframe, bars)``;
            ``None`` is a valid result (e.g. an indicator during warm-up) and
            is cached like any other value.
        Postconditions:
          - Returns ``(value, hit)``. On a hit, ``value`` is the result
            stored by the first call with the same key and ``compute`` was
            not invoked. On a miss, ``compute`` was invoked exactly once (per
            this call — see the concurrency invariant for racing calls) and
            its result stored and returned.
          - ``hits``/``misses`` are incremented to reflect the outcome.
        """
        assert isinstance(indicator_name, str) and indicator_name, (
            "indicator_name must be non-empty"
        )
        assert isinstance(symbol, str) and symbol, "symbol must be non-empty"
        assert isinstance(timeframe, str) and timeframe, "timeframe must be non-empty"
        assert bars, "bars must be non-empty"

        key = self._key(indicator_name, params, symbol, timeframe, bars)

        with self._lock:
            cached = self._values.get(key, _ABSENT)
            if cached is not _ABSENT:
                self.hits += 1
                return cached, True

        # Compute outside the lock so concurrent callers on distinct keys
        # (or even the same key) are not serialized behind one computation;
        # a race on the same key costs redundant work, not correctness.
        value = compute()

        with self._lock:
            existing = self._values.get(key, _ABSENT)
            if existing is not _ABSENT:
                # Lost the race: another thread already stored a result.
                self.hits += 1
                return existing, True
            self._values[key] = value
            self.misses += 1
            return value, False
