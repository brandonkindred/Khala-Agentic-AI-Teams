"""Single-threaded correctness tests for :class:`KeyedLazyRegistry`.

This module covers the single-threaded contract: build-once-per-key,
return-same-object, independence between keys, raise-and-retry on a failing
factory, and the cross-key nesting rules the class documents as preconditions.

The concurrent-first-call proof — that a key's value is built exactly once under
genuinely concurrent first access, the postcondition the class leads with — is
**not yet written**, here or anywhere else in this repository. It is planned as
one dedicated thread-safety suite covering this class and ``LazySingleton``
together, so the property is proven once rather than twice. Until that lands,
treat the guarantee as reasoned-but-unproven: no test in this tree exercises it.
"""

from __future__ import annotations

from typing import Callable

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

    def factory_for(key: str) -> Callable[[], object]:
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


def test_factory_may_build_a_key_this_registry_has_not_seen() -> None:
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()

    def build_a_then_b() -> str:
        registry.get_or_create("b", lambda: "b-value")
        return "a-value"

    # "a" is resolved first and so holds the lower global order; "b" is brand new
    # and is assigned a higher one, which is exactly what the underlying manager's
    # ordering rule permits.
    assert registry.get_or_create("a", build_a_then_b) == "a-value"
    assert registry.get_or_create("b", lambda: "unused") == "b-value"


def test_factory_may_read_an_already_built_key_whatever_its_order() -> None:
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()
    registry.get_or_create("b", lambda: "b-value")

    def build_a_reading_b() -> str:
        # "b" holds a *lower* order than "a" here, so this would be refused if it
        # reached the lock at all. It does not: a built key returns on the
        # unlocked fast path in get_or_create and never acquires its lock, so the
        # ordering rule is never consulted. This is why the class's precondition
        # is narrower than "never nest into an earlier-seen key".
        assert registry.get_or_create("b", lambda: "unused") == "b-value"
        return "a-value"

    assert registry.get_or_create("a", build_a_reading_b) == "a-value"


def test_factory_nesting_into_a_seen_but_unbuilt_key_raises_instead_of_deadlocking() -> None:
    registry: KeyedLazyRegistry[str, str] = KeyedLazyRegistry()

    def boom() -> str:
        raise RuntimeError("boom")

    # A *failed* build is what leaves "b" seen-but-unbuilt: it holds the lower
    # global order yet has no value, so a later nested call for it really does
    # reach the lock and is refused. This is the only shape that exercises the
    # guard — nesting into a successfully built key takes the fast path instead
    # (see the test above), so a test written that way would pass without ever
    # reaching the ordering check.
    with pytest.raises(RuntimeError, match="boom"):
        registry.get_or_create("b", boom)

    def build_a_then_b() -> str:
        registry.get_or_create("b", lambda: "b-value")
        return "a-value"

    with pytest.raises(RuntimeError, match="lower-order key nested under"):
        registry.get_or_create("a", build_a_then_b)


def test_supports_any_hashable_key_type() -> None:
    registry: KeyedLazyRegistry[object, str] = KeyedLazyRegistry()

    assert registry.get_or_create(7, lambda: "int") == "int"
    assert registry.get_or_create(("brand", 1), lambda: "tuple") == "tuple"
    # Distinct key types don't collide, and each still returns its own value.
    assert registry.get_or_create(7, lambda: "unused") == "int"
    assert registry.get_or_create(("brand", 1), lambda: "unused") == "tuple"
