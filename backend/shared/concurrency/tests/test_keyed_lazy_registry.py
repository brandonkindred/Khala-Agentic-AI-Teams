"""Single-threaded correctness tests for :class:`KeyedLazyRegistry`.

Concurrent-first-call proof is tracked separately (the dedicated thread-safety
test suite covering both ``LazySingleton`` and ``KeyedLazyRegistry``); this
module covers the single-threaded contract: build-once-per-key,
return-same-object, independence between keys, and raise-and-retry on a failing
factory.
"""

from __future__ import annotations

import pytest

from shared.concurrency.keyed_lazy_registry import KeyedLazyRegistry


def test_get_or_create_builds_once_per_key_and_returns_same_object() -> None:
    calls: list[str] = []

    def factory() -> object:
        calls.append("a")
        return object()

    registry: KeyedLazyRegistry[str, object] = KeyedLazyRegistry()
    first = registry.get_or_create("a", factory)
    second = registry.get_or_create("a", factory)

    assert first is second
    assert len(calls) == 1


def test_distinct_keys_build_distinct_values() -> None:
    calls: list[str] = []

    def factory_for(key: str):
        def build() -> object:
            calls.append(key)
            return object()

        return build

    registry: KeyedLazyRegistry[str, object] = KeyedLazyRegistry()
    a = registry.get_or_create("a", factory_for("a"))
    b = registry.get_or_create("b", factory_for("b"))

    assert a is not b
    assert calls == ["a", "b"]
    # Each key keeps returning its own value, not the most recently built one.
    assert registry.get_or_create("a", factory_for("a")) is a
    assert registry.get_or_create("b", factory_for("b")) is b
    assert calls == ["a", "b"]


def test_get_or_create_does_not_invoke_factory_of_a_later_call() -> None:
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()
    registry.get_or_create("k", lambda: "first")

    def should_not_run() -> str:
        raise AssertionError("factory of a later call must not run once the key is built")

    assert registry.get_or_create("k", should_not_run) == "first"


def test_raising_factory_propagates_and_leaves_that_key_retryable() -> None:
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()

    def failing() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        registry.get_or_create("k", failing)

    # The failed attempt must not have cached anything for that key — a
    # subsequent call retries by invoking its own factory again.
    assert registry.get_or_create("k", lambda: "recovered") == "recovered"


def test_failing_key_does_not_disturb_an_already_built_key() -> None:
    calls: list[str] = []

    def build_a() -> object:
        calls.append("a")
        return object()

    def failing() -> object:
        raise RuntimeError("boom")

    registry: KeyedLazyRegistry[str, object] = KeyedLazyRegistry()
    a = registry.get_or_create("a", build_a)

    with pytest.raises(RuntimeError, match="boom"):
        registry.get_or_create("b", failing)

    # "a" is untouched: same object, and its factory did not re-run.
    assert registry.get_or_create("a", build_a) is a
    assert calls == ["a"]


def test_none_returning_factory_raises_and_leaves_key_retryable() -> None:
    registry: KeyedLazyRegistry[str, object] = KeyedLazyRegistry()

    with pytest.raises(ValueError, match="factory for key 'k' returned None"):
        registry.get_or_create("k", lambda: None)

    # Nothing was cached — a subsequent call with a valid factory still builds.
    assert registry.get_or_create("k", lambda: "recovered") == "recovered"


def test_factory_reentering_its_own_key_raises_instead_of_deadlocking() -> None:
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()

    def reentrant() -> str:
        return registry.get_or_create("k", lambda: "inner")

    with pytest.raises(RuntimeError, match="reentrantly"):
        registry.get_or_create("k", reentrant)

    # The rejected attempt cached nothing, so the key is still buildable.
    assert registry.get_or_create("k", lambda: "recovered") == "recovered"


def test_supports_any_hashable_key_type() -> None:
    registry: KeyedLazyRegistry[object, str] = KeyedLazyRegistry()

    assert registry.get_or_create(7, lambda: "int") == "int"
    assert registry.get_or_create(("brand", 1), lambda: "tuple") == "tuple"
    # Distinct key types don't collide, and each still returns its own value.
    assert registry.get_or_create(7, lambda: "unused") == "int"
    assert registry.get_or_create(("brand", 1), lambda: "unused") == "tuple"
