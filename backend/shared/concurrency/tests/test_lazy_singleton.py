"""Single-threaded correctness tests for :class:`LazySingleton`.

Concurrent-first-call proof is tracked separately (the dedicated
thread-safety test suite covering both ``LazySingleton`` and
``KeyedLazyRegistry``); this module covers the single-threaded contract:
build-once, return-same-object, and raise-and-retry on a failing factory.
"""

from __future__ import annotations

import pytest

from shared.concurrency.lazy_singleton import LazySingleton


def test_get_or_create_builds_once_and_returns_same_object() -> None:
    calls: list[int] = []

    def factory() -> object:
        calls.append(1)
        return object()

    singleton: LazySingleton[object] = LazySingleton()
    first = singleton.get_or_create(factory)
    second = singleton.get_or_create(factory)

    assert first is second
    assert len(calls) == 1


def test_get_or_create_does_not_invoke_factory_of_a_later_call() -> None:
    singleton: LazySingleton[str] = LazySingleton()
    singleton.get_or_create(lambda: "first")

    def should_not_run() -> str:
        raise AssertionError("factory of a later call must not run once constructed")

    assert singleton.get_or_create(should_not_run) == "first"


def test_raising_factory_propagates_and_leaves_instance_retryable() -> None:
    singleton: LazySingleton[str] = LazySingleton()

    def failing() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        singleton.get_or_create(failing)

    # The failed attempt must not have cached anything — a subsequent call
    # retries by invoking its own factory again.
    assert singleton.get_or_create(lambda: "recovered") == "recovered"


def test_none_returning_factory_raises_and_leaves_instance_retryable() -> None:
    singleton: LazySingleton[object] = LazySingleton()

    with pytest.raises(ValueError, match="factory returned None"):
        singleton.get_or_create(lambda: None)

    # Nothing was cached — a subsequent call with a valid factory still builds.
    assert singleton.get_or_create(lambda: "recovered") == "recovered"
