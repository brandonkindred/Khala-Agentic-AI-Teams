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
    Postconditions: returns ``current`` unchanged when it is not ``None`` and has not
        been shut down (checked via the ``_shutdown`` flag, mirroring the same
        undocumented-but-stable check every lazy executor accessor in this codebase
        already relies on). Otherwise returns a brand-new
        ``ThreadPoolExecutor(max_workers, thread_name_prefix)``. Never raises. Callers
        own the module-level singleton slot — each integration keeps its own variable
        (sized independently) and must store the return value back into it; this
        function only decides "is it still live?", it does not manage global state.

        ``_shutdown`` risk: it is a private ``ThreadPoolExecutor`` attribute, not part of
        the documented API, and could disappear in a future CPython release.
        ``getattr(current, "_shutdown", False)`` degrades gracefully if it does — a
        missing attribute reads as "not shut down", so this function would incorrectly
        reuse a truly-shut-down executor rather than crash. That reused executor's
        ``.submit()`` would then raise ``RuntimeError``, which callers going through
        :func:`submit_safely` already catch and log — so the practical failure mode is
        "briefly stops recreating the pool after a shutdown", not a crash. Verified by
        ``test_get_or_recreate_executor_degrades_gracefully_without_shutdown_attr`` in
        ``tests/test_bounded_executor.py``, which exercises exactly this "attribute
        missing" case against a double lacking ``_shutdown``.
    """
    if current is not None and not getattr(current, "_shutdown", False):
        return current
    return futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)


def submit_safely(
    executor: futures.ThreadPoolExecutor,
    fn: Callable[..., Any],
    *args: Any,
    logger: logging.Logger,
    log_prefix: str,
) -> None:
    """Submit ``fn(*args)`` to ``executor``, never letting a shutdown race raise.

    Preconditions: ``executor`` is a (possibly already shut-down) ``ThreadPoolExecutor``.
    Postconditions: calls ``executor.submit(fn, *args)``. If the executor — or the
        interpreter itself, via ``concurrent.futures``' own ``atexit`` teardown — has
        been shut down, ``submit()`` raises ``RuntimeError``; that is caught and logged
        rather than propagated, so a caller whose contract is "never raises" (e.g. a
        webhook dispatcher that has already returned its HTTP response) keeps that
        guarantee even during a process-shutdown race, where a raw ``threading.Thread``
        would not have raised. Never raises.
    """
    try:
        executor.submit(fn, *args)
    except RuntimeError:
        logger.warning("%s: executor unavailable (process shutting down?); dropping submitted work", log_prefix)
