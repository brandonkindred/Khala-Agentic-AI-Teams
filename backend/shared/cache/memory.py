"""In-process OrderedDict LRU + Future single-flight backend.

Preserves the semantics of the former process-local caches in the code review
agent and ``llm_service.compaction`` when Redis is not configured.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import Future
from typing import Callable, Dict, Optional, Tuple, Union


class _CacheClearedError(RuntimeError):
    """Raised into waiters when ``delete``/``clear`` drops an in-flight Future."""


class MemoryBackend:
    """Bounded in-process LRU with single-flight de-duplication.

    Invariants:
        - All mutations of the store and the in-flight registry happen under
          ``_lock``; hold times stay short (no compute under the lock).
        - Every in-flight Future is resolved before its slot is released: the
          leader sets a result/exception, or ``delete``/``clear`` resolves it
          with ``_CacheClearedError``. A mid-flight clear may resolve the
          Future before the leader finishes; the leader still returns its
          payload (and may store it) without re-resolving — intentional so an
          in-flight review is not discarded by a concurrent clear (see
          ``test_clear_mid_flight_does_not_prevent_leader_from_caching``).
    """

    def __init__(self) -> None:
        self._store: "OrderedDict[str, bytes]" = OrderedDict()
        self._inflight: "Dict[str, Future]" = {}
        self._lock = threading.Lock()

    @staticmethod
    def _ensure_non_negative_capacity(max_entries: int) -> None:
        if max_entries < 0:
            raise ValueError("max_entries must be >= 0")

    def get(self, key: str) -> Optional[bytes]:
        """Return the cached value for ``key``, marking it recently used.

        Postconditions:
            - Returns the exact bytes previously ``set``, or ``None`` on miss.
            - Does not participate in single-flight; use ``single_flight`` for
              exactly-once compute under contention.
        """
        with self._lock:
            hit = self._store.get(key)
            if hit is not None:
                self._store.move_to_end(key)
            return hit

    def set(self, key: str, value: bytes, *, max_entries: int) -> None:
        """Store ``value`` under ``key``, evicting oldest entries past capacity.

        Preconditions:
            - ``max_entries`` >= 0. ``0`` means do not store (no-op).
        Postconditions:
            - On success the next ``get(key)`` returns ``value`` until eviction
              or ``delete``/``clear``.
        """
        self._ensure_non_negative_capacity(max_entries)
        if max_entries == 0:
            return
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > max_entries:
                self._store.popitem(last=False)

    @staticmethod
    def _resolve_abandoned(fut: Future) -> None:
        if not fut.done():
            fut.set_exception(_CacheClearedError("shared.cache entry cleared while in flight"))

    def _finish_inflight(
        self,
        key: str,
        fut: Future,
        resolve_with: Optional[Union[bytes, BaseException]] = None,
    ) -> None:
        """Resolve ``fut`` (if still pending) and drop the in-flight slot.

        Preconditions:
            - ``fut`` is the Future originally registered for ``key``.
        Postconditions:
            - When ``resolve_with`` is bytes, sets the result if not done.
            - When ``resolve_with`` is a ``BaseException``, sets that exception
              if not done.
            - When ``resolve_with`` is ``None``, leaves an already-resolved
              Future alone (clear/delete may have resolved it).
            - The in-flight slot for ``key`` is removed iff it still points at
              ``fut``.
        """
        if resolve_with is not None and not fut.done():
            if isinstance(resolve_with, BaseException):
                fut.set_exception(resolve_with)
            else:
                fut.set_result(resolve_with)
        with self._lock:
            if self._inflight.get(key) is fut:
                del self._inflight[key]

    def delete(self, key: str) -> None:
        """Drop ``key`` and abandon any in-flight single-flight waiters for it.

        Postconditions:
            - Any value stored for ``key`` at the time of the call is removed.
            - Any waiter on an in-flight Future for ``key`` receives
              ``_CacheClearedError``.
            - A concurrent ``single_flight`` leader that has already started
              computing may still store its result (same intentional mid-flight
              behavior as ``clear``), so a later ``get(key)`` may hit with that
              freshly computed value.
        """
        with self._lock:
            self._store.pop(key, None)
            fut = self._inflight.pop(key, None)
        if fut is not None:
            self._resolve_abandoned(fut)

    def single_flight(
        self,
        key: str,
        compute: Callable[[], Tuple[bytes, bool]],
        *,
        max_entries: int,
    ) -> bytes:
        """Return a cached value or run ``compute`` at most once per key.

        Preconditions:
            - ``compute`` returns ``(payload, cacheable)``. When ``cacheable``
              is False the payload is handed to waiters but not stored.
            - ``max_entries`` >= 0. ``0`` means passthrough (no caching / no
              single-flight).
        Postconditions:
            - At most one leader runs ``compute`` for ``key`` at a time unless
              ``delete``/``clear`` invalidated the in-flight marker.
            - Waiters receive the leader's payload or the same exception the
              leader raised (including control-flow ``BaseException``s), unless
              ``delete``/``clear`` resolved the in-flight Future with
              ``_CacheClearedError`` first.
        """
        self._ensure_non_negative_capacity(max_entries)
        if max_entries == 0:
            payload, _cacheable = compute()
            return payload

        with self._lock:
            hit = self._store.get(key)
            if hit is not None:
                self._store.move_to_end(key)
            else:
                fut = self._inflight.get(key)
                is_leader = fut is None
                if is_leader:
                    fut = self._inflight[key] = Future()
        if hit is not None:
            return hit

        if not is_leader:
            return fut.result()

        try:
            payload, cacheable = compute()
        except BaseException as exc:
            # Propagate the same exception to waiters (including control-flow
            # BaseExceptions) so KeyboardInterrupt/SystemExit is not
            # misreported as a cache clear.
            self._finish_inflight(key, fut, resolve_with=exc)
            raise

        with self._lock:
            if cacheable:
                self._store[key] = payload
                self._store.move_to_end(key)
                while len(self._store) > max_entries:
                    self._store.popitem(last=False)
        # A mid-flight clear/delete may have already resolved this Future for
        # waiters; the leader still returns its payload (and may have stored it).
        self._finish_inflight(key, fut, resolve_with=payload)
        return payload

    def clear(self) -> int:
        """Drop every entry and abandon all in-flight Futures.

        Postconditions:
            - Returns the number of store entries removed at the moment the
              locked wipe ran (``0`` when empty). Emptiness is only guaranteed
              immediately after that wipe — concurrent writers may insert again
              once the lock is released.
            - Waiters receive ``_CacheClearedError``.
            - A concurrent ``single_flight`` leader that has already started
              computing may still store its result, so a later ``get(key)`` may
              hit with that freshly computed value.
        """
        with self._lock:
            removed = len(self._store)
            pending = list(self._inflight.values())
            self._store.clear()
            self._inflight.clear()
        for fut in pending:
            self._resolve_abandoned(fut)
        return removed
