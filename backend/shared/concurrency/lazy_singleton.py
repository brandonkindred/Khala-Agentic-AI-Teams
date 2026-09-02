"""Single-slot lazy-initialization primitive with double-checked locking.

:class:`LazySingleton` consolidates the hand-rolled "if value is None: with
lock: if value is None: value = build()" idiom duplicated across
``branding_team/store.py::get_default_store``,
``branding_team/api/main.py::_get_assistant_agent``, and
``shared/coro_runner.py``'s worker-pool singleton (see
``shared/concurrency/README.md`` for the full list of call sites this is
intended to replace). It is stdlib-only and lives in ``shared.concurrency``
alongside :class:`~shared.concurrency.keyed_lock_manager.KeyedLockManager`.
"""

from __future__ import annotations

import threading
from typing import Callable, Generic, Optional, TypeVar

__all__ = ["LazySingleton"]

_T = TypeVar("_T")


class LazySingleton(Generic[_T]):
    """A single value, lazily built at most once even under concurrent first access.

    Intended lifetime: construct one instance at module scope (or as an
    instance attribute) per value that should be built exactly once, and call
    :meth:`get_or_create` from every access point instead of hand-rolling the
    double-checked-locking check.

    Usage::

        _store: LazySingleton[BrandingStore] = LazySingleton()

        def get_default_store() -> BrandingStore:
            return _store.get_or_create(BrandingStore)

        # A factory that also performs a one-time side effect (e.g. atexit
        # registration) is just a closure — the primitive doesn't care:
        _pool: LazySingleton[ProcessPoolExecutor] = LazySingleton()

        def _get_pool() -> ProcessPoolExecutor:
            def _build() -> ProcessPoolExecutor:
                pool = ProcessPoolExecutor()
                atexit.register(pool.shutdown)
                return pool

            return _pool.get_or_create(_build)

    Preconditions:
        - Every ``factory`` passed to :meth:`get_or_create` takes no
          arguments and either returns a ``_T`` or raises.
        - ``factory`` never returns ``None`` — ``None`` is this class's
          internal sentinel for "not yet constructed", exactly as the
          hand-rolled call sites it replaces already relied on (e.g.
          ``assistant_agent is None``, ``_default_store is None``).
        - A caller does not assume a *specific* call's ``factory`` ran just
          because that call returned — once some call's ``factory`` has
          already constructed the value, later calls return that value
          without invoking their own ``factory`` at all.

    Postconditions:
        - The first ``factory`` invocation to complete without raising
          constructs the value exactly once; every call to
          :meth:`get_or_create` on this instance — whether it arrives
          before, during, or after that construction — returns that exact
          same object, never a second instance, even under concurrent first
          calls.
        - If ``factory`` raises, the exception propagates to that caller
          unchanged and the instance remains unconstructed: a later call
          retries by invoking its own ``factory`` again, rather than caching
          the failure — matching the ``HTTPException``-on-failure-then-retry
          contract ``branding_team/api/main.py::_get_assistant_agent``
          already had.

    Invariants:
        - At most one thread runs a ``factory`` call for this instance at a
          time (serialized by the internal lock) — two ``factory`` calls for
          the same instance never execute concurrently, even though only one
          of them can ultimately populate the slot.
    """

    def __init__(self) -> None:
        # Guards the construct-on-first-successful-call race; held only long
        # enough to re-check the slot and, if still empty, run factory() —
        # never across any caller-side work beyond that.
        self._lock = threading.Lock()
        self._value: Optional[_T] = None

    def get_or_create(self, factory: Callable[[], _T]) -> _T:
        """Return the constructed value, building it via ``factory`` on first success.

        Preconditions:
            ``factory`` takes no arguments and returns a non-``None`` ``_T``,
            or raises.

        Postconditions:
            See the class Postconditions: exactly-once construction across
            concurrent first calls, and raise-and-retry semantics when
            ``factory`` fails.
        """
        value = self._value
        if value is None:
            with self._lock:
                value = self._value
                if value is None:
                    value = factory()
                    self._value = value
        return value
