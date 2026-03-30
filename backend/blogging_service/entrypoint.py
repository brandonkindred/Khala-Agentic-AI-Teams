"""Blogging microservice entrypoint: FastAPI server + optional Temporal worker."""

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


if __name__ == "__main__":
    _start_temporal_worker()
    # workers=1 is required: the Temporal worker thread stores the client and
    # event loop in module-level globals. With workers>1 uvicorn forks, and
    # child processes lose access to the parent's globals. Using 1 worker
    # keeps the Temporal client and API handler in the same process.
    uvicorn.run(
        "blogging.api.main:app",
        host="0.0.0.0",
        port=8090,
        workers=1,
        log_level="info",
    )
