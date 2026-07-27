"""Unit tests for the shared :func:`parallel_map` driver.

Covers every variation point the migrated callers rely on: empty-input
short-circuit, worker bounding, order preservation (and completion order),
``skip_none`` filtering, contextvar propagation (and its opt-out), the
fast-fail error policy with the ``on_first_exception`` hook, the opt-in
``wait_for_stragglers`` policy for callers that must not leave in-flight work
running in the background after a failure, and the opt-in per-item
``timeout``/``on_timeout`` degrade-not-abort policy.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time

import pytest

from shared.concurrency.parallel_map import parallel_map


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


def test_trace_id_propagates_into_workers() -> None:
    """A bound trace_id (shared.observability.trace_context) survives fan-out,
    proving parallel_map's generic contextvar propagation covers it too."""
    from shared.observability import bind_trace_id, current_trace_id

    def fn(_x: int) -> str:
        return current_trace_id()

    with bind_trace_id("trace-abc"):
        assert parallel_map([1, 2, 3], fn, max_workers=3) == ["trace-abc"] * 3


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


def test_wait_for_stragglers_blocks_until_inflight_tasks_finish() -> None:
    """wait_for_stragglers=True still fails fast on the exception itself, but
    blocks until an already-running sibling finishes before re-raising — no
    task is left executing in the background after parallel_map returns."""
    slow_started = threading.Event()
    slow_finished = {"done": False}

    def fn(x: int) -> int:
        if x == 0:
            slow_started.wait(timeout=10)  # don't raise until the sibling is running
            raise RuntimeError("fast")
        slow_started.set()
        time.sleep(0.1)
        slow_finished["done"] = True
        return x

    with pytest.raises(RuntimeError, match="fast"):
        parallel_map([0, 1], fn, max_workers=2, wait_for_stragglers=True)

    # The slow task was awaited before the exception surfaced.
    assert slow_finished["done"] is True


def test_wait_for_stragglers_still_cancels_not_yet_started_tasks() -> None:
    """wait_for_stragglers=True only waits for *already-running* tasks — tasks
    still queued behind a single busy worker are cancelled rather than run to
    completion, exactly like the default policy.

    Item 1 may or may not slip in: a freed worker racing the internal abort
    flag (set the instant the first exception is caught — see
    ``test_abort_is_set_before_a_slow_hook_so_queued_tasks_never_start`` for
    the deterministic half of this contract) can win that single race. But
    items 2 and 3 can't — each item that *does* start sleeps a full second,
    which is ample time for the flag to land before the next one would be
    dispatched — so the leak is bounded to at most one extra item, never the
    whole remaining queue.
    """
    started = {"count": 0}
    lock = threading.Lock()

    def fn(x: int) -> int:
        with lock:
            started["count"] += 1
        if x == 0:
            raise RuntimeError("fast")
        time.sleep(1)
        return x

    with pytest.raises(RuntimeError, match="fast"):
        parallel_map([0, 1, 2, 3], fn, max_workers=1, wait_for_stragglers=True)

    assert 1 <= started["count"] <= 2


def test_abort_is_set_before_a_slow_hook_so_queued_tasks_never_start() -> None:
    """The internal abort flag is set before ``on_first_exception`` runs, so
    a slow hook can't widen the window for a freed worker to start another
    queued item — cancellation would otherwise only happen after the hook
    returns, inside ``shutdown(cancel_futures=True)``. Unlike the sibling
    "no hook" test above, this scenario is fully deterministic: the hook's
    sleep gives the freed worker ample time to observe the flag, which was
    already set before the hook was even called."""
    started = {"count": 0}
    lock = threading.Lock()

    def fn(x: int) -> int:
        with lock:
            started["count"] += 1
        if x == 0:
            raise RuntimeError("fast")
        return x

    def slow_hook() -> None:
        time.sleep(0.2)

    with pytest.raises(RuntimeError, match="fast"):
        parallel_map(
            [0, 1, 2, 3],
            fn,
            max_workers=1,
            wait_for_stragglers=True,
            on_first_exception=slow_hook,
        )

    assert started["count"] == 1


def test_on_first_exception_hook_raising_does_not_mask_worker_error(caplog) -> None:
    """A raising hook is logged and discarded — the original worker exception
    still propagates rather than being replaced by the hook's error."""

    class _Worker(RuntimeError):
        pass

    def hook() -> None:
        raise KeyError("hook blew up")

    def fn(_x: int) -> int:
        raise _Worker("real failure")

    with caplog.at_level(logging.ERROR, logger="shared.concurrency.parallel_map"):
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

    import shared.concurrency.parallel_map  # noqa: F401 — ensure the module is imported

    # The package re-exports the ``parallel_map`` function under the same name as
    # the submodule, so reach the module object through sys.modules.
    pm = sys.modules["shared.concurrency.parallel_map"]

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


def test_timeout_degrades_slow_item_success_items_unaffected() -> None:
    """A per-item timeout degrades only the slow item to None; other items
    still complete and return their real values, and on_timeout fires once
    with the timed-out item."""
    timed_out_items: list[int] = []

    def on_timeout(item: int) -> None:
        timed_out_items.append(item)

    def fn(x: int) -> int:
        if x == 0:
            time.sleep(1.0)  # exceeds the timeout below
        return x * 10

    out = parallel_map(
        [0, 1, 2],
        fn,
        max_workers=3,
        skip_none=False,
        timeout=0.1,
        on_timeout=on_timeout,
    )

    assert out == [None, 10, 20]
    assert timed_out_items == [0]


def test_timeout_degrades_queued_item_stuck_behind_a_hung_worker() -> None:
    """With max_workers=1, a permanently hung first item must not starve a
    queued second item forever: once the number of abandoned stragglers
    reaches the worker count, a not-yet-started item is degraded too instead
    of parallel_map hanging indefinitely."""
    release = threading.Event()

    def fn(x: int) -> int:
        if x == 0:
            release.wait(timeout=10)  # simulates a permanent hang
            return x
        return x * 10

    try:
        out = parallel_map([0, 1], fn, max_workers=1, skip_none=False, timeout=0.05)
        # Item 1 never got a worker slot (item 0's hung thread never freed one
        # up) so it degrades to None exactly like the timed-out item 0.
        assert out == [None, None]
    finally:
        release.set()


def test_timeout_queued_item_still_runs_behind_a_merely_slow_not_hung_worker() -> None:
    """A queued item behind a slightly-over-budget (but not permanently hung)
    worker must still get to run and receive its own full budget, rather than
    being force-degraded the instant every worker looks saturated."""
    release = threading.Event()

    def fn(x: int) -> int:
        if x == 0:
            release.wait(timeout=2)  # a bit over the timeout, then returns
            return x
        return x * 10

    threading.Timer(0.1, release.set).start()
    out = parallel_map([0, 1], fn, max_workers=1, skip_none=False, timeout=0.05)

    assert out == [None, 10]


def test_timeout_degrades_a_late_but_successfully_completed_future() -> None:
    """A future that genuinely ran past its own budget must be degraded even
    if it happens to complete (successfully) while parallel_map's loop is
    busy elsewhere (here: a slow on_timeout hook for a different, hung
    item) and only gets to observe it as already-``done`` afterward — it
    must not slip through as a real result just because the loop was late
    to check its deadline while it was still pending."""
    release_hang = threading.Event()

    def fn(x: int) -> int:
        if x == 0:
            release_hang.wait(timeout=5)  # permanently hung, released in finally
            return 0
        if x == 1:
            # Comfortably within budget; frees a worker at ~0.15s so item 2
            # starts well after item 0 (needed so item 0's own deadline is
            # reached — and its slow hook fires — before item 2's is, even
            # accounting for poll-interval jitter).
            time.sleep(0.15)
            return 11
        # Starts at ~0.15s, finishes at ~0.55s: 0.4s of real runtime against
        # a 0.3s budget — genuinely over budget, and finishes while item 0's
        # slow hook (below) still has parallel_map's loop tied up.
        time.sleep(0.40)
        return 22

    def on_timeout(item: int) -> None:
        if item == 0:
            # Long enough to span item 2's real completion (~0.55s) before
            # the loop gets back around to calling `wait()` again.
            time.sleep(0.4)

    try:
        out = parallel_map(
            [0, 1, 2],
            fn,
            max_workers=2,
            skip_none=False,
            timeout=0.3,
            on_timeout=on_timeout,
        )
        assert out == [None, 11, None]
    finally:
        release_hang.set()


def test_timeout_saturation_resets_once_the_pool_shows_progress() -> None:
    """The saturation grace-period fallback must not accumulate forever: once
    an abandoned straggler's worker resumes and makes real progress (here:
    several small in-budget items complete one after another on the single
    worker), later queued items must still get to run instead of being
    force-cancelled just because a stale straggler count once reached the
    worker count."""
    release_hang = threading.Event()

    def fn(x: int) -> int:
        if x == 0:
            release_hang.wait(timeout=0.2)  # slightly over budget, then returns
            return 0
        time.sleep(0.01)  # comfortably within budget
        return x * 10

    threading.Timer(0.06, release_hang.set).start()
    try:
        out = parallel_map([0, 1, 2, 3], fn, max_workers=1, skip_none=False, timeout=0.05)
        # Item 0 alone degrades (it ran over budget); every item queued
        # behind it keeps running normally once the worker recovers.
        assert out == [None, 10, 20, 30]
    finally:
        release_hang.set()


def test_timeout_uses_item_not_index_for_non_subscriptable_items() -> None:
    """on_timeout receives the actual submitted item even when `items` is a
    sized-but-not-subscriptable iterable (e.g. a set), rather than indexing
    the original input with `items[i]`."""
    timed_out_items: list[int] = []

    def on_timeout(item: int) -> None:
        timed_out_items.append(item)

    def fn(x: int) -> int:
        if x == 0:
            time.sleep(1.0)
        return x

    out = parallel_map(
        {0, 1},
        fn,
        max_workers=2,
        skip_none=False,
        timeout=0.1,
        on_timeout=on_timeout,
    )

    assert None in out
    assert timed_out_items == [0]


def test_nan_timeout_rejected() -> None:
    """A NaN timeout is rejected up front rather than silently disabling the
    timeout check (NaN compares False against both <= 0 and > 0)."""
    with pytest.raises(ValueError):
        parallel_map([1, 2], lambda x: x, max_workers=2, timeout=float("nan"))


def test_timeout_none_default_is_unaffected() -> None:
    """timeout unset (default None) behaves exactly as before — no degrade
    logic engages even for a slow item."""

    def fn(x: int) -> int:
        time.sleep(0.05)
        return x * 10

    assert parallel_map([0, 1, 2], fn, max_workers=3) == [0, 10, 20]


def test_timeout_racing_a_genuine_exception_still_fast_fails() -> None:
    """When one item times out and a different item raises, the exception
    still propagates (fast-fail wins over a degrade)."""

    def fn(x: int) -> int:
        if x == 0:
            time.sleep(1.0)  # will time out
        if x == 1:
            raise RuntimeError("boom")
        return x

    with pytest.raises(RuntimeError, match="boom"):
        parallel_map([0, 1], fn, max_workers=2, timeout=0.1)


def test_timeout_wait_for_stragglers_blocks_on_degraded_item() -> None:
    """wait_for_stragglers=True blocks on a degraded (timed-out) item's
    still-running future before parallel_map returns."""
    finished = {"done": False}

    def fn(x: int) -> int:
        if x == 0:
            time.sleep(0.2)  # exceeds the timeout, but keeps running afterward
            finished["done"] = True
        return x

    out = parallel_map(
        [0, 1],
        fn,
        max_workers=2,
        skip_none=False,
        timeout=0.05,
        wait_for_stragglers=True,
    )

    assert out == [None, 1]
    assert finished["done"] is True


def test_timeout_without_wait_for_stragglers_does_not_block() -> None:
    """Default wait_for_stragglers=False does not block on a degraded item's
    still-running future — parallel_map returns without joining it."""
    finished = {"done": False}
    release = threading.Event()

    def fn(x: int) -> int:
        if x == 0:
            release.wait(timeout=10)
            finished["done"] = True
        return x

    try:
        out = parallel_map([0, 1], fn, max_workers=2, skip_none=False, timeout=0.05)
        assert out == [None, 1]
        assert finished["done"] is False
    finally:
        release.set()


def test_timeout_set_but_everything_completes_in_time() -> None:
    """A timeout configured but never hit — every item finishes before its
    deadline — behaves like a normal successful run."""

    def fn(x: int) -> int:
        return x * 10

    assert parallel_map([0, 1, 2], fn, max_workers=3, timeout=5.0) == [0, 10, 20]


def test_on_timeout_hook_raising_does_not_mask_the_degrade(caplog) -> None:
    """A raising on_timeout hook is logged and discarded — the timed-out
    item's result still degrades to None rather than the hook's error
    propagating."""

    def hook(_item: int) -> None:
        raise KeyError("hook blew up")

    def fn(x: int) -> int:
        if x == 0:
            time.sleep(1.0)
        return x

    with caplog.at_level(logging.ERROR, logger="shared.concurrency.parallel_map"):
        out = parallel_map(
            [0, 1],
            fn,
            max_workers=2,
            skip_none=False,
            timeout=0.1,
            on_timeout=hook,
        )

    assert out == [None, 1]
    assert "on_timeout hook raised" in caplog.text
    assert "hook blew up" in caplog.text


def test_invalid_timeout_rejected() -> None:
    """A non-positive or non-numeric timeout raises up front."""
    with pytest.raises(ValueError):
        parallel_map([1, 2], lambda x: x, max_workers=2, timeout=0)
    with pytest.raises(ValueError):
        parallel_map([1, 2], lambda x: x, max_workers=2, timeout=-1.0)
    with pytest.raises(TypeError):
        parallel_map([1, 2], lambda x: x, max_workers=2, timeout="1")
    with pytest.raises(TypeError):
        parallel_map([1, 2], lambda x: x, max_workers=2, timeout=True)


def test_non_callable_on_timeout_rejected() -> None:
    """A non-callable on_timeout raises TypeError up front."""
    with pytest.raises(TypeError):
        parallel_map([1, 2], lambda x: x, max_workers=2, timeout=1.0, on_timeout="nope")


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
