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

The N-thread tests below race through :class:`~shared.concurrency.testing.ConcurrentFirstCallHarness`,
the shared hold-open/release/join-and-confirm race harness — the same idiom already
proven reliable in ``branding_team/tests/test_store_singleton.py``,
``branding_team/tests/test_conversation_phase_cache.py``, and
``branding_team/tests/test_brand_phase_cache.py``, each of which still hand-rolls its
own copy since it holds open a monkeypatched constructor rather than an arbitrary
factory; migrating those three to the shared harness is a separate, opportunistic
follow-up. Unlike those call sites, ``LazySingleton``/``KeyedLazyRegistry`` take an
arbitrary ``factory`` callable directly, so the harness's :meth:`~shared.concurrency.testing.ConcurrentFirstCallHarness.hold_open`
blocks inside the factory itself rather than needing to monkeypatch a constructor.
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

    def _build_a() -> None:
        results["a"] = registry.get_or_create("a", factory_a)

    def _build_b() -> None:
        results["b"] = registry.get_or_create("b", lambda: "b-value")
        b_done.set()

    thread_a = threading.Thread(target=_build_a)
    thread_a.start()
    assert started_a.wait(timeout=_WAIT_TIMEOUT), "key 'a' factory never started"

    thread_b = threading.Thread(target=_build_b)
    thread_b.start()
    assert b_done.wait(timeout=_NON_BLOCKING_BOUND), "key 'b' construction blocked on key 'a's in-flight build"
    thread_b.join(timeout=_WAIT_TIMEOUT)

    release_a.set()
    thread_a.join(timeout=_WAIT_TIMEOUT)

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
    errors: list[BaseException] = []
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
    errors: list[BaseException] = []
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
