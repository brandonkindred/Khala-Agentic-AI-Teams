"""Blogging microservice entrypoint: FastAPI server + optional Temporal worker."""

import atexit
import logging
import os

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("blogging_service")


def _start_temporal_worker() -> None:
    """Start the blogging Temporal worker thread when TEMPORAL_ADDRESS is configured."""
    if not os.environ.get("TEMPORAL_ADDRESS", "").strip():
        return
    try:
        from blogging.temporal.worker import start_blogging_temporal_worker_thread

        if start_blogging_temporal_worker_thread():
            logger.info("Blogging Temporal worker started")
    except Exception:
        logger.warning("Could not start Temporal worker", exc_info=True)


def _shutdown_hook() -> None:
    """Mark active blogging jobs as failed on service shutdown."""
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team="blogging_team")
        client.mark_all_active_jobs_failed("Blogging service shutting down")
    except Exception:
        logger.warning("Shutdown hook failed", exc_info=True)


if __name__ == "__main__":
    _start_temporal_worker()
    atexit.register(_shutdown_hook)
    uvicorn.run(
        "blogging.api.main:app",
        host="0.0.0.0",
        port=8090,
        workers=2,
        log_level="info",
    )
