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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Callable, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

__all__ = ["parallel_map"]

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(
    items: Sequence[T],
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
            task exceeds its own budget, only that task is degraded to a
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
        equals ``len(items)`` unless ``skip_none`` dropped some (or a worker
        raised, in which case nothing is returned).

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
        - When ``timeout`` is set, a task that exceeds it is **degraded, not
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
    if not isinstance(max_workers, int) or isinstance(max_workers, bool):
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
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
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
    # Only populated (and only consulted) when ``timeout`` is set — each cell
    # is written exactly once, by that item's own worker thread, so no lock
    # is needed. ``None`` means "not started yet" (queued behind a busy
    # worker), which must never count against the item's own budget.
    start_times: list[Optional[float]] = [None] * n

    def _guarded(i: int, item: T) -> Optional[R]:
        if abort.is_set():
            return None
        if timeout is not None:
            start_times[i] = time.monotonic()
        return fn(item)

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
        submitted_items: list[T] = [None] * n  # type: ignore[list-item]
        futures = []
        for i, item in enumerate(items):
            submitted_items[i] = item
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
            pending = set(futures)
            poll_interval = min(timeout, 0.05)
            # Count of items degraded-and-abandoned so far: once a straggler is
            # discarded from ``pending`` its worker thread is never watched
            # again, so if it never returns that thread is gone for good.
            stragglers = 0
            # Set the first moment every worker slot is presumed occupied by
            # an abandoned straggler. A single instant of saturation proves
            # nothing on its own — the straggler may simply be a bit slow and
            # about to return, freeing its worker for the next queued item —
            # so a not-yet-started item only gets force-degraded once
            # saturation has persisted for a further full ``timeout`` with no
            # sign of it starting, giving it the same budget a normal item
            # gets before it's called stuck.
            saturation_since: Optional[float] = None

            def _degrade(i: int) -> None:
                nonlocal timed_out
                timed_out = True
                if on_timeout is not None:
                    try:
                        on_timeout(submitted_items[i])
                    except Exception:
                        logger.exception(
                            "on_timeout hook raised for a degraded item; "
                            "the item's result remains None"
                        )
                ordered[i] = None
                completion.append(None)

            while pending:
                done, pending = wait(pending, timeout=poll_interval, return_when=FIRST_COMPLETED)
                for fut in done:
                    value = fut.result()  # re-raises the worker's exception with its traceback
                    ordered[index_of[fut]] = value
                    completion.append(value)
                if not pending:
                    break
                now = time.monotonic()
                for fut in list(pending):
                    i = index_of[fut]
                    started_at = start_times[i]
                    if started_at is not None and now - started_at >= timeout:
                        # Degrade, don't abort: this item alone becomes None;
                        # its future is left running in the background (it
                        # already started, so it can't be cancelled) and the
                        # rest of the batch is unaffected. This never touches
                        # ``abort`` — a timeout is not a worker exception.
                        pending.discard(fut)
                        stragglers += 1
                        _degrade(i)
                if pending and stragglers >= workers:
                    if saturation_since is None:
                        saturation_since = now
                    elif now - saturation_since >= timeout:
                        # Saturation has persisted for a full timeout with no
                        # worker freeing up: every slot is genuinely stuck, so
                        # a not-yet-started item behind it can never be
                        # scheduled and would spin forever. Cancel it while
                        # it's still queued — this both stops it from ever
                        # running unobserved in the background after we
                        # return, and doubles as the correctness check: if
                        # cancel() fails, a worker freed up and started it
                        # after all (race), so it's left in ``pending`` to be
                        # tracked normally against its own start time instead.
                        for fut in list(pending):
                            i = index_of[fut]
                            if start_times[i] is None and fut.cancel():
                                pending.discard(fut)
                                _degrade(i)
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
