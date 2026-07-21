"""Async blogging job queue and bounded daemon worker pool.

Async blogging jobs (the non-Temporal fallback) run on a BOUNDED pool of daemon worker
threads draining a shared queue, instead of an unbounded ``threading.Thread`` per
request. A pipeline thread can stay alive for a long time — it blocks on
human-in-the-loop polling until the user responds or the ~1h stale-job monitor fires —
so without a cap a burst of concurrent HITL jobs would spawn proportionally many
idle-but-alive OS threads (the scalability risk this pool exists to bound). When every
worker is busy, further submissions queue; the async endpoints still return a job_id
immediately. The workers are daemon threads so an HITL-parked job never blocks process
shutdown/deploys (preserving the previous per-job ``daemon=True`` behavior) — a
ThreadPoolExecutor would instead join its non-daemon workers at interpreter exit and
hang until the wait cleared. Temporal remains the durable path for high HITL
concurrency. Tunable via BLOGGING_ASYNC_MAX_WORKERS (clamped to >= 1, default 16).

This module owns its queue/flag/lock as fully self-consistent internal state — the same
shape as ``software_engineering_team/api/state.py``'s ``_stale_monitor_started``/
``_stale_monitor_lock``. ``api.main`` re-exports the four public names by reference so
``monkeypatch.setattr(main, "_submit_async_job", fake)`` keeps intercepting enqueue calls
from the routers; the raw queue/flag/lock stay this module's own attributes (tests that
need to poke them directly target ``agents.blogging.api.job_workers``, not ``api.main``).

The two blog-job-store collaborators this module depends on (``get_blog_job``,
``fail_blog_job``) are fallback-guarded, monkeypatched-on-``main`` names (see
``api/main.py``'s ``try/except ImportError`` block), so they're late-imported through
``main`` at call time rather than captured at module load.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional, Tuple

from job_service_client import JOB_STATUS_INTERRUPTED
from shared.env_config import env_int

logger = logging.getLogger(__name__)

_ASYNC_JOB_MAX_WORKERS = env_int("BLOGGING_ASYNC_MAX_WORKERS", 16, floor=1)
# A queue item is a ``(target, args)`` pair, or ``None`` (a stop sentinel dequeued to
# retire a worker). Spelling the payload out documents the contract for readers/type
# checkers instead of a bare ``tuple``.
JobItem = Optional[Tuple[Callable[..., Any], Tuple[Any, ...]]]
_ASYNC_JOB_QUEUE: "queue.Queue[JobItem]" = queue.Queue()
_ASYNC_JOB_WORKERS_STARTED = False
_ASYNC_JOB_WORKERS_LOCK = threading.Lock()


def _async_job_worker() -> None:
    """Daemon worker loop: run queued ``(target, args)`` jobs one at a time.

    Preconditions:
        - Queue items are ``(callable, args_tuple)`` or ``None`` (stop sentinel).
    Postconditions:
        - A job that raises is logged and skipped so one bad job never kills the worker
          (the job funcs already fail their own job-store entry); the loop runs until the
          process exits (daemon threads are reclaimed at interpreter shutdown) or a
          ``None`` sentinel is dequeued.

    Note: no ``task_done()``/``Queue.join()`` coordination — nothing ever joins the queue
    (workers are daemons reclaimed at shutdown), so a per-item ``task_done()`` would only
    imply a completion barrier that does not exist.
    """
    from agents.blogging.api import main as _main

    while True:
        item = _ASYNC_JOB_QUEUE.get()
        if item is None:
            return
        target, args = item
        # Both async targets take job_id as their first positional arg; capture it up
        # front so a crash can be correlated with the specific job in the logs.
        job_id = args[0] if args else "unknown"
        try:
            target(*args)
        except Exception as e:
            logger.exception("Async blogging job worker crashed on job %s", job_id)
            # Safety net: the job funcs mark their own job-store entry failed on error, but
            # if one crashes BEFORE reaching its own handler (e.g. a TypeError while building
            # inputs), mark it failed here so it doesn't sit in 'running' until the ~1h stale
            # monitor reaps it. Best-effort — bookkeeping must never kill the worker loop.
            if _main.fail_blog_job is not None and job_id != "unknown":
                try:
                    _main.fail_blog_job(job_id, error=str(e))
                except Exception:
                    logger.warning("Could not mark crashed job %s failed", job_id, exc_info=True)


def _ensure_async_workers() -> None:
    """Lazily start the bounded set of daemon workers on first submit (idempotent, thread-safe)."""
    global _ASYNC_JOB_WORKERS_STARTED
    if _ASYNC_JOB_WORKERS_STARTED:
        return
    with _ASYNC_JOB_WORKERS_LOCK:
        if _ASYNC_JOB_WORKERS_STARTED:
            return
        for i in range(_ASYNC_JOB_MAX_WORKERS):
            threading.Thread(
                target=_async_job_worker,
                name=f"blogging-async-job-{i}",
                daemon=True,
            ).start()
        _ASYNC_JOB_WORKERS_STARTED = True


def _submit_async_job(target: Callable[..., Any], *args: Any) -> None:
    """Enqueue a background job for the bounded daemon worker pool (returns immediately).

    Preconditions: ``target`` is callable; ``args`` are its positional arguments.
    Postconditions: the job is queued and will run on a worker as soon as one is free
        (submissions beyond ``_ASYNC_JOB_MAX_WORKERS`` in-flight jobs wait in the queue).
    """
    # Enforce the callable precondition at the boundary rather than letting a
    # non-callable slip through and only surface as a TypeError when a worker
    # dequeues it (where the bad item is logged and dropped, obscuring the caller bug).
    # An explicit raise (not assert) so the guard survives `python -O`/PYTHONOPTIMIZE,
    # which strips asserts — matching shared.concurrency.parallel_map's boundary checks.
    if not callable(target):
        raise TypeError(f"async job target must be callable, got {type(target).__name__}")
    _ensure_async_workers()
    _ASYNC_JOB_QUEUE.put((target, args))


def _job_already_terminal(job_id: str) -> bool:
    """True if a worker must not start this queued job because it is no longer runnable.

    The bounded worker pool can leave a job queued (status ``pending``) for a while when
    all workers are busy. In that window the job may transition out of ``pending`` before
    a worker dequeues it:

    - the stale-job monitor may mark a long-queued job ``failed``, or a user may
      ``cancel`` it — starting it anyway would "resurrect" a terminal job (flip it back to
      ``running``);
    - the shutdown hook marks active jobs ``interrupted`` for a later resume — a worker
      dequeuing one before process exit and flipping it to ``running`` would defeat that
      handoff and leave it un-resumable. (Resume itself first sets the job ``running``, so
      a legitimately-resumed job is never ``interrupted`` at dequeue time.)

    ``get_blog_job`` returns None only for a genuinely-absent job — transient/HTTP errors
    raise rather than returning None — so a missing (deleted) job is skipped too.

    Fails OPEN: if the store is unavailable or the preflight read itself raises (a
    transient job-service outage at dequeue time), this returns False so the job still
    runs (it starts/heartbeats/fails on its own). Abandoning a valid queued job on a
    transient read blip would leave it pending until the stale monitor reaps it.

    Preconditions: ``job_id`` is non-empty.
    Postconditions: pure read; returns a bool (False when the job store is unavailable or
        the read fails). Only a definitive failed/cancelled/interrupted/missing status
        returns True.
    """
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None:
        return False
    try:
        job = _main.get_blog_job(job_id)
    except Exception:
        logger.warning(
            "Preflight status read failed for job %s; proceeding to run it", job_id, exc_info=True
        )
        return False
    return job is None or job.get("status") in (
        _main.JOB_STATUS_FAILED,
        _main.JOB_STATUS_CANCELLED,
        JOB_STATUS_INTERRUPTED,
    )
