"""Blogging microservice entrypoint: FastAPI server + optional Temporal worker."""

import logging
import os

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("blogging_service")


def _register_usage_flusher() -> None:
    """Register the process-local LLM usage observer before Temporal starts.

    This entrypoint starts the Temporal worker before uvicorn runs the FastAPI
    lifespan, so ``create_team_app``'s registration is too late for activities
    that run in that window. ``atexit`` covers a crash before lifespan shutdown
    and stops in-process Temporal workers before draining usage.
    Idempotent with the team-app lifespan hook.

    Postconditions:
        - ``register_usage_flusher`` has been invoked and ``shutdown`` is
          registered with ``atexit``, or a failure was logged. Never raises.
    """
    try:
        import atexit

        from llm_service.usage_flusher import register_usage_flusher
        from llm_service.usage_flusher import shutdown as usage_flush_shutdown

        def _teardown() -> None:
            try:
                from shared.temporal.worker import stop_all_team_workers

                stop_all_team_workers()
            except Exception:
                logger.warning("in-process Temporal worker shutdown failed", exc_info=True)
            usage_flush_shutdown()

        register_usage_flusher()
        atexit.register(_teardown)
    except Exception:
        logger.warning("llm usage flusher registration failed", exc_info=True)


def _start_temporal_worker() -> None:
    """Start the blogging Temporal worker thread when TEMPORAL_ADDRESS is configured."""
    if not os.environ.get("TEMPORAL_ADDRESS", "").strip():
        return
    try:
        from agents.blogging.temporal.worker import start_blogging_temporal_worker_thread

        if start_blogging_temporal_worker_thread():
            logger.info("Blogging Temporal worker started")
    except Exception:
        logger.warning("Could not start Temporal worker", exc_info=True)


if __name__ == "__main__":
    _register_usage_flusher()
    _start_temporal_worker()

    # Import the app object so we can instrument it in-process before uvicorn
    # starts. Safe because workers=1 (see note below).
    from agents.blogging.api.main import app as _blogging_app

    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator(
            should_group_status_codes=True,
            should_ignore_untemplated=True,
            excluded_handlers=["/metrics", "/health"],
        ).instrument(_blogging_app).expose(_blogging_app, endpoint="/metrics", include_in_schema=False)
    except Exception:
        logger.warning("prometheus instrumentator unavailable", exc_info=True)

    # workers=1 is required: the Temporal worker thread stores the client and
    # event loop in module-level globals. With workers>1 uvicorn forks, and
    # child processes lose access to the parent's globals. Using 1 worker
    # keeps the Temporal client and API handler in the same process.
    uvicorn.run(
        _blogging_app,
        host="0.0.0.0",
        port=8090,
        workers=1,
        log_level="info",
    )
