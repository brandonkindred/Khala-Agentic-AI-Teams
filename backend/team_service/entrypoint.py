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
    """Mark active jobs as failed on service shutdown."""
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team=TEAM_NAME)
        client.mark_all_active_jobs_failed(f"{TEAM_NAME} service shutting down")
    except Exception:
        logger.warning("Shutdown hook failed for %s", TEAM_NAME, exc_info=True)


def _resolve_app() -> str:
    """Return a uvicorn import string for the ASGI app.

    If TEAM_APP_ATTR points to an APIRouter (not a FastAPI app), create a
    wrapper module with a FastAPI app that includes the router so uvicorn can
    serve it.
    """
    if TEAM_APP_ATTR == "router":
        # Build a wrapper FastAPI app at runtime and register it as a module attribute.
        import types

        from fastapi import FastAPI

        mod = importlib.import_module(TEAM_MODULE)
        router = getattr(mod, TEAM_APP_ATTR)
        wrapper_app = FastAPI(title=f"{TEAM_NAME} API")
        wrapper_app.include_router(router)
        # Attach to a synthetic module so uvicorn's import string works.
        wrapper = types.ModuleType("_team_wrapper")
        wrapper.app = wrapper_app
        import sys

        sys.modules["_team_wrapper"] = wrapper
        return "_team_wrapper:app"
    return f"{TEAM_MODULE}:{TEAM_APP_ATTR}"


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
