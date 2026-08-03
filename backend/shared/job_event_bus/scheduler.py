"""Asyncio-native periodic scheduling for :func:`shared.job_event_bus.bus.reap_once`.

:class:`~shared.job_event_bus.reaper.ReaperHandle` drives ``reap_once`` from a
lazily-started background OS thread — the right fit for a team that owns its
own thread budget. A team hosted in-process on a single asyncio event loop
(e.g. a module mounted directly into a FastAPI app rather than run as its own
service) instead wants the reaper to run as a plain ``asyncio.Task`` on that
same loop: no extra thread, and shutdown is ordinary task cancellation.
:func:`schedule_periodic_reap` / :func:`stop_periodic_reap` are that pair —
call the former once at startup, the latter once at shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional

from shared.job_event_bus.bus import BusState, FloatSource, IntSource, reap_once, resolve_float, resolve_int

_DEFAULT_LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300.0

__all__ = ["DEFAULT_INTERVAL_SECONDS", "schedule_periodic_reap", "stop_periodic_reap"]


def schedule_periodic_reap(
    state: BusState,
    *,
    ttl_seconds: FloatSource,
    max_jobs: IntSource,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    label: str = "event-bus",
    logger: Optional[Any] = None,
) -> asyncio.Task:
    """Start a task that calls :func:`reap_once` on *state* every *interval_seconds*.

    Preconditions:
        - Called from inside a running asyncio event loop (e.g. a FastAPI
          startup handler) — internally this is :func:`asyncio.create_task`.
        - ``interval_seconds > 0``.
        - ``ttl_seconds`` / ``max_jobs`` are non-negative, or zero-arg
          callables returning non-negative values; both are resolved live on
          each pass, matching :class:`~shared.job_event_bus.reaper.ReaperHandle`.

    Postconditions:
        - Returns the created task immediately; the first reap happens after
          one ``interval_seconds`` sleep, not on start. The caller MUST keep a
          reference to the returned task and pass it to
          :func:`stop_periodic_reap` at shutdown — an unreferenced
          ``asyncio.Task`` can be garbage-collected mid-sleep and silently
          stop reaping.
        - A failing reap pass is logged and swallowed; the loop keeps running
          on the same cadence.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    log = logger if logger is not None else _DEFAULT_LOGGER

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                reap_once(
                    state,
                    ttl_seconds=resolve_float(ttl_seconds),
                    max_jobs=resolve_int(max_jobs),
                    logger=log,
                    label=label,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s periodic reap iteration failed", label)

    return asyncio.create_task(_loop(), name=f"{label}-periodic-reap")


async def stop_periodic_reap(task: asyncio.Task) -> None:
    """Cancel *task* and await its completion (idempotent).

    Preconditions:
        - ``task`` was returned by :func:`schedule_periodic_reap`.

    Postconditions:
        - On return, ``task`` is done — call this from the app's shutdown hook
          so no periodic-reap task is left dangling past process lifetime.
          Calling this on an already-done task is a no-op.
    """
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
