"""Shared bounded parallel-map driver with contextvar propagation.

:func:`parallel_map` consolidates the "fan a per-item function across a bounded
``ThreadPoolExecutor``" pattern that several teams had each hand-rolled with
subtly different ordering, error-handling, and — most importantly —
context-propagation semantics. A raw ``ThreadPoolExecutor`` does **not** copy
contextvars into its worker threads, so every fan-out site must wrap submissions
in ``contextvars.copy_context().run(...)`` or it silently drops the LLM
attribution / request-id contextvars (see ``llm_service.attribution``) and the
job's ``trace_id`` (see ``shared.observability.trace_context``). Routing
all fan-out through this one helper fixes worker bounds, exception propagation,
and context propagation once instead of per-team. It is stdlib-only and lives in
``shared.concurrency`` so any team can use it without extra dependencies. See
``shared/concurrency/README.md`` for the rationale and usage examples.

Requires Python 3.9+ (uses ``ThreadPoolExecutor.shutdown(cancel_futures=...)``).
The project targets Python 3.10 (``backend/pyproject.toml`` ``target-version =
"py310"``), so this is always satisfied.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from typing import Callable, Collection, Optional, TypeVar

logger = logging.getLogger(__name__)

__all__ = ["parallel_map"]

T = TypeVar("T")
R = TypeVar("R")

# How often the timeout loop wakes to re-check pending futures' deadlines —
# bounds how late a timeout is noticed without busy-looping.
_TIMEOUT_POLL_INTERVAL_SECONDS = 0.05


def _is_int_not_bool(value: object) -> bool:
    # ``bool`` is a subclass of ``int`` in Python, so an ``isinstance(x, int)``
    # check alone would silently accept ``True``/``False`` as valid integers.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_not_bool(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _run_with_timeout(
    futures: list[Future],
    index_of: dict[Future, int],
    start_times: list[Optional[float]],
    finish_times: list[Optional[float]],
    submitted_items: list,
    ordered: list,
    completion: list,
    timeout: float,
    workers: int,
    on_timeout: Optional[Callable[[T], None]],
) -> bool:
    """Drain *futures* in completion order, degrading any item that reaches or
    exceeds *timeout* (measured from its own ``start_times``/``finish_times``
    entries) to ``None`` instead of aborting.

    Preconditions:
        - ``futures``, ``start_times``, ``finish_times``, ``submitted_items``,
          and ``ordered`` all have the same length, and every future in
          ``futures`` maps (via ``index_of``) to a valid index into all four.
        - ``ordered`` is pre-sized (e.g. ``[None] * n``) — this function only
          assigns ``ordered[i]``, never appends to it. ``completion`` starts
          empty; this function only appends to it.
        - ``start_times[i]`` is written by item ``i``'s own worker before
          ``fn`` runs, and ``finish_times[i]`` right after, whenever
          ``timeout`` is not ``None`` for the caller — both are ``None`` for
          an item that hasn't started yet.
        - ``timeout > 0`` and ``workers >= 1`` (both already validated by
          the caller, ``parallel_map``).

    Postconditions:
        - If no worker future raises an exception, every index reachable
          through ``index_of`` gets exactly one write to ``ordered`` and
          exactly one append to ``completion`` — either the future's real
          result, or ``None`` for a degraded item. A worker exception still
          re-raises immediately (see Preconditions on ``timeout``/fast-fail
          above), leaving any not-yet-processed index unwritten — the same
          fast-fail contract ``parallel_map`` documents for ``timeout=None``.
        - Returns ``True`` iff at least one item was degraded; the caller
          uses this to choose the ``wait_for_stragglers`` shutdown behavior.
        - Mutates ``ordered`` and ``completion`` from the caller's state
          (plus reading, never writing, ``start_times``/``finish_times``/
          ``submitted_items``) — the sets and counters used internally to
          track stragglers are local to this call. Also invokes the
          caller-supplied ``on_timeout(item)`` in this thread for each
          degraded item (logging, not propagating, an ``Exception`` it
          raises) — callers relying on ``on_timeout`` for side effects
          should treat those as happening synchronously, here.
    """
    timed_out = False

    def _degrade(i: int) -> None:
        nonlocal timed_out
        timed_out = True
        # Record the degrade (ordered/completion) before letting a hook
        # BaseException (e.g. SystemExit) propagate: timed_out is already
        # True at this point, so the caller must never see that combined
        # with a missing entry for this index. An Exception is logged and
        # swallowed instead — the existing "one bad hook can't take down
        # the batch" contract.
        hook_error: Optional[BaseException] = None
        if on_timeout is not None:
            try:
                on_timeout(submitted_items[i])
            except Exception:
                logger.exception("on_timeout hook raised for a degraded item; the item's result remains None")
            except BaseException as exc:
                hook_error = exc
        ordered[i] = None
        completion.append(None)
        if hook_error is not None:
            raise hook_error

    pending = set(futures)
    poll_interval = min(timeout, _TIMEOUT_POLL_INTERVAL_SECONDS)
    # Futures we've discarded from ``pending`` as over budget but whose
    # worker thread we don't know is free yet. Each is only dropped once
    # *that specific future* actually finishes (``fut.done()``) — unrelated
    # progress elsewhere in the pool (other queued items starting or
    # completing on a healthy worker) proves nothing about whether this one
    # ever will, so it must never be credited as this straggler recovering.
    abandoned: set[Future] = set()
    # Set the first moment every worker slot is presumed occupied by an
    # abandoned straggler. A single instant of saturation proves nothing on
    # its own — the straggler may simply be a bit slow and about to return,
    # freeing its worker for the next queued item — so a not-yet-started
    # item only gets force-degraded once saturation has persisted for a
    # further full ``timeout`` with no sign of it starting, giving it the
    # same budget a normal item gets before it's called stuck.
    saturation_since: Optional[float] = None

    while pending:
        done, pending = wait(pending, timeout=poll_interval, return_when=FIRST_COMPLETED)
        now = time.monotonic()
        for fut in done:
            i = index_of[fut]
            started_at, finished_at = start_times[i], finish_times[i]
            # Measured against the worker's own recorded finish time, not
            # this observing thread's clock — this loop can be delayed
            # elsewhere (e.g. a slow on_timeout hook for a different item),
            # and a task that genuinely finished within budget must not be
            # blamed for a delay in us noticing. Genuine worker exceptions
            # still always fast-fail regardless of timing; only a
            # *successful*-but-genuinely-overdue completion is degraded.
            if (
                fut.exception() is None
                and started_at is not None
                and finished_at is not None
                and finished_at - started_at >= timeout
            ):
                _degrade(i)
            else:
                value = fut.result()  # re-raises the worker's exception with its traceback
                ordered[i] = value
                completion.append(value)
        # Drop any abandoned straggler that has since actually finished —
        # only this (not unrelated pool progress) proves its worker is free.
        abandoned = {fut for fut in abandoned if not fut.done()}
        if len(abandoned) < workers:
            saturation_since = None
        if not pending:
            break
        now = time.monotonic()
        for fut in list(pending):
            i = index_of[fut]
            started_at = start_times[i]
            if started_at is not None and now - started_at >= timeout:
                # Degrade, don't abort: this item alone becomes None; its
                # future is left running in the background (it already
                # started, so it can't be cancelled) and the rest of the
                # batch is unaffected. This never touches ``abort`` — a
                # timeout is not a worker exception.
                pending.discard(fut)
                abandoned.add(fut)
                _degrade(i)
        if pending and len(abandoned) >= workers:
            if saturation_since is None:
                saturation_since = now
            elif now - saturation_since >= timeout:
                # Saturation has persisted for a full timeout with no worker
                # freeing up: every slot is genuinely stuck, so a
                # not-yet-started item behind it can never be scheduled and
                # would spin forever. Cancel it while it's still queued —
                # this both stops it from ever running unobserved in the
                # background after we return, and doubles as the
                # correctness check: if cancel() fails, a worker freed up
                # and started it after all (race), so it's left in
                # ``pending`` to be tracked normally against its own start
                # time instead.
                for fut in list(pending):
                    i = index_of[fut]
                    if start_times[i] is None and fut.cancel():
                        pending.discard(fut)
                        _degrade(i)

    return timed_out


def parallel_map(
    items: Collection[T],
    fn: Callable[[T], R],
    *,
    max_workers: int,
    preserve_order: bool = True,
    skip_none: bool = True,
    propagate_context: bool = True,
    on_first_exception: Optional[Callable[[], None]] = None,
    wait_for_stragglers: bool = False,
    timeout: Optional[float] = None,
    on_timeout: Optional[Callable[[T], None]] = None,
) -> list[Optional[R]]:
    """Run ``fn(item)`` concurrently across *items* in a bounded thread pool.

    This is the single, correct home for the bounded-fan-out pattern. The
    defaults match the common case — preserve input order, skip ``None`` results,
    propagate the caller's context into every worker — so most call sites read as
    ``parallel_map(items, fn, max_workers=N)``.

    Scope note: all tasks are submitted up front (one future per item, plus an
    ``n``-sized result list), so memory is ``O(len(items))``. This is intended for
    bounded fan-out over modest collections — the existing callers pass at most a
    few dozen items — not for streaming over very large or unbounded inputs.

    Args:
        items: The inputs to map over (any sized iterable). Empty input
            short-circuits to ``[]``.
        fn: The per-item function, invoked once per item in a worker thread.
        max_workers: Upper bound on concurrent workers. The pool is sized at
            ``min(max_workers, len(items))`` so a small batch never spins up idle
            threads.
        preserve_order: When True (default), results come back in the same order
            as *items*; when False, in completion order.
        skip_none: When True (default), ``None`` results are filtered out (the
            "return ``None`` to skip this item" convention); when False, they are
            kept positionally.
        propagate_context: When True (default), each task runs inside a **fresh**
            ``contextvars.copy_context()`` so the caller's contextvars (LLM
            attribution / request-id, ``trace_id``) are visible in the worker.
            Set False only for CPU-only callers that explicitly want no
            propagation.
        on_first_exception: Optional zero-arg callback invoked exactly once, on
            the first worker **``Exception``**, **before** pending tasks are
            cancelled and the exception is re-raised. It is *not* called for a
            main-thread interrupt (``KeyboardInterrupt``/``SystemExit``) that lands
            while waiting — those still cancel and propagate, just without the hook.
            Lets a caller flip its own "abandoned" flag (e.g. under a progress
            lock) before any cancellation lands. If the hook itself raises an
            ``Exception``, that error is logged and discarded so the original
            worker exception is the one that propagates; a ``BaseException`` from
            the hook (e.g. ``KeyboardInterrupt``) is left to propagate, never
            swallowed.
        wait_for_stragglers: When True, the first worker exception still
            cancels every not-yet-started task, but ``parallel_map`` blocks
            until every **already-running** task finishes before re-raising —
            no task is left executing in the background after the call
            returns. Default False keeps the original fast-fail contract
            (return immediately; already-running tasks finish unobserved).
            Opt in when a worker's side effects (provider fetches, cache
            writes, subprocess calls) must not outlive the caller's failure
            handling.
        timeout: Optional per-item wall-clock budget in seconds, measured
            from when that item's ``fn`` call actually **starts running**
            in its worker thread (not from submission — so an item still
            queued behind a busy worker isn't charged for its wait). When a
            task reaches or exceeds its own budget, only that task is degraded to a
            ``None`` result (subject to ``skip_none`` like any other
            ``None``); the rest of the batch continues unaffected. Default
            ``None`` disables timeouts entirely — the loop is byte-for-byte
            the pre-existing implementation in that case.
        on_timeout: Optional one-arg callback invoked with the timed-out
            *item* (not the result) when a per-item timeout fires. Mirrors
            ``on_first_exception``'s hook contract: a raising callback is
            logged and discarded rather than replacing/masking the
            degrade, so one bad hook can't take down the batch.

    Returns:
        ``list[Optional[R]]`` — results in input (or completion) order. With the
        default ``skip_none=True`` every element is an ``R`` (the ``None``\\ s are
        filtered out); with ``skip_none=False`` a ``None`` element marks a task
        whose ``fn`` returned ``None``. The annotation is ``Optional[R]`` rather
        than ``R`` because it must stay sound for the ``skip_none=False`` path;
        callers on that path should treat elements as possibly ``None``. Length
        equals ``len(items)`` unless ``skip_none`` dropped some — including any
        item degraded to ``None`` by a ``timeout`` — or a worker raised (in
        which case nothing is returned).

    Preconditions (enforced — invalid input raises at the boundary, and the
    checks survive ``python -O`` which strips ``assert``):
        - ``fn`` is callable (else ``TypeError``) and safe to invoke concurrently
          from worker threads.
        - ``max_workers`` is an ``int`` (else ``TypeError``) and >= 1 (else
          ``ValueError``).
        - ``items`` is a sized iterable — ``__len__`` and ``__iter__`` (else
          ``TypeError``). Any sized iterable is accepted, not only a ``Sequence``;
          the helper only needs to size and iterate the input.
        - ``on_first_exception``, when not ``None``, is callable (else
          ``TypeError``).
        - ``timeout``, when not ``None``, is an ``int``/``float`` (else
          ``TypeError``) and > 0 (else ``ValueError``).
        - ``on_timeout``, when not ``None``, is callable (else ``TypeError``).
        - When ``propagate_context`` is True, this function is called on the
          thread whose context should be snapshotted into the workers.

    Postconditions:
        - Empty *items* returns ``[]`` without creating an executor.
        - At most ``min(max_workers, len(items))`` workers run concurrently.
        - Error policy is **fast-fail**: the first worker exception is observed as
          it happens (never delayed behind a slower earlier task). An internal
          abort flag is set *before* anything else — before ``on_first_exception``
          and before ``ThreadPoolExecutor.shutdown(cancel_futures=True)`` — so a
          not-yet-started task that hasn't begun ``fn`` yet skips it, narrowing
          (not eliminating: a task that already slipped past its own check races
          the flag) the window in which ``ThreadPoolExecutor``'s own worker loop
          can pull another queued item before cancellation lands. With the default
          ``wait_for_stragglers=False``, already-running tasks (including any that
          won that race) are left to finish in the background rather than blocking
          the failure; with ``wait_for_stragglers=True``, the shutdown instead
          blocks until every already-running task finishes before the exception
          propagates.
        - On success with ``preserve_order`` True, ``result[i]`` corresponds to
          ``items[i]`` (before any ``skip_none`` filtering).
        - When ``timeout`` is set, a task that reaches or exceeds it is **degraded, not
          aborted**: its result becomes ``None`` (``on_timeout`` is invoked
          with the original item first, if provided), the rest of the batch
          keeps running unaffected, and this does *not* trip the fast-fail
          ``abort`` flag — a timeout is not a worker exception. A degraded
          task's future is still running in the background at that point
          (it cannot be cancelled once started); the final
          ``ThreadPoolExecutor.shutdown`` call blocks on it exactly when
          ``wait_for_stragglers`` is True, same as an already-running task
          left behind by the fast-fail path. If a *different* item raises a
          genuine exception, the existing fast-fail policy still applies on
          top of any timeout bookkeeping already done.
        - If enough items degrade that every worker slot is presumed occupied
          by an abandoned straggler (``workers`` such degrades reached), a
          not-yet-started task behind them gets one further full ``timeout``
          as a grace period — the same budget a running item gets — in case
          a straggler was merely slow and about to free its worker. Only if
          that saturation persists past the grace period is the queued task
          finally degraded (``on_timeout``/``None``), and its future is
          cancelled first so it can never start running unobserved in the
          background after this call returns; if the cancel fails (a worker
          freed up and started it after all), it's left alone and tracked
          normally against its own deadline instead. A task that has already
          started is never affected by this: it keeps being watched and
          resolves within its own deadline regardless.
        - With ``timeout=None`` (the default), behavior is identical to the
          version of this function without timeout support — the code path
          taken is the same, not merely equivalent.

    Invariants:
        - Each task receives its own ``copy_context()`` (a single ``Context``
          cannot be entered concurrently), so workers never share mutable context
          state.
    """
    # Explicit raises rather than ``assert`` so the preconditions still hold under
    # ``python -O`` (which strips asserts) — an invalid argument fails here with a
    # clear error instead of a confusing downstream ``TypeError``/``ValueError``
    # from ``ThreadPoolExecutor`` or from calling a non-callable.
    if not callable(fn):
        raise TypeError("fn must be callable")
    if not _is_int_not_bool(max_workers):
        raise TypeError("max_workers must be an int")
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    if not (hasattr(items, "__len__") and hasattr(items, "__iter__")):
        raise TypeError("items must be a sized iterable")
    if on_first_exception is not None and not callable(on_first_exception):
        # Validate up front like ``fn``: an uncallable hook would otherwise only
        # surface inside the failure handler, where it's caught and logged, hiding
        # the misconfiguration behind whatever worker error happened to occur.
        raise TypeError("on_first_exception must be callable")
    if timeout is not None:
        if not _is_number_not_bool(timeout):
            raise TypeError("timeout must be an int or float")
        # ``not (timeout > 0)`` (rather than ``timeout <= 0``) also rejects NaN:
        # every comparison with NaN is False, so ``NaN <= 0`` would silently pass
        # while ``NaN > 0`` is False too, giving ``not False`` == True here.
        if not (timeout > 0):
            raise ValueError("timeout must be > 0")
    if on_timeout is not None and not callable(on_timeout):
        raise TypeError("on_timeout must be callable")

    n = len(items)
    if n == 0:
        return []

    workers = min(max_workers, n)
    # Set the instant a first exception is caught (see the ``except`` block
    # below) so a task that hasn't called ``fn`` yet can skip it — this is
    # what ``_guarded`` checks. It only narrows the race documented in the
    # Postconditions above; a task that already passed the check is treated
    # as legitimately "already running".
    abort = threading.Event()
    if timeout is None:
        # Exactly the pre-existing implementation for this path — no
        # timeout bookkeeping allocated or touched per call.
        start_times: "list[Optional[float]]" = []
        finish_times: "list[Optional[float]]" = []

        def _guarded(i: int, item: T) -> Optional[R]:
            if abort.is_set():
                return None
            return fn(item)
    else:
        # Only populated (and only consulted) when ``timeout`` is set — each
        # cell is written exactly once, by that item's own worker thread, so
        # no lock is needed. ``None`` means "not started yet" (queued behind
        # a busy worker), which must never count against the item's own
        # budget.
        start_times = [None] * n
        # Recorded by the worker itself right when ``fn`` returns (or
        # raises) — not by whatever thread later happens to notice the
        # future is done. ``parallel_map``'s own loop can be delayed
        # elsewhere (e.g. a slow ``on_timeout`` hook for a different item),
        # so measuring "was this over budget" against the *observing*
        # thread's clock would blame a task for a delay that was never its
        # own; this is the timestamp of the work actually finishing.
        finish_times = [None] * n

        def _guarded(i: int, item: T) -> Optional[R]:
            if abort.is_set():
                return None
            start_times[i] = time.monotonic()
            try:
                return fn(item)
            finally:
                finish_times[i] = time.monotonic()

    def _submit(pool: ThreadPoolExecutor, i: int, item: T):
        # A fresh context copy per task: a single Context can't be entered
        # concurrently, and each worker must see the parent's attribution.
        if propagate_context:
            return pool.submit(contextvars.copy_context().run, _guarded, i, item)
        return pool.submit(_guarded, i, item)

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        # Captured positionally during the single pass over *items* so a
        # timeout's ``on_timeout(item)`` call never re-indexes the original
        # ``items`` — which only has to support ``__len__``/``__iter__`` per
        # the precondition above, not ``__getitem__`` (e.g. a ``set`` is a
        # valid sized iterable but isn't subscriptable).
        submitted_items: list[T] = []
        futures = []
        for i, item in enumerate(items):
            submitted_items.append(item)
            futures.append(_submit(pool, i, item))
        index_of = {fut: i for i, fut in enumerate(futures)}
        ordered: list = [None] * n
        completion: list = []
        timed_out = False
        if timeout is None:
            # Drain in completion order so the first failure surfaces as it happens
            # — never delayed behind a slower earlier task — while ``ordered``
            # records each result at its submission index for preserve-order.
            for fut in as_completed(futures):
                value = fut.result()  # re-raises the worker's exception with its traceback
                ordered[index_of[fut]] = value
                completion.append(value)
        else:
            # Same completion-order draining as above, interleaved with a
            # per-item deadline check on whatever's still pending: a short
            # poll bounds how late a timeout is noticed without busy-looping.
            timed_out = _run_with_timeout(
                futures,
                index_of,
                start_times,
                finish_times,
                submitted_items,
                ordered,
                completion,
                timeout,
                workers,
                on_timeout,
            )
    except BaseException as exc:
        # Set before anything else below (including the hook, which is
        # caller-supplied and may be slow) to give not-yet-started tasks the
        # best chance of seeing it in ``_guarded`` before a freed worker
        # thread pulls them off the queue.
        abort.set()
        # Fire the caller's hook only for an actual worker failure (an
        # ``Exception``), never for a main-thread interrupt — ``KeyboardInterrupt``
        # / ``SystemExit`` are ``BaseException`` but not ``Exception`` — that
        # merely landed while we were waiting; the contract is "first worker
        # exception". Cleanup (cancel pending, re-raise) still runs for any exit.
        try:
            if on_first_exception is not None and isinstance(exc, Exception):
                # A raising hook must not replace the worker exception we are about
                # to propagate, or the original error context is lost; log and
                # discard a hook ``Exception``. A hook ``BaseException`` is left to
                # propagate — but the ``finally`` still shuts the pool down first,
                # so the executor is never leaked on any exit path.
                try:
                    on_first_exception()
                except Exception:
                    logger.exception(
                        "on_first_exception hook raised; the original worker "
                        "exception will still propagate"
                    )
        finally:
            pool.shutdown(wait=wait_for_stragglers, cancel_futures=True)
        raise
    # A degraded (timed-out) item's future may still be running in the
    # background — mirror the fast-fail path's ``wait_for_stragglers``
    # contract instead of always blocking, so the "degrade, don't abort"
    # promise holds for the success path too. With no timeouts, this is
    # exactly the original unconditional ``wait=True``.
    pool.shutdown(wait=wait_for_stragglers if timed_out else True)

    results = ordered if preserve_order else completion
    if skip_none:
        return [r for r in results if r is not None]
    return results
