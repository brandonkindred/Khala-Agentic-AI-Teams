"""Test-only race harness for proving concurrent-first-call correctness.

Not part of ``shared.concurrency``'s production public API (see
``shared/concurrency/__init__.py``) — this module is imported only from test
files. Before it existed, the "hold the first caller open inside construction via a
started/release ``threading.Event`` handshake, let N threads queue up behind it,
then release and join" idiom was hand-rolled, byte-for-byte, once per test in
``shared/concurrency/tests/test_lazy_thread_safety.py`` and independently in three
``branding_team`` test modules (``tests/test_store_singleton.py``,
``tests/test_conversation_phase_cache.py``, ``tests/test_brand_phase_cache.py``).
This is the single shared copy, following the same pattern already established by
``shared/temporal/testing.py`` and ``shared/postgres/testing.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Generic, TypeVar

__all__ = ["ConcurrentFirstCallHarness"]

_T = TypeVar("_T")


class ConcurrentFirstCallHarness(Generic[_T]):
    """Holds a first "build" open across a started/release handshake, then races threads through it.

    Usage::

        harness: ConcurrentFirstCallHarness[object] = ConcurrentFirstCallHarness()
        singleton: LazySingleton[object] = LazySingleton()
        results: list[object] = []
        results_lock = threading.Lock()

        def _call() -> None:
            value = singleton.get_or_create(lambda: harness.hold_open(object))
            with results_lock:
                results.append(value)

        harness.run(8, _call)
        assert len(results) == 8
        assert len({id(r) for r in results}) == 1
        assert harness.build_count == 1

    Preconditions:
        - Exactly one thread passed to :meth:`run` invokes :meth:`hold_open` on this
          instance first, and does so before any thread returns — that first call is
          what the started/release handshake in :meth:`run` waits on and unblocks.
          A ``call`` where no thread ever reaches :meth:`hold_open` deadlocks
          :meth:`run` until its own wait times out and raises.
        - ``build`` passed to :meth:`hold_open` takes no arguments and either
          returns a ``_T`` or raises; it is safe to call from any thread.
        - This instance is used for one :meth:`run` call — construct a fresh
          instance per test/race rather than reusing one, since ``build_count`` and
          the internal events accumulate across calls.

    Postconditions:
        - :meth:`hold_open` increments :attr:`build_count` exactly once per
          invocation (whether ``build`` succeeds or raises), signals every thread
          waiting in :meth:`run` for the first call to arrive, then blocks the
          calling thread until :meth:`run` releases it.
        - :meth:`run` returns only once every thread it started has been joined and
          confirmed no longer alive; a thread still alive after its join timeout
          raises ``AssertionError`` naming it, rather than returning silently and
          leaving a hung thread as an unexplained downstream count mismatch.

    Invariants:
        - :attr:`build_count` is only ever incremented, under
          :attr:`_build_count_lock`, and never reset by this instance.
    """

    def __init__(self, *, wait_timeout: float = 5.0, settle: float = 0.1) -> None:
        self.build_count = 0
        self._build_count_lock = threading.Lock()
        self._started = threading.Event()
        self._release = threading.Event()
        self._wait_timeout = wait_timeout
        self._settle = settle

    def hold_open(self, build: Callable[[], _T]) -> _T:
        """Count this call, signal :meth:`run`'s waiter, block for release, then run ``build``.

        Preconditions:
            ``build`` takes no arguments and either returns a ``_T`` or raises.

        Postconditions:
            :attr:`build_count` has been incremented by exactly one, the internal
            "started" signal has been set, and this call has blocked until the
            internal "release" signal was set (raising ``AssertionError`` instead of
            hanging forever if that wait times out) before ``build`` ran. ``build``'s
            return value is returned, or its exception propagates unchanged.
        """
        with self._build_count_lock:
            self.build_count += 1
        self._started.set()
        assert self._release.wait(timeout=self._wait_timeout), "test setup deadlocked waiting for release"
        return build()

    def run(self, thread_count: int, call: Callable[[], None]) -> None:
        """Race ``thread_count`` threads through ``call``, releasing once the first reaches :meth:`hold_open`.

        Preconditions:
            ``thread_count >= 1``; see the class Preconditions for what ``call``
            must do (invoke :meth:`hold_open` on this instance, from whichever
            thread gets there first).

        Postconditions:
            See the class Postconditions: every started thread is joined and
            confirmed finished before this call returns, or ``AssertionError`` names
            the ones still alive.
        """
        assert thread_count >= 1, f"thread_count must be >= 1, got {thread_count}"
        # Daemon so a thread that's genuinely stuck (the deadlock this harness exists
        # to catch) can never block interpreter exit after the assertion below has
        # already reported the failure — the join-and-confirm loop is what actually
        # detects the hang; daemon status only keeps a detected hang from also
        # wedging the process.
        threads = [threading.Thread(target=call, daemon=True) for _ in range(thread_count)]
        threads[0].start()
        assert self._started.wait(timeout=self._wait_timeout), "first thread never entered hold_open"
        for t in threads[1:]:
            t.start()
        # Give the other threads a chance to reach (and queue up behind) the first
        # call's in-progress build before releasing it to finish.
        time.sleep(self._settle)
        self._release.set()
        for t in threads:
            t.join(timeout=self._wait_timeout)
        still_alive: list[str] = [t.name for t in threads if t.is_alive()]
        assert not still_alive, f"threads still alive after join (possible deadlock): {still_alive}"
