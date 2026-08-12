"""Shared per-key mutual-exclusion primitive for concurrent writers.

:class:`KeyedLockManager` consolidates the "serialize concurrent writers that
touch the same key, but let writers touching disjoint keys proceed fully
concurrently" pattern. It is stdlib-only and lives in ``shared.concurrency``
alongside :func:`~shared.concurrency.parallel_map.parallel_map` and
:class:`~shared.concurrency.latest_value_flusher.LatestValueFlusher`. See
``shared/concurrency/README.md`` for the motivating use case (protecting the
SE code-v2 gated execution loop's shared ``all_files`` accumulator and git
worktree once cross-microtask concurrency is wired in) and usage examples.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Dict, Generic, Hashable, Iterable, Iterator, List, TypeVar

__all__ = ["KeyedLockManager"]

K = TypeVar("K", bound=Hashable)


class KeyedLockManager(Generic[K]):
    """A registry of per-key locks: same key serializes, disjoint keys don't.

    Intended lifetime: construct one instance per logical run (e.g. one per
    task execution) and reuse it for every :meth:`lock` call across that run —
    it has no start/stop lifecycle, unlike :class:`~shared.concurrency.heartbeat.BackgroundHeartbeat`
    or :class:`~shared.concurrency.latest_value_flusher.LatestValueFlusher`; it
    is simply constructed and used.

    Usage::

        locks: KeyedLockManager[str] = KeyedLockManager()

        with locks.lock(["a.py", "b.py"]):
            ...write a.py and b.py, update shared state for both...

    A concurrent caller locking ``["b.py", "c.py"]`` blocks only on the
    overlapping key (``b.py``) until the first caller's ``with`` block exits;
    a caller locking ``["d.py"]`` (disjoint from both) proceeds immediately.

    Preconditions:
        - Every key passed to :meth:`lock` is hashable.
        - A thread does not call :meth:`lock` for a key it already holds from
          an outer, not-yet-exited :meth:`lock` call on that same thread (not
          reentrant — see :meth:`lock`'s own Preconditions for why, and what
          happens instead of a silent deadlock).

    Postconditions:
        - Two :meth:`lock` calls whose key sets are disjoint never block each
          other, regardless of timing.
        - Two :meth:`lock` calls that share at least one key are serialized
          with respect to each other for as long as either holds an
          overlapping key: the second caller to actually acquire it only
          proceeds after the first has released it (its ``with`` block has
          exited), so whichever caller acquires second observes every
          side effect the first caller made under the lock.

    Invariants:
        - A key is assigned exactly one internal ``Lock`` for the lifetime of
          this manager; repeated :meth:`lock` calls for the same key reuse it
          rather than creating a new one (which would defeat mutual
          exclusion). Locks are never removed, so this manager's memory usage
          is bounded by the number of distinct keys ever passed to it — the
          intended per-run lifetime keeps that bounded in practice.
        - Every two callers acquire any two given keys in the same relative
          order, fixed the first time either key is seen by this manager
          (see :meth:`lock`) — this is what makes multi-key batch acquisition
          deadlock-free without requiring ``K`` to support ordering (``<``).
    """

    def __init__(self) -> None:
        # Guards creation of a new key's Lock/order-index pair and lookup of an
        # existing one — held only for that brief registration/lookup, never
        # across an actual per-key lock acquisition (which would serialize
        # every key's acquisition through this one registry lock, defeating
        # the "disjoint keys proceed concurrently" contract).
        self._registry_lock = threading.Lock()
        self._locks: Dict[K, threading.Lock] = {}
        # Assigned once per key, at first sight, in a single monotonically
        # increasing sequence — this is the total order every lock(...) call
        # sorts a batch's keys by before acquiring, which is what prevents an
        # AB/BA-style deadlock between two callers that pass overlapping keys
        # in different orders (see the class Invariants).
        self._order: Dict[K, int] = {}
        self._next_order = 0
        # The thread currently holding each key's lock, if any — written only
        # by the thread that acquired it, read by a same-thread re-lock() to
        # raise instead of deadlocking. Not a general cross-key deadlock
        # detector (none is needed: acquisition order is already globally
        # consistent); this only catches direct reentrancy on one key.
        self._owners: Dict[K, threading.Thread] = {}

    def _resolve(self, key: K) -> threading.Lock:
        """Return ``key``'s Lock, creating and order-assigning it on first sight.

        Preconditions:
            ``key`` is hashable.
        Postconditions:
            The same ``Lock`` instance is returned for every call with an
            equal ``key`` for the lifetime of this manager. ``key`` is
            assigned an order index the first time it is seen (across all
            threads — resolved atomically under ``_registry_lock``, so two
            threads racing to resolve the same brand-new key never create two
            different Lock objects for it).
        """
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
                self._order[key] = self._next_order
                self._next_order += 1
            return lock

    @contextmanager
    def lock(self, keys: Iterable[K]) -> Iterator[None]:
        """Acquire every key in ``keys`` for the duration of the ``with`` block.

        Preconditions:
            - Every element of ``keys`` is hashable.
            - The current thread does not already hold, from an outer,
              not-yet-exited :meth:`lock` call, any key also present in this
              call's ``keys`` — plain ``threading.Lock`` is not reentrant, so
              this would otherwise deadlock the thread against itself
              forever; detected and raised as ``RuntimeError`` instead
              (best-effort: this checks each key about to be acquired against
              its current owner, not a full cross-key cycle detector, which
              is unnecessary here since acquisition order is already globally
              consistent — see the class Invariants).

        Postconditions:
            - ``keys`` is deduplicated before acquisition, so a batch
              containing a repeated key acquires that key's ``Lock`` exactly
              once (acquiring the same non-reentrant ``Lock`` twice on one
              thread would self-deadlock).
            - The deduplicated keys are acquired in this manager's
              globally-consistent order (see the class Invariants), not the
              order they appear in ``keys`` — this is what makes two callers
              locking overlapping key sets in different orders deadlock-free.
            - An empty ``keys`` is a no-op: no lock is acquired, and the
              ``with`` block's body still runs.
            - Every lock acquired by this call is released — in reverse
              acquisition order — before this call returns control to the
              caller, including when the ``with`` block's body raises: the
              release happens in a ``finally``, so a failed critical section
              never permanently withholds a key from later callers.
        """
        unique_keys: List[K] = list(dict.fromkeys(keys))
        current_thread = threading.current_thread()
        for key in unique_keys:
            if self._owners.get(key) is current_thread:
                raise RuntimeError(
                    f"KeyedLockManager.lock() called reentrantly for key {key!r} — "
                    "this thread already holds it from an outer, not-yet-exited "
                    "lock() call, which would deadlock against itself"
                )
        for key in unique_keys:
            self._resolve(key)  # ensure every key has an assigned order before sorting
        ordered_keys = sorted(unique_keys, key=lambda k: self._order[k])
        acquired: List[K] = []
        try:
            for key in ordered_keys:
                self._locks[key].acquire()
                self._owners[key] = current_thread
                acquired.append(key)
            yield
        finally:
            for key in reversed(acquired):
                del self._owners[key]
                self._locks[key].release()
