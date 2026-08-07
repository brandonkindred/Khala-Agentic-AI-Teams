"""ASGI startup/shutdown hooks for the SE team app.

Passed to ``create_team_app`` by ``main``. Startup fails fast when Temporal is
disabled or unreachable; other individual steps remain log-and-continue so a
non-Temporal failure never leaks the Postgres pool.
"""

import logging

logger = logging.getLogger(__name__)


async def _assert_temporal_ready() -> None:
    """Require a live Temporal connection before SE starts serving.

    Preconditions:
        - None (reads Temporal env via ``is_temporal_enabled`` / connect helpers).

    Postconditions:
        - Raises ``RuntimeError`` when Temporal is disabled (no ``TEMPORAL_ADDRESS``).
        - Awaits ``connect_temporal_client`` when enabled; propagates connect errors.
        - Returns only after a successful connect probe.
    """
    from shared.temporal.client import connect_temporal_client, is_temporal_enabled

    if not is_temporal_enabled():
        raise RuntimeError(
            "SE requires TEMPORAL_ADDRESS; refusing to start without Temporal"
        )
    await connect_temporal_client()


async def _se_startup() -> None:  # pragma: no cover - integration-only ASGI startup hook
    """Register SE telemetry observers and start SE's + coding_team's Temporal workers.

    Runs after the factory has registered the SE Postgres schema. Fails fast
    when Temporal is disabled or unreachable (see ``_assert_temporal_ready``).
    Subsequent steps are log-and-continue so a single non-Temporal failure never
    leaks the Postgres pool the factory may have opened.

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
    try:
        from software_engineering_team.temporal.worker import start_se_temporal_worker_thread

        start_se_temporal_worker_thread()
    except Exception as e:
        logger.warning("Could not start SE Temporal worker: %s", e)
    try:
        from software_engineering_team.coding_engine_provider import SECodeEngineProvider
        from software_engineering_team.engine_provider import set_engine_provider

        set_engine_provider(SECodeEngineProvider())
    except Exception as e:
        logger.warning("Could not install SE-backed CodeEngineProvider for coding_team: %s", e)
    try:
        from software_engineering_team.temporal.coding_team_worker import (
            start_coding_team_temporal_worker_thread,
        )

        start_coding_team_temporal_worker_thread()
    except Exception as e:
        logger.warning("Could not start coding_team Temporal worker: %s", e)


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
