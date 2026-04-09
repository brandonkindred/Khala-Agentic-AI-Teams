"""Generic team microservice entrypoint.

Reads configuration from environment variables:
  TEAM_MODULE                    — dotted import path, e.g. "branding_team.api.main"
  TEAM_APP_ATTR                  — attribute name on the module (default "app")
  TEAM_PORT                      — listen port (default 8090)
  TEAM_NAME                      — job-service team name for shutdown hooks
  TEAM_TEMPORAL_WORKER_MODULE    — optional Temporal worker module path
  TEAM_TEMPORAL_WORKER_FUNC      — optional Temporal worker start function name
"""

import atexit
import importlib
import logging
import os

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("team_service")


class _HealthCheckFilter(logging.Filter):
    """Suppress health-check access log lines unless the logger is at DEBUG level."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno <= logging.DEBUG:
            return True  # Always show in debug mode
        msg = record.getMessage()
        return not ("GET /health" in msg and "200" in msg)


# Apply to uvicorn's access logger so health probes don't fill the logs.
logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

TEAM_MODULE = os.environ["TEAM_MODULE"]
TEAM_APP_ATTR = os.environ.get("TEAM_APP_ATTR", "app")
TEAM_PORT = int(os.environ.get("TEAM_PORT", "8090"))
TEAM_NAME = os.environ.get("TEAM_NAME", "team")
TEMPORAL_MODULE = os.environ.get("TEAM_TEMPORAL_WORKER_MODULE", "").strip()
TEMPORAL_FUNC = os.environ.get("TEAM_TEMPORAL_WORKER_FUNC", "").strip()


def _start_temporal_worker() -> None:
    """Start the team's Temporal worker thread when TEMPORAL_ADDRESS is configured."""
    if not TEMPORAL_MODULE or not TEMPORAL_FUNC:
        return
    if not os.environ.get("TEMPORAL_ADDRESS", "").strip():
        return
    try:
        mod = importlib.import_module(TEMPORAL_MODULE)
        start_fn = getattr(mod, TEMPORAL_FUNC)
        if start_fn():
            logger.info("Temporal worker started for %s", TEAM_NAME)
    except Exception:
        logger.warning("Could not start Temporal worker for %s", TEAM_NAME, exc_info=True)


def _shutdown_hook() -> None:
    """Mark active jobs as interrupted on service shutdown."""
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team=TEAM_NAME)
        client.mark_all_active_jobs_interrupted(f"{TEAM_NAME} service shutting down")
    except Exception:
        logger.warning("Shutdown hook failed for %s", TEAM_NAME, exc_info=True)


def _resolve_app() -> str:
    """Return a uvicorn import string for the instrumented ASGI app.

    Always writes /app/_team_wrapper.py so every team gets:
      * OpenTelemetry initialized before the team module is imported, so any
        tracer/meter calls made during import land on the real providers.
      * A FastAPI app (wrapping a router if TEAM_APP_ATTR == "router", else
        re-exporting the team's own FastAPI app).
      * FastAPI OpenTelemetry instrumentation (trace every request/response).
      * prometheus-fastapi-instrumentator installed and /metrics exposed.

    The wrapper is re-imported by each uvicorn worker on fork, so per-worker
    instrumentation state is fine with workers>1.
    """
    import pathlib

    # Initialize OpenTelemetry *before* importing the team module so any
    # tracer/meter references captured at import time see the real providers.
    try:
        from shared_observability import init_otel

        init_otel(service_name=TEAM_NAME, team_key=TEAM_NAME)
    except Exception:
        logger.warning("shared_observability init_otel unavailable", exc_info=True)

    # Validate the team module can be imported (fail fast with a clear error).
    try:
        importlib.import_module(TEAM_MODULE)
    except Exception:
        logger.exception("FATAL: cannot import team module %s", TEAM_MODULE)
        raise

    wrapper_path = pathlib.Path("/app/_team_wrapper.py")

    # Every worker re-runs this wrapper on fork, so re-initialise OTel and
    # re-instrument the app on each import.
    body = (
        "try:\n"
        "    from shared_observability import init_otel, instrument_fastapi_app\n"
        f"    init_otel(service_name='{TEAM_NAME}', team_key='{TEAM_NAME}')\n"
        "except Exception:\n"
        "    import logging\n"
        "    logging.getLogger('team_service').warning(\n"
        "        'shared_observability init_otel failed', exc_info=True\n"
        "    )\n"
        "    def instrument_fastapi_app(*_a, **_k):\n"
        "        return None\n"
    )

    if TEAM_APP_ATTR == "router":
        body += (
            "from fastapi import FastAPI\n"
            f"from {TEAM_MODULE} import {TEAM_APP_ATTR} as _router\n"
            f"app = FastAPI(title='{TEAM_NAME} API')\n"
            "app.include_router(_router)\n"
        )
    else:
        body += f"from {TEAM_MODULE} import {TEAM_APP_ATTR} as app\n"

    body += (
        "try:\n"
        f"    instrument_fastapi_app(app, team_key='{TEAM_NAME}')\n"
        "except Exception:\n"
        "    import logging\n"
        "    logging.getLogger('team_service').warning(\n"
        "        'instrument_fastapi_app failed', exc_info=True\n"
        "    )\n"
    )

    body += (
        "try:\n"
        "    from prometheus_fastapi_instrumentator import Instrumentator\n"
        "    Instrumentator(\n"
        "        should_group_status_codes=True,\n"
        "        should_ignore_untemplated=True,\n"
        "        excluded_handlers=['/metrics', '/health'],\n"
        "    ).instrument(app).expose(\n"
        "        app, endpoint='/metrics', include_in_schema=False\n"
        "    )\n"
        "except Exception:\n"
        "    import logging\n"
        "    logging.getLogger('team_service').warning(\n"
        "        'prometheus instrumentator unavailable', exc_info=True\n"
        "    )\n"
    )

    wrapper_path.write_text(body, encoding="utf-8")
    return "_team_wrapper:app"


if __name__ == "__main__":
    logger.info("Starting %s on port %d (module=%s)", TEAM_NAME, TEAM_PORT, TEAM_MODULE)
    _start_temporal_worker()
    atexit.register(_shutdown_hook)
    app_import = _resolve_app()
    uvicorn.run(
        app_import,
        host="0.0.0.0",
        port=TEAM_PORT,
        workers=2,
        log_level="info",
    )
