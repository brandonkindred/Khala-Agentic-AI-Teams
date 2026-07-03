"""
Shared helpers for bounded background-work ``ThreadPoolExecutor`` instances.

Multiple integrations (the health-check probe pool in ``main.py``, the GitHub webhook
dispatcher in ``github_events_handler.py``) each need a small, fixed-size worker pool
that survives module reload / test teardown by recreating itself on demand. This module
centralizes that "lazily create, recreate after shutdown" logic, and a "submit without
ever raising" helper, so both are implemented exactly once instead of once per
integration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent import futures
from typing import Any


def get_or_recreate_executor(
    current: futures.ThreadPoolExecutor | None,
    *,
    max_workers: int,
    thread_name_prefix: str,
) -> futures.ThreadPoolExecutor:
    """Return ``current`` if it is still usable, else a freshly created executor.

    Preconditions: ``max_workers`` is a positive int; ``thread_name_prefix`` identifies
        the pool in thread names/logs.
    Postconditions: returns ``current`` unchanged when it is not ``None``, has not been
        shut down (``_shutdown`` flag), AND is not broken (``_broken`` flag — set when a
        worker thread fails to spawn, e.g. at the OS thread limit / under OOM). Otherwise
        returns a brand-new ``ThreadPoolExecutor(max_workers, thread_name_prefix)``. Never
        raises. Callers own the module-level singleton slot — each integration keeps its
        own variable (sized independently) and must store the return value back into it;
        this function only decides "is it still live?", it does not manage global state.

        Why check ``_broken`` as well as ``_shutdown``: a ``BrokenThreadPool`` leaves
        ``_shutdown`` False but every ``submit()`` raises ``BrokenThreadPool`` (a
        ``RuntimeError`` subclass) — which :func:`submit_safely` would swallow forever,
        silently dropping all work. Treating ``_broken`` as "recreate" lets the pool
        self-heal after a transient thread-spawn failure instead of staying dead until
        the process restarts.

        ``_shutdown``/``_broken`` risk: both are private ``ThreadPoolExecutor``
        attributes, not part of the documented API, and could disappear in a future
        CPython release. The ``getattr(..., <falsy default>)`` reads degrade gracefully
        if either does — a missing attribute reads as "not shut down / not broken", so
        this function would at worst reuse a dead executor rather than crash. That reused
        executor's ``.submit()`` raises ``RuntimeError``, which callers going through
        :func:`submit_safely` already catch and log — so the practical failure mode is
        "briefly stops recreating the pool", not a crash. Verified by
        ``test_get_or_recreate_executor_degrades_gracefully_without_shutdown_attr`` in
        ``tests/test_bounded_executor.py``.
    """
    if current is not None and not getattr(current, "_shutdown", False) and not getattr(current, "_broken", False):
        return current
    return futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)


def submit_safely(
    executor: futures.ThreadPoolExecutor,
    fn: Callable[..., Any],
    *args: Any,
    logger: logging.Logger,
    log_prefix: str,
) -> bool:
    """Submit ``fn(*args)`` to ``executor``, never letting a shutdown race raise.

    Preconditions: ``executor`` is a (possibly already shut-down) ``ThreadPoolExecutor``.
    Postconditions: calls ``executor.submit(fn, *args)`` and returns ``True`` when the
        work was accepted. If the executor — or the interpreter itself, via
        ``concurrent.futures``' own ``atexit`` teardown — has been shut down (or is
        broken), ``submit()`` raises ``RuntimeError``; that is caught and logged rather
        than propagated, and ``False`` is returned so callers can roll back any
        bookkeeping (e.g. a dedup-table entry) that assumed the work would run. A caller
        whose contract is "never raises" (e.g. a webhook dispatcher that has already
        returned its HTTP response) keeps that guarantee even during a process-shutdown
        race, where a raw ``threading.Thread`` would not have raised. A ``done`` callback
        logs any exception that escapes ``fn`` — ``ThreadPoolExecutor`` otherwise stores
        it on the discarded ``Future`` and never surfaces it, so a failure in submitted
        work would vanish without diagnostics. Never raises.
    """
    try:
        future = executor.submit(fn, *args)
    except RuntimeError:
        logger.warning("%s: executor unavailable (process shutting down?); dropping submitted work", log_prefix)
        return False

    def _log_if_failed(fut: futures.Future[Any]) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.error("%s: submitted work raised an exception", log_prefix, exc_info=exc)

    future.add_done_callback(_log_if_failed)
    return True
