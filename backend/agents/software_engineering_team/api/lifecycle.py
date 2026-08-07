"""ASGI startup/shutdown hooks for the SE team app.

Passed to ``create_team_app`` by ``main``. Startup fails fast when Temporal is
disabled, unreachable, either Temporal worker fails to start, or the worker
thread never becomes ready; telemetry and CodeEngineProvider install remain
log-and-continue so a non-Temporal failure never leaks the Postgres pool.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def _assert_temporal_ready() -> None:
    """Require a live Temporal connection before SE starts serving.

    Preconditions:
        - None (reads Temporal env via ``is_temporal_enabled`` / connect helpers).

    Postconditions:
        - Raises ``RuntimeError`` when Temporal is disabled (no ``TEMPORAL_ADDRESS``).
        - Awaits ``connect_temporal_client`` when enabled; propagates connect errors.
        - Raises ``RuntimeError`` when the probe returns no client.
        - Returns only after a successful connect probe that yields a client.
    """
    from shared.temporal.client import connect_temporal_client, is_temporal_enabled

    if not is_temporal_enabled():
        raise RuntimeError(
            "SE requires TEMPORAL_ADDRESS; refusing to start without Temporal"
        )
    client = await connect_temporal_client()
    if client is None:
        raise RuntimeError(
            "SE Temporal connect probe returned no client; refusing to start"
        )


async def _wait_for_shared_temporal_client() -> None:
    """Block until a team worker has populated the shared Temporal client + loop.

    ``start_team_worker`` returns True as soon as the daemon thread is spawned;
    connect + ``Worker.run`` happen asynchronously. Polling via
    ``await_client`` turns that race into a hard startup failure when the
    worker never becomes ready.

    Preconditions:
        - A Temporal worker thread has already been started for this process.

    Postconditions:
        - Returns once ``await_client`` observes a live shared client + loop.
        - Propagates ``RuntimeError`` from ``await_client`` on timeout.
    """
    from shared.temporal import await_client

    # ``await_client`` sleeps while polling — keep it off the ASGI event loop.
    await asyncio.to_thread(await_client)


async def _wait_for_team_worker_thread(team: str) -> None:
    """Require ``team``'s worker thread to stay alive through connect settle.

    Shared ``await_client`` only proves *some* worker populated the shared
    client slots (typically SE). Coding_team runs on its own thread/queue; if
    its connect fails, the daemon exits while SE's client may still look ready.

    Preconditions:
        - ``team`` was passed to ``start_team_worker`` (thread registered).

    Postconditions:
        - Returns once the team's thread is alive and remains alive after a
          short settle, or raises ``RuntimeError`` if it exits / never appears
          within ``CLIENT_READY_TIMEOUT_S``.
    """
    from shared.temporal import worker as temporal_worker
    from shared.temporal.runner import CLIENT_READY_POLL_S, CLIENT_READY_TIMEOUT_S

    deadline = time.monotonic() + CLIENT_READY_TIMEOUT_S
    seen_alive = False
    while time.monotonic() < deadline:
        thread = temporal_worker._worker_threads.get(team)
        if thread is not None and thread.is_alive():
            seen_alive = True
            # Brief settle so a connect that fails immediately can exit the
            # thread before we declare success.
            await asyncio.sleep(CLIENT_READY_POLL_S)
            thread = temporal_worker._worker_threads.get(team)
            if thread is not None and thread.is_alive():
                return
            break
        await asyncio.sleep(CLIENT_READY_POLL_S)
    status = "exited after start" if seen_alive else "never became ready"
    raise RuntimeError(
        f"{team} Temporal worker thread {status}; refusing to serve without a worker"
    )


async def _se_startup() -> None:
    """Register SE telemetry observers and start SE's + coding_team's Temporal workers.

    Runs after the factory has registered the SE Postgres schema. Fails fast
    when Temporal is disabled, unreachable, either Temporal worker fails to
    start, or a worker thread never becomes ready. Telemetry and
    CodeEngineProvider install are log-and-continue so a single non-Temporal
    failure never leaks the Postgres pool the factory may have opened.

    Also installs the SE-backed ``CodeEngineProvider`` and starts coding_team's
    own Temporal worker (on its own task queue) — this is the in-process
    replacement for what the now-retired standalone ``coding-team-service``
    container and its ``coding_team_service`` composition root used to do.
    """
    await _assert_temporal_ready()
    try:
        from software_engineering_team.shared.cost_tracker import register_cost_observer
        from software_engineering_team.shared.trace_flusher import register_trace_flusher

        register_cost_observer()
        register_trace_flusher()
    except Exception as e:
        logger.warning("Could not register SE telemetry observers: %s", e)
    from software_engineering_team.temporal.worker import start_se_temporal_worker_thread

    if not start_se_temporal_worker_thread():
        raise RuntimeError(
            "SE Temporal worker failed to start; refusing to serve without a worker"
        )
    await _wait_for_shared_temporal_client()
    await _wait_for_team_worker_thread("software_engineering")
    try:
        from software_engineering_team.coding_engine_provider import SECodeEngineProvider
        from software_engineering_team.engine_provider import set_engine_provider

        set_engine_provider(SECodeEngineProvider())
    except Exception as e:
        logger.warning("Could not install SE-backed CodeEngineProvider for coding_team: %s", e)
    from software_engineering_team.temporal.coding_team_worker import (
        start_coding_team_temporal_worker_thread,
    )

    if not start_coding_team_temporal_worker_thread():
        raise RuntimeError(
            "coding_team Temporal worker failed to start; refusing to serve without a worker"
        )
    await _wait_for_team_worker_thread("coding_team")


def _se_shutdown() -> None:  # pragma: no cover - integration-only ASGI shutdown hook
    """Flush buffered traces and mark active SE jobs as failed for resume.

    Runs before the factory closes the Postgres pool (see
    ``shared/app/factory.py``), so the trace flusher's final drain can still use
    the pool. Log-and-continue — a single failure never aborts shutdown or leaks
    the pool the factory closes next.
    """
    try:
        from software_engineering_team.shared.trace_flusher import shutdown as flush_shutdown

        flush_shutdown()
    except Exception as e:
        logger.warning("Could not flush SE traces on shutdown: %s", e)
    try:
        from software_engineering_team.shared.job_store import mark_all_running_jobs_failed

        mark_all_running_jobs_failed("Server shutdown — job can be resumed")
        logger.info("Marked all active SE jobs as failed (server shutdown)")
    except Exception as e:
        logger.warning("Could not mark SE jobs as failed on shutdown: %s", e)
