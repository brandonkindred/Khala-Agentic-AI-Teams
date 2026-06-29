"""Unit tests for the shared :func:`parallel_map` driver.

Covers every variation point the migrated callers rely on: empty-input
short-circuit, worker bounding, order preservation (and completion order),
``skip_none`` filtering, contextvar propagation (and its opt-out), and the
fast-fail error policy with the ``on_first_exception`` hook.
"""

from __future__ import annotations

import contextvars
import logging
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


def test_not_preserving_order_returns_every_result_once() -> None:
    """preserve_order=False returns every result exactly once.

    Completion order across a burst of near-simultaneous completions is
    inherently non-deterministic, so we assert the multiset of results — the
    deterministic property — rather than a specific sequence. (test_preserves_
    input_order_under_jittered_completion already proves ordering works when it
    is requested.)
    """

    def fn(x: int) -> int:
        return x * 10

    out = parallel_map([0, 1, 2, 3, 4], fn, max_workers=5, preserve_order=False)
    assert sorted(out) == [0, 10, 20, 30, 40]


def test_worker_bound_is_min_of_max_workers_and_len() -> None:
    """The pool is sized at min(max_workers, len(items)). With 6 items and
    max_workers=3, exactly 3 workers run at once.

    A ``threading.Barrier(3)`` makes this deterministic with no sleep: it only
    trips when 3 workers are simultaneously inside ``fn`` — so if the pool ran
    fewer than 3, the barrier would time out and the test would fail loudly —
    while the live counter confirms it never exceeds 3.
    """
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    gate = threading.Barrier(3, timeout=30)

    def fn(x: int) -> int:
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        gate.wait()  # blocks until 3 workers are here together
        with lock:
            live["now"] -= 1
        return x

    # 6 items but max_workers=3 → exactly 3 run concurrently (two batches).
    out = parallel_map(list(range(6)), fn, max_workers=3)
    assert sorted(out) == list(range(6))
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


def test_skip_none_filters_in_completion_order() -> None:
    """skip_none and preserve_order are independent: with both completion order
    and None-filtering, the result is the non-None values (multiset asserted,
    since completion order is non-deterministic)."""

    def fn(x: int):
        return x * 10 if x % 2 == 0 else None

    out = parallel_map([0, 1, 2, 3, 4], fn, max_workers=5, preserve_order=False, skip_none=True)
    assert sorted(out) == [0, 20, 40]


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


def test_on_first_exception_hook_raising_does_not_mask_worker_error(caplog) -> None:
    """A raising hook is logged and discarded — the original worker exception
    still propagates rather than being replaced by the hook's error."""

    class _Worker(RuntimeError):
        pass

    def hook() -> None:
        raise KeyError("hook blew up")

    def fn(_x: int) -> int:
        raise _Worker("real failure")

    with caplog.at_level(logging.ERROR, logger="shared_concurrency.parallel_map"):
        with pytest.raises(_Worker, match="real failure"):
            parallel_map([1], fn, max_workers=1, on_first_exception=hook)

    # The hook's failure is recorded (so it isn't silently swallowed) but does
    # not replace the worker exception that propagates.
    assert "on_first_exception hook raised" in caplog.text
    assert "hook blew up" in caplog.text


def test_pool_is_shut_down_even_when_hook_raises_baseexception(monkeypatch) -> None:
    """If on_first_exception raises a BaseException (e.g. KeyboardInterrupt), the
    pool is still shut down — cleanup must not be skipped — and the BaseException
    propagates."""
    import sys

    import shared_concurrency.parallel_map  # noqa: F401 — ensure the module is imported

    # The package re-exports the ``parallel_map`` function under the same name as
    # the submodule, so reach the module object through sys.modules.
    pm = sys.modules["shared_concurrency.parallel_map"]

    shutdown_calls: list = []
    real_pool_cls = pm.ThreadPoolExecutor

    class _SpyPool(real_pool_cls):
        def shutdown(self, *args, **kwargs):
            shutdown_calls.append((args, kwargs))
            return super().shutdown(*args, **kwargs)

    monkeypatch.setattr(pm, "ThreadPoolExecutor", _SpyPool)

    def hook() -> None:
        raise KeyboardInterrupt("interrupt inside hook")

    def fn(_x: int) -> int:
        raise RuntimeError("worker failure")

    with pytest.raises(KeyboardInterrupt):
        pm.parallel_map([1], fn, max_workers=1, on_first_exception=hook)

    assert shutdown_calls, "pool.shutdown must run even when the hook raises BaseException"


def test_invalid_max_workers_rejected() -> None:
    """max_workers < 1 raises ValueError (an explicit check, not an assert, so it
    holds under ``python -O``)."""
    with pytest.raises(ValueError):
        parallel_map([1, 2], lambda x: x, max_workers=0)


def test_non_int_max_workers_rejected() -> None:
    """A non-int max_workers (including bool) raises TypeError before the pool is
    built, instead of a confusing error from ThreadPoolExecutor."""
    with pytest.raises(TypeError):
        parallel_map([1, 2], lambda x: x, max_workers=2.5)
    with pytest.raises(TypeError):
        parallel_map([1, 2], lambda x: x, max_workers=True)


def test_non_callable_fn_rejected() -> None:
    """A non-callable fn raises TypeError up front."""
    with pytest.raises(TypeError):
        parallel_map([1, 2], "not-callable", max_workers=2)


def test_non_callable_on_first_exception_rejected() -> None:
    """A non-callable on_first_exception raises TypeError up front, rather than
    only failing (and being swallowed) inside the failure handler."""
    with pytest.raises(TypeError):
        parallel_map([1, 2], lambda x: x, max_workers=2, on_first_exception="nope")


def test_non_sized_items_rejected() -> None:
    """A non-sized iterable (e.g. a generator) raises TypeError rather than
    failing obscurely inside the helper."""
    with pytest.raises(TypeError):
        parallel_map((x for x in range(3)), lambda x: x, max_workers=2)


def test_sized_but_not_iterable_items_rejected() -> None:
    """An object with ``__len__`` but no ``__iter__`` is rejected up front, rather
    than passing the length check and then failing inside the fan-out loop."""

    class _SizedNotIterable:
        def __len__(self) -> int:
            return 3

    with pytest.raises(TypeError):
        parallel_map(_SizedNotIterable(), lambda x: x, max_workers=2)
