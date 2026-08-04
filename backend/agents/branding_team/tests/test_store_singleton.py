"""Tests for the ``BrandingStore`` process-wide singleton — no Postgres required.

``BrandingStore`` is stateless (the constructor never opens a connection — see
its docstring), so ``get_default_store()``'s caching/locking behavior can be
exercised without live Postgres. Mirrors the lazy/locked singleton test idiom
in ``backend/shared/neo4j/tests/test_shared_neo4j.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

from branding_team import store as store_mod
from branding_team.store import BrandingStore, get_default_store


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Give each test a clean slate, then restore whatever was cached before it.

    Other modules (notably ``branding_team.api.main``) call
    ``get_default_store()`` once at import time and hold onto that instance
    (e.g. tests elsewhere patch methods on ``main_mod.branding_store``). If
    this fixture left the module global cleared after the test, any later
    test in the same process that expects ``get_default_store()`` to keep
    returning that same cached instance would silently get a fresh,
    unpatched one instead. Restoring the pre-test value (rather than always
    resetting to ``None``) keeps this file's tests isolated without leaking
    that state into the rest of the session.
    """
    original = store_mod._default_store
    store_mod._default_store = None
    yield
    store_mod._default_store = original


def test_get_default_store_returns_cached_singleton() -> None:
    first = get_default_store()
    second = get_default_store()
    assert first is second
    assert isinstance(first, BrandingStore)


def test_get_default_store_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent first calls must not race past the None-check and double-construct.

    The first thread's ``__init__`` is held open (simulating slow construction)
    while several other threads call ``get_default_store()`` concurrently. With
    correct double-checked locking those threads block on ``_store_lock`` until
    the first construction finishes, so exactly one ``BrandingStore`` is ever
    built. An unsynchronized ``if _default_store is None`` check would let them
    all pass while the first thread is still constructing, building more than
    one instance — this test fails in that case.
    """
    build_count = 0
    build_count_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()
    real_init = BrandingStore.__init__

    def _slow_init(self) -> None:
        nonlocal build_count
        with build_count_lock:
            build_count += 1
        started.set()
        assert release.wait(timeout=5), "test setup deadlocked waiting for release"
        real_init(self)

    monkeypatch.setattr(BrandingStore, "__init__", _slow_init)

    results: list[BrandingStore] = []
    results_lock = threading.Lock()

    def _call() -> None:
        store = get_default_store()
        with results_lock:
            results.append(store)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    threads[0].start()
    assert started.wait(timeout=5), "first thread never entered __init__"
    for t in threads[1:]:
        t.start()
    # Give the other threads a chance to reach (and block on) _store_lock
    # before releasing the first thread to finish construction.
    time.sleep(0.1)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    assert len({id(s) for s in results}) == 1
    assert build_count == 1
