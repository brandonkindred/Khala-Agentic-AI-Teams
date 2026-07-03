"""ASGI startup/shutdown hooks for the SE team app.

Passed to ``create_team_app`` by ``main``; each hook is log-and-continue so a
single failure never aborts app startup or leaks the Postgres pool.
"""

import logging

logger = logging.getLogger(__name__)


def _se_startup() -> None:  # pragma: no cover - integration-only ASGI startup hook
    """Register SE telemetry observers and start the Temporal worker if enabled.

    Runs after the factory has registered the SE Postgres schema. Each step is
    log-and-continue so a single failure never aborts app startup (and never
    leaks the Postgres pool the factory may have opened).
    """
    try:
        from software_engineering_team.shared.cost_tracker import register_cost_observer
        from software_engineering_team.shared.trace_store import register_trace_observer

        register_cost_observer()
        register_trace_observer()
    except Exception as e:
        logger.warning("Could not register SE telemetry observers: %s", e)
    try:
        from software_engineering_team.temporal.worker import start_se_temporal_worker_thread

        start_se_temporal_worker_thread()
    except Exception as e:
        logger.warning("Could not start SE Temporal worker: %s", e)


def _se_shutdown() -> None:  # pragma: no cover - integration-only ASGI shutdown hook
    """Mark active SE jobs as failed so they can be resumed after a restart.

    Runs before the factory closes the Postgres pool. Log-and-continue.
    """
    try:
        from software_engineering_team.shared.job_store import mark_all_running_jobs_failed

        mark_all_running_jobs_failed("Server shutdown — job can be resumed")
        logger.info("Marked all active SE jobs as failed (server shutdown)")
    except Exception as e:
        logger.warning("Could not mark SE jobs as failed on shutdown: %s", e)
