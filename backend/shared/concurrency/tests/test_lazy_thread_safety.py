"""Concurrent-first-call proof for :class:`LazySingleton` and :class:`KeyedLazyRegistry`.

Both classes' own test modules (``test_lazy_singleton.py``,
``test_keyed_lazy_registry.py``) cover the single-threaded contract only and
explicitly defer the concurrency proof here. This module is that proof: it spins up
real threads racing a blocking factory on first access and asserts, deterministically
rather than by timing luck, that:

- the factory runs exactly once even under concurrent first calls, and every caller
  observes that same built value (both primitives);
- for :class:`KeyedLazyRegistry`, distinct keys build fully independently — a slow
  factory for one key never delays another key's first construction;
- a factory that always raises is never allowed to "win" a race by accident: every
  racing caller gets its own factory attempt and its own exception, nothing is
  cached, and the primitive is still buildable afterward (both primitives).

The builds-exactly-once and raising-factory tests below (four of the five) race
through :class:`~shared.concurrency.testing.ConcurrentFirstCallHarness`, the shared
hold-open/release/join-and-confirm race harness — the same idiom already proven
reliable in ``branding_team/tests/test_store_singleton.py``,
``branding_team/tests/test_conversation_phase_cache.py``, and
``branding_team/tests/test_brand_phase_cache.py``. Those three still hand-roll their
own copy of the idiom rather than using the harness; migrating them is a separate,
opportunistic follow-up out of scope for this module (see
``shared/concurrency/testing.py`` for why).

The fifth test,
``test_keyed_lazy_registry_distinct_keys_construct_concurrently_without_blocking_each_other``,
is also multi-threaded but does not use the harness: it needs two independent
started/release gates, one per key, so it can hold one key's build open while timing
the other's completion — the harness has only a single shared release, built for
racing many threads through one build rather than coordinating two independent ones.
"""

from __future__ import annotations

import threading

from shared.concurrency.keyed_lazy_registry import KeyedLazyRegistry
from shared.concurrency.lazy_singleton import LazySingleton
from shared.concurrency.testing import ConcurrentFirstCallHarness

_THREAD_COUNT = 8
_WAIT_TIMEOUT = 5
# Generous margin for the distinct-keys non-blocking proof below: an independent
# build is near-instant, while a wrongly serialized build would block until
# release_a is set — i.e. effectively forever within this bound — so any value
# much larger than the cost of one uncontended build works.
_NON_BLOCKING_BOUND = 2


def test_lazy_singleton_concurrent_first_call_builds_exactly_once() -> None:
    """Concurrent first calls must not race past the None-check and double-build."""
    harness: ConcurrentFirstCallHarness[object] = ConcurrentFirstCallHarness()
    singleton: LazySingleton[object] = LazySingleton()
    results: list[object] = []
    results_lock = threading.Lock()

    def _call() -> None:
        value = singleton.get_or_create(lambda: harness.hold_open(object))
        with results_lock:
            results.append(value)

    harness.run(_THREAD_COUNT, _call)

    assert len(results) == _THREAD_COUNT
    assert len({id(r) for r in results}) == 1
    assert harness.build_count == 1


def test_keyed_lazy_registry_concurrent_first_call_builds_exactly_once_per_key() -> None:
    """Concurrent first calls for the same new key must not double-build."""
    harness: ConcurrentFirstCallHarness[object] = ConcurrentFirstCallHarness()
    registry: KeyedLazyRegistry[str, object] = KeyedLazyRegistry()
    results: list[object] = []
    results_lock = threading.Lock()

    def _call() -> None:
        value = registry.get_or_create("shared-key", lambda: harness.hold_open(object))
        with results_lock:
            results.append(value)

    harness.run(_THREAD_COUNT, _call)

    assert len(results) == _THREAD_COUNT
    assert len({id(r) for r in results}) == 1
    assert harness.build_count == 1


def test_keyed_lazy_registry_distinct_keys_construct_concurrently_without_blocking_each_other() -> None:
    """A slow first build for one key must never delay another key's first build.

    Proven deterministically rather than by timing luck: key "b"'s build must
    complete within a short bound *while key "a"'s build is still deliberately held
    open*. If distinct keys were wrongly serialized through one shared lock, "b"
    would hang until "a" is released and this assertion would fail on the bound
    instead of racing against it.
    """
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()
    started_a = threading.Event()
    release_a = threading.Event()

    def factory_a() -> str:
        started_a.set()
        assert release_a.wait(timeout=_WAIT_TIMEOUT), "test setup deadlocked waiting for release_a"
        return "a-value"

    results: dict[str, str] = {}
    b_done = threading.Event()
    thread_errors: list[BaseException] = []
    thread_errors_lock = threading.Lock()

    def _build_a() -> None:
        try:
            results["a"] = registry.get_or_create("a", factory_a)
        except BaseException as exc:  # noqa: BLE001 - surfaced via thread_errors below
            with thread_errors_lock:
                thread_errors.append(exc)

    def _build_b() -> None:
        try:
            results["b"] = registry.get_or_create("b", lambda: "b-value")
        except BaseException as exc:  # noqa: BLE001 - surfaced via thread_errors below
            with thread_errors_lock:
                thread_errors.append(exc)
        finally:
            # Set even on failure so a crashed _build_b reports its real exception
            # via thread_errors below instead of a misattributed "blocked" message.
            b_done.set()

    thread_a = threading.Thread(target=_build_a, daemon=True)
    thread_a.start()
    try:
        assert started_a.wait(timeout=_WAIT_TIMEOUT), "key 'a' factory never started"

        thread_b = threading.Thread(target=_build_b, daemon=True)
        thread_b.start()
        try:
            assert b_done.wait(timeout=_NON_BLOCKING_BOUND), (
                "key 'b' construction blocked on key 'a's in-flight build"
            )
        finally:
            # Join, but don't assert here: in the exact failure this test targets
            # (keys wrongly serialized), thread_b is still blocked and this join
            # times out too — asserting inside this finally would let a generic
            # "did not finish" message override the more informative b_done
            # assertion above as the primary reported failure.
            thread_b.join(timeout=_WAIT_TIMEOUT)
        assert not thread_b.is_alive(), "thread_b did not finish"
    finally:
        # Always release key 'a's factory and join it, even if an assertion above
        # failed — otherwise thread_a stays blocked on release_a.wait() for up to
        # _WAIT_TIMEOUT seconds and raises its own confusing secondary
        # AssertionError on a background thread after the real failure is reported.
        release_a.set()
        thread_a.join(timeout=_WAIT_TIMEOUT)
    assert not thread_a.is_alive(), "thread_a did not finish"

    # Worker-thread exceptions never propagate on their own: assert on them here so
    # a genuine failure (e.g. factory_a's own guard assert firing) surfaces as
    # itself rather than as a KeyError or a misattributed "blocked" message below.
    assert not thread_errors, f"worker thread(s) failed: {thread_errors!r}"

    assert results["b"] == "b-value"
    assert results["a"] == "a-value"


def test_lazy_singleton_concurrent_raising_factory_leaves_instance_retryable() -> None:
    """A factory that always raises must never let a race cache a false success.

    Every racing caller gets its own factory attempt (serialized, since nothing is
    ever cached on failure) and its own exception — not just the first one to reach
    the lock — and the instance is still buildable afterward.
    """
    harness: ConcurrentFirstCallHarness[str] = ConcurrentFirstCallHarness()
    singleton: LazySingleton[str] = LazySingleton()
    errors: list[RuntimeError] = []
    errors_lock = threading.Lock()

    def _boom() -> str:
        raise RuntimeError("boom")

    def _call() -> None:
        try:
            singleton.get_or_create(lambda: harness.hold_open(_boom))
        except RuntimeError as exc:
            with errors_lock:
                errors.append(exc)

    harness.run(_THREAD_COUNT, _call)

    assert len(errors) == _THREAD_COUNT
    assert harness.build_count == _THREAD_COUNT

    # Nothing was cached across the whole race: the instance still builds normally.
    assert singleton.get_or_create(lambda: "recovered") == "recovered"


def test_keyed_lazy_registry_concurrent_raising_factory_leaves_key_retryable() -> None:
    """Same proof as the singleton case, for one key racing a failing factory."""
    harness: ConcurrentFirstCallHarness[str] = ConcurrentFirstCallHarness()
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()
    errors: list[RuntimeError] = []
    errors_lock = threading.Lock()

    def _boom() -> str:
        raise RuntimeError("boom")

    def _call() -> None:
        try:
            registry.get_or_create("shared-key", lambda: harness.hold_open(_boom))
        except RuntimeError as exc:
            with errors_lock:
                errors.append(exc)

    harness.run(_THREAD_COUNT, _call)

    assert len(errors) == _THREAD_COUNT
    assert harness.build_count == _THREAD_COUNT

    # Nothing was cached for this key across the whole race: it still builds normally.
    assert registry.get_or_create("shared-key", lambda: "recovered") == "recovered"
