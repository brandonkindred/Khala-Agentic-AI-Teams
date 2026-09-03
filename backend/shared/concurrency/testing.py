"""Test-only race harness for proving concurrent-first-call correctness.

Not part of ``shared.concurrency``'s production public API (see
``shared/concurrency/__init__.py``) — this module is imported only from test
files. Before it existed, the "hold the first caller open inside construction via a
started/release ``threading.Event`` handshake, let N threads queue up behind it,
then release and join" idiom was hand-rolled, byte-for-byte, once per test in three
``branding_team`` test modules (``tests/test_store_singleton.py``,
``tests/test_conversation_phase_cache.py``, ``tests/test_brand_phase_cache.py``).
This is the single shared copy, following the same pattern already established by
``shared/temporal/testing.py`` and ``shared/postgres/testing.py``;
``shared/concurrency/tests/test_lazy_thread_safety.py`` is its first consumer.

Migrating those three ``branding_team`` copies to this harness is a mechanical,
drop-in change, not a technical blocker — :meth:`ConcurrentFirstCallHarness.hold_open`
accepts any zero-arg callable, including one that wraps a monkeypatched constructor.
It is left undone here because it is out of scope for the issue this module was
written for (``shared/concurrency`` only, no call-site or existing-test migration);
tracked as a separate, opportunistic follow-up.
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
        """Configure this harness's timeouts.

        Preconditions:
            ``wait_timeout`` and ``settle`` are non-negative seconds.

        Postconditions:
            ``wait_timeout`` bounds every internal wait this instance performs —
            the release handshake in :meth:`hold_open`, the started handshake and
            every per-thread join in :meth:`run` — each raising ``AssertionError``
            rather than hanging forever if it elapses; raise it for a slow CI
            runner. ``settle`` is the grace period :meth:`run` sleeps after
            starting the secondary threads and before releasing the first build,
            giving them a chance to reach and queue behind it; it is a best-effort
            window, not a guarantee, so setting it too low weakens (without
            invalidating) the race this harness exists to exercise.
        """
        assert wait_timeout >= 0, f"wait_timeout must be non-negative, got {wait_timeout}"
        assert settle >= 0, f"settle must be non-negative, got {settle}"
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
        assert self._release.wait(timeout=self._wait_timeout), (
            "release was never signaled — run() failed or timed out before self._release.set()"
        )
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
            the ones still alive. If any thread's ``call`` raised an exception that
            escaped it, the first such exception (in thread-start order) is
            re-raised on the calling thread after the join/still-alive checks, with
            its original type and traceback — a crashed worker surfaces as itself
            rather than as a downstream assertion elsewhere in the caller.
        """
        assert thread_count >= 1, f"thread_count must be >= 1, got {thread_count}"
        exceptions: list[BaseException] = []
        exceptions_lock = threading.Lock()

        def _target() -> None:
            try:
                call()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller below
                with exceptions_lock:
                    exceptions.append(exc)

        # Daemon so a thread that's genuinely stuck (the deadlock this harness exists
        # to catch) can never block interpreter exit after the assertion below has
        # already reported the failure — the join-and-confirm loop is what actually
        # detects the hang; daemon status only keeps a detected hang from also
        # wedging the process. Named so the still-alive assertion below identifies
        # which logical worker hung, instead of an unhelpful default "Thread-17".
        threads = [
            threading.Thread(target=_target, name=f"concurrent-first-call-{i}", daemon=True)
            for i in range(thread_count)
        ]
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
        if exceptions:
            raise exceptions[0]
