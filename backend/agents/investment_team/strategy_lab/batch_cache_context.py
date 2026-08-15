"""Process-local resolution + context binding for the batch indicator cache.

A :class:`~.batch_indicator_cache.BatchIndicatorCache` holds a ``threading.Lock``
and a live dict, so it cannot be serialized across a Temporal activity /
child-workflow boundary. Instead the batch workflow
(``temporal/workflows.py``'s ``StrategyLabBatchWorkflow``) passes a deterministic
*string key* down to each cycle's ``run_design_attempt_activity``; the worker
resolves the one shared instance for that key here (:func:`get_or_create_batch_cache`)
and binds it on a ``ContextVar`` (:func:`use_batch_indicator_cache`) for the
duration of the attempt. The executor's registry-construction helper
(:func:`new_registry`) reads that binding, so every ``IndicatorRegistry`` built
inside one batch attempt shares the same cache instance. Mirrors
``agents/_llm_budget.py``'s ``use_budget``/``_active_budget`` pattern, which the
same activity already uses to thread the per-cycle LLM budget through.

Scope note — timeframe: the batch cache is *shared* here, but the per-strategy
``timeframe`` an ``IndicatorRegistry`` needs to actually *consult* the cache
(``resolve_indicator`` skips a registry whose ``_timeframe`` is empty) is a
``StrategySpec`` field chosen during design — not known at this batch/attempt
boundary, and not a batch-level constant. Supplying it (together with extending
``BatchIndicatorCache``'s bar fingerprint to the streaming engine's
``timestamp``-only bars, a gap ``resolve_indicator`` documents) is a separate
concern; here every shared registry is built with the default empty timeframe,
so the instance is shared but consultation stays inert — matching the feature's
flag-off-by-default, not-yet-engaged state.

Module invariants:
  - This module is imported only in-process (executor + the Temporal activity),
    never by the flat-sandbox subprocess (``trading_service/strategy/
    streaming_harness.py`` uses its own copied registry). The process-local
    ``_caches`` map is therefore scoped to one worker process.
  - It imports only stdlib plus :class:`BatchIndicatorCache` (whose module
    imports nothing from ``strategy_lab``), so it introduces no import cycle.
"""

from __future__ import annotations

import contextvars
import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Iterator, Optional

from .batch_indicator_cache import BatchIndicatorCache

# Bounded LRU of key -> BatchIndicatorCache. Batches run SEQUENTIALLY within a
# run, so at most one key per run is active at a time; the cap only has to
# exceed the number of runs whose current batch a single worker may be servicing
# concurrently off the shared "strategy-lab-queue". 64 is comfortably above any
# realistic concurrent-run count while bounding worst-case memory to 64 caches.
# LRU by access (get_or_create refreshes recency) means a batch's key stays at
# the MRU end for its whole active window — every cycle of that batch touches it
# repeatedly — so eviction only reclaims keys of batches that have gone idle
# (finished; sequential batches never reuse a key). Evicting a still-active
# batch's key would hand later cycles a fresh instance and break sharing, but
# that requires 64 *other* batches to be touched more recently, which the cap
# is sized to prevent. Deeper concurrency-correctness is a separate sub-issue.
_MAX_CACHES = 64

_caches: "OrderedDict[str, BatchIndicatorCache]" = OrderedDict()
_caches_lock = threading.Lock()


def get_or_create_batch_cache(key: str) -> BatchIndicatorCache:
    """Return the one :class:`BatchIndicatorCache` for ``key`` in this process.

    Preconditions:
        ``key`` is a non-empty string uniquely identifying one batch run
        (e.g. ``f"{run_id}-b{batch_idx}"``).
    Postconditions:
        Returns the same instance on every call with the same ``key`` (so every
        cycle/strategy of one batch on this worker shares it), and a distinct
        instance for a distinct ``key``. Thread-safe. The returned key is marked
        most-recently-used; keys beyond ``_MAX_CACHES`` are evicted
        least-recently-used first. Never raises.
    """
    assert isinstance(key, str) and key, "key must be a non-empty string"
    with _caches_lock:
        cache = _caches.get(key)
        if cache is None:
            cache = BatchIndicatorCache()
            _caches[key] = cache
        _caches.move_to_end(key)  # mark most-recently used
        while len(_caches) > _MAX_CACHES:
            _caches.popitem(last=False)  # evict least-recently used
        return cache


# The BatchIndicatorCache bound for the active batch attempt, or None outside
# one. Default None means "no batch cache" — the executor's new_registry() then
# builds a plain IndicatorRegistry(), preserving today's behavior for every
# caller until a batch attempt binds a cache.
_active_batch_cache: "contextvars.ContextVar[Optional[BatchIndicatorCache]]" = (
    contextvars.ContextVar("strategy_lab_active_batch_cache", default=None)
)


@contextmanager
def use_batch_indicator_cache(cache: BatchIndicatorCache) -> Iterator[None]:
    """Bind ``cache`` as the active batch indicator cache for the duration.

    Preconditions:
        Called once per design attempt around the whole attempt (the batch
        workflow's activity wraps ``_run_design_attempt`` with it), analogous to
        ``use_budget``.
    Postconditions:
        Within the ``with`` block :func:`active_batch_indicator_cache` (and thus
        :func:`new_registry`) sees ``cache``; the prior binding is restored on
        exit even if the block raises.
    """
    token = _active_batch_cache.set(cache)
    try:
        yield
    finally:
        _active_batch_cache.reset(token)


def active_batch_indicator_cache() -> Optional[BatchIndicatorCache]:
    """Return the cache bound by :func:`use_batch_indicator_cache`, or ``None``.

    Postconditions: pure read — never raises, never mutates.
    """
    return _active_batch_cache.get()


def new_registry():
    """Build an ``IndicatorRegistry`` wired to the active batch cache, if any.

    Postconditions:
        When no batch cache is bound (the default, and always when the
        ``STRATEGY_LAB_BATCH_INDICATOR_CACHE_ENABLED`` flag is off — the activity
        never binds then), returns a plain ``IndicatorRegistry()`` — byte-for-byte
        today's behavior. When one is bound, returns
        ``IndicatorRegistry(batch_cache=cache)`` so the whole attempt shares one
        cache instance; note the registry ctor itself additionally nulls
        ``batch_cache`` unless the flag is enabled, so a binding is inert with the
        flag off.
    """
    # Local import: avoids importing ``indicators.streaming`` (a heavier module,
    # copied into the sandbox subprocess) at this module's import time and keeps
    # the dependency edge one-directional.
    from .indicators.streaming import IndicatorRegistry

    cache = _active_batch_cache.get()
    if cache is None:
        return IndicatorRegistry()
    return IndicatorRegistry(batch_cache=cache)
