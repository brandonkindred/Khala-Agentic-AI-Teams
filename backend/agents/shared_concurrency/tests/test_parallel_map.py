"""Unit tests for the shared :func:`parallel_map` driver.

Covers every variation point the migrated callers rely on: empty-input
short-circuit, worker bounding, order preservation (and completion order),
``skip_none`` filtering, contextvar propagation (and its opt-out), and the
fast-fail error policy with the ``on_first_exception`` hook.
"""

from __future__ import annotations

import contextvars
import threading
import time

import pytest

from shared_concurrency.parallel_map import parallel_map


def test_empty_input_returns_empty_without_executor() -> None:
    """Empty *items* short-circuits to [] and never calls fn."""
    calls = {"n": 0}

    def fn(_x: int) -> int:
        calls["n"] += 1
        return _x

    assert parallel_map([], fn, max_workers=4) == []
    assert calls["n"] == 0


def test_preserves_input_order_under_jittered_completion() -> None:
    """Results come back aligned to input order even when later items finish
    first (earlier items sleep longer)."""

    def fn(x: int) -> int:
        # Earlier items sleep longer so completion order != submission order.
        time.sleep((5 - x) * 0.01)
        return x * 10

    assert parallel_map([0, 1, 2, 3, 4], fn, max_workers=5) == [0, 10, 20, 30, 40]


def test_completion_order_when_not_preserving() -> None:
    """preserve_order=False returns results in genuine completion order.

    Gates force item 4 to finish first, then 3, 2, 1, 0 — the reverse of input
    order — so the result is deterministic, not timing-dependent.
    """
    gates = {i: threading.Event() for i in range(5)}

    def fn(x: int) -> int:
        gates[x].wait(timeout=5)
        # Open the next item's gate so completion is strictly 4 -> 3 -> ... -> 0.
        if x > 0:
            gates[x - 1].set()
        return x

    gates[4].set()  # release the chain head
    out = parallel_map([0, 1, 2, 3, 4], fn, max_workers=5, preserve_order=False)
    assert out == [4, 3, 2, 1, 0]


def test_worker_bound_is_min_of_max_workers_and_len() -> None:
    """At most min(max_workers, len(items)) workers run concurrently."""
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    barrier_release = threading.Event()

    def fn(_x: int) -> int:
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        barrier_release.wait(timeout=2)
        with lock:
            live["now"] -= 1
        return _x

    # 6 items but max_workers=3 → never more than 3 concurrent.
    t = threading.Thread(target=lambda: parallel_map(list(range(6)), fn, max_workers=3))
    t.start()
    time.sleep(0.2)
    barrier_release.set()
    t.join(timeout=5)
    assert live["peak"] == 3


def test_skip_none_drops_none_results() -> None:
    """skip_none=True (default) filters None out, preserving order of the rest."""

    def fn(x: int):
        return x if x % 2 == 0 else None

    assert parallel_map([0, 1, 2, 3, 4], fn, max_workers=4) == [0, 2, 4]


def test_skip_none_false_keeps_none_positionally() -> None:
    """skip_none=False keeps None results in place."""

    def fn(x: int):
        return x if x % 2 == 0 else None

    assert parallel_map([0, 1, 2, 3], fn, max_workers=4, skip_none=False) == [0, None, 2, None]


def test_context_propagates_into_workers() -> None:
    """Each worker sees the caller's contextvar (propagate_context default)."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("cv", default="unset")
    var.set("parent-value")

    def fn(_x: int) -> str:
        return var.get()

    assert parallel_map([1, 2, 3], fn, max_workers=3) == [
        "parent-value",
        "parent-value",
        "parent-value",
    ]


def test_context_not_propagated_when_disabled() -> None:
    """propagate_context=False runs fn raw — workers do NOT see the parent var."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("cv2", default="unset")
    var.set("parent-value")

    def fn(_x: int) -> str:
        return var.get()

    # A raw ThreadPoolExecutor worker starts from the default context.
    assert parallel_map([1, 2, 3], fn, max_workers=3, propagate_context=False) == [
        "unset",
        "unset",
        "unset",
    ]


def test_each_task_gets_independent_context_copy() -> None:
    """A mutation a worker makes to a contextvar must not leak across tasks —
    each task runs in its own fresh copy."""
    var: contextvars.ContextVar[int] = contextvars.ContextVar("cv3", default=0)
    var.set(100)

    def fn(x: int) -> int:
        # Mutate within this task's own context copy.
        var.set(var.get() + x)
        return var.get()

    # Every task starts from 100 (the parent snapshot), not from another task's
    # mutation, so result[i] == 100 + items[i].
    assert parallel_map([1, 2, 3], fn, max_workers=3) == [101, 102, 103]
    # The parent's own value is untouched by the worker copies.
    assert var.get() == 100


def test_fast_fail_reraises_with_traceback() -> None:
    """The first worker exception propagates with its original type/message."""

    class _Boom(RuntimeError):
        pass

    def fn(x: int) -> int:
        if x == 2:
            raise _Boom("kaboom")
        return x

    with pytest.raises(_Boom, match="kaboom"):
        parallel_map([0, 1, 2, 3], fn, max_workers=4)


def test_on_first_exception_fires_once_before_raise() -> None:
    """on_first_exception is invoked exactly once and before the failure
    propagates to the caller."""
    events: list[str] = []
    lock = threading.Lock()

    def hook() -> None:
        with lock:
            events.append("abandoned")

    def fn(x: int) -> int:
        if x == 1:
            with lock:
                events.append("raised")
            raise ValueError("nope")
        return x

    with pytest.raises(ValueError):
        parallel_map([1], fn, max_workers=1, on_first_exception=hook)

    assert events.count("abandoned") == 1
    # The hook ran as part of failure handling, after the worker raised.
    assert "raised" in events
    assert events.index("raised") < events.index("abandoned")


def test_fast_fail_does_not_wait_for_inflight_tasks() -> None:
    """A fast failure surfaces without joining a slow sibling still in flight."""
    release = threading.Event()
    slow_finished = {"done": False}

    def fn(x: int) -> int:
        if x == 0:
            raise RuntimeError("fast")
        release.wait(timeout=10)
        slow_finished["done"] = True
        return x

    try:
        with pytest.raises(RuntimeError, match="fast"):
            parallel_map([0, 1], fn, max_workers=2)
        # The slow task was left running in the background, not awaited.
        assert slow_finished["done"] is False
    finally:
        release.set()


def test_invalid_max_workers_rejected() -> None:
    """max_workers < 1 is a precondition violation."""
    with pytest.raises(AssertionError):
        parallel_map([1, 2], lambda x: x, max_workers=0)
