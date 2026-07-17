"""ASGI shutdown hook for the branding team app.

Passed to ``create_team_app`` by ``main``. The mutable globals it acts on
(``_stale_monitor_stop``, ``_run_executor``, ``_job_manager``) are owned by
``main``; this hook dereferences them through ``main`` at call time (a lazy
import inside the function body, executed at shutdown — long after ``main`` has
finished importing — so there is no import cycle and test monkeypatches on
``main`` are still observed).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _branding_service_shutdown() -> None:
    """Runs while Uvicorn still has the event loop, before the shared Postgres pool closes."""
    from branding_team.api import main as _main

    if _main._stale_monitor_stop is not None:
        _main._stale_monitor_stop.set()

    # Stop accepting new runs and cancel any still queued so worker threads
    # don't outlive the app. Don't block teardown on an in-flight pipeline
    # (a full run can take minutes); those threads finish on their own.
    _main._run_executor.shutdown(wait=False, cancel_futures=True)

    if _main._job_manager is not None:
        logger.info("Branding service shutdown: notifying job-service…")
        try:
            _main._job_manager.mark_all_active_jobs_interrupted(
                "Branding service shutting down",
                http_timeout=5.0,
                http_max_retries=0,
            )
        except Exception as exc:
            logger.info("Job-service shutdown notification skipped: %s", exc)
