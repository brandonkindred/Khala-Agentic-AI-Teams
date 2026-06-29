"""Shared bounded parallel-map driver with contextvar propagation.

:func:`parallel_map` consolidates the "fan a per-item function across a bounded
``ThreadPoolExecutor``" pattern that several teams had each hand-rolled with
subtly different ordering, error-handling, and — most importantly —
context-propagation semantics. A raw ``ThreadPoolExecutor`` does **not** copy
contextvars into its worker threads, so every fan-out site must wrap submissions
in ``contextvars.copy_context().run(...)`` or it silently drops the LLM
attribution / request-id contextvars (see ``llm_service.attribution``). Routing
all fan-out through this one helper fixes worker bounds, exception propagation,
and context propagation once instead of per-team. It is stdlib-only and lives in
``shared_concurrency`` so any team can use it without extra dependencies. See
``shared_concurrency/README.md`` for the rationale and usage examples.
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
) -> list:
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
            attribution / request-id) are visible in the worker. Set False only
            for CPU-only callers that explicitly want no propagation.
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

    Returns:
        The list of results — element type ``R``, plus ``None`` entries when
        ``skip_none`` is False (the annotation is a bare ``list`` because the
        element type depends on the ``skip_none`` flag). Length equals
        ``len(items)`` unless ``skip_none`` dropped some (or a worker raised, in
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
        - When ``propagate_context`` is True, this function is called on the
          thread whose context should be snapshotted into the workers.

    Postconditions:
        - Empty *items* returns ``[]`` without creating an executor.
        - At most ``min(max_workers, len(items))`` workers run concurrently.
        - Error policy is **fast-fail**: the first worker exception is observed as
          it happens (never delayed behind a slower earlier task), pending tasks
          are cancelled, ``on_first_exception`` fires once, and the exception
          propagates with its original traceback. Already-running tasks are left
          to finish in the background rather than blocking the failure.
        - On success with ``preserve_order`` True, ``result[i]`` corresponds to
          ``items[i]`` (before any ``skip_none`` filtering).

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

    n = len(items)
    if n == 0:
        return []

    workers = min(max_workers, n)

    def _submit(pool: ThreadPoolExecutor, item: T):
        # A fresh context copy per task: a single Context can't be entered
        # concurrently, and each worker must see the parent's attribution.
        if propagate_context:
            return pool.submit(contextvars.copy_context().run, fn, item)
        return pool.submit(fn, item)

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [_submit(pool, item) for item in items]
        index_of = {fut: i for i, fut in enumerate(futures)}
        ordered: list = [None] * n
        completion: list = []
        # Drain in completion order so the first failure surfaces as it happens —
        # never delayed behind a slower earlier task — while ``ordered`` records
        # each result at its submission index for the preserve-order return.
        for fut in as_completed(futures):
            value = fut.result()  # re-raises the worker's exception with its traceback
            ordered[index_of[fut]] = value
            completion.append(value)
    except BaseException as exc:
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
            pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)

    results = ordered if preserve_order else completion
    if skip_none:
        return [r for r in results if r is not None]
    return results
