"""Shared cache protocol: opaque string keys, opaque byte values.

Callers own key derivation and value serialization. This layer only stores and
retrieves bytes under a namespace prefix, with optional single-flight dedup.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, Tuple


class SharedCache(Protocol):
    """Process- or cluster-shared cache for opaque byte payloads.

    Invariants:
        - ``get`` / ``set`` / ``delete`` / ``single_flight`` / ``clear`` never raise for
          backend unavailability: Redis errors degrade to miss / no-op so a
          review or compaction never fails because the cache is down.
        - Keys are opaque caller-supplied strings (typically hex digests). The
          backend may prefix them with a namespace; callers must not depend on
          the stored key encoding.
    """

    def get(self, key: str) -> Optional[bytes]:
        """Return the cached payload for ``key``, or ``None`` on miss.

        Preconditions:
            - ``key`` is an opaque, non-empty string.
        Postconditions:
            - Returns the exact bytes previously ``set``, or ``None``.
            - Never raises for a backend outage (returns ``None`` instead).
        """
        ...

    def set(self, key: str, value: bytes, *, max_entries: int) -> None:
        """Store ``value`` under ``key``, evicting oldest entries past capacity.

        Preconditions:
            - ``max_entries`` >= 0. ``0`` means "do not store" (no-op).
        Postconditions:
            - On success the next ``get(key)`` returns ``value`` (until eviction
              or TTL). Backend failures are swallowed (fail-open).
        """
        ...

    def delete(self, key: str) -> None:
        """Drop a single key (and any associated single-flight markers).

        Preconditions:
            - ``key`` is an opaque, non-empty string.
        Postconditions:
            - Subsequent ``get(key)`` misses until a new ``set``. Backend
              failures are swallowed (fail-open). Used to evict corrupt entries.
        """
        ...

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
              is False the payload is handed to concurrent waiters but not
              stored for later hits (matching the chunk-outcome "degraded
              outcomes are never cached" rule).
            - ``max_entries`` >= 0. ``0`` means passthrough: call ``compute``
              and return its payload without caching or single-flight.
        Postconditions:
            - At most one leader runs ``compute`` for ``key`` at a time (best
              effort across processes on Redis; exact in-process on Memory
              unless ``delete`` / ``clear`` invalidates the in-flight marker,
              which resolves waiters with a clear error and allows a new
              leader).
            - Waiters receive the leader's payload, or recompute on Redis
              timeout / missing publish (fail-open, never hang forever).
            - Exceptions from ``compute`` propagate to the leader and to
              waiters (Memory: the same exception object, including
              control-flow ``BaseException``s such as ``KeyboardInterrupt``;
              Redis: reconstructed from a published JSON error marker for
              allow-listed ``Exception`` subclasses — control-flow
              ``BaseException``s are not published on Redis, so waiters fail
              open to recompute when the leader is interrupted).
        """
        ...

    def clear(self) -> Optional[int]:
        """Drop every entry in this namespace.

        Preconditions:
            - None.
        Postconditions:
            - Returns the number of entries removed on success, or ``None`` when
              a backend failure aborts the clear (Redis fail-open). Subsequent
              ``get`` calls miss until new ``set``s. Intended for tests;
              backend failures are swallowed.
        """
        ...
