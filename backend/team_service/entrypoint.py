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
import re

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("team_service")

# Uvicorn access log format: '%(client_addr)s - "%(request_line)s" %(status_code)s'
# Anchoring on the quoted request line avoids false matches against IPv6 client
# addresses (e.g. 2001:...) or "200" appearing inside a request path.
_ACCESS_LINE_RE = re.compile(r'"GET (?P<path>[^ "]+) HTTP/[^"]+"\s+(?P<status>\d{3})')
_QUIET_PATHS = frozenset({"/health", "/metrics"})


class _HealthCheckFilter(logging.Filter):
    """Suppress successful health-check and metrics-scrape access log lines.

    Non-2xx responses still pass through so failures are visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno <= logging.DEBUG:
            return True  # Always show in debug mode
        match = _ACCESS_LINE_RE.search(record.getMessage())
        if not match:
            return True
        if not match["status"].startswith("2"):
            return True
        path = match["path"].split("?", 1)[0]
        return path not in _QUIET_PATHS


# Apply to uvicorn's access logger so health probes and Prometheus scrapes
# don't fill the logs.
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
    """Mark active jobs as interrupted on service shutdown.

    When the whole stack is being torn down, job-service often disappears
    first and this call races the shutdown — treat connection errors as a
    single-line WARNING instead of a full traceback, since there's nothing
    the team service can do about it. Other exceptions still get the full
    stack trace so real bugs aren't hidden.
    """
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team=TEAM_NAME)
        client.mark_all_active_jobs_interrupted(f"{TEAM_NAME} service shutting down")
    except Exception as exc:
        # httpx connection errors vs. everything else: quiet the common
        # "job-service is already gone" case during stack shutdown.
        is_conn_error = False
        try:
            import httpx

            is_conn_error = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        except Exception:
            pass
        if is_conn_error:
            logger.warning(
                "Shutdown hook for %s: job-service unreachable (%s); skipping",
                TEAM_NAME,
                exc,
            )
        else:
            logger.warning("Shutdown hook failed for %s", TEAM_NAME, exc_info=True)


def build_wrapper_body(team_name: str, team_module: str, app_attr: str) -> str:
    """Assemble the per-worker ``_team_wrapper.py`` source for a team service.

    Pure (no side effects): returns the wrapper source as a string so the
    generated code can be compiled/asserted in tests. When imported by each
    uvicorn worker the module re-initialises OpenTelemetry, arms fault
    diagnostics + the memory watchdog, imports/builds the FastAPI ``app``,
    instruments it, and exposes ``/metrics``.

    The OTel *import* and the ``init_otel()`` *call* sit in separate try blocks
    on purpose: a transient init failure must not discard the successfully
    imported ``instrument_fastapi_app`` and leave the worker serving untraced
    requests.

    Preconditions:
        - ``team_name`` and ``team_module`` are non-empty. ``app_attr`` selects
          the attribute imported from the team module — ``"router"`` wraps a
          router in a fresh FastAPI app, anything else re-exports the team's own
          FastAPI ``app``.
        - ``team_module``/``app_attr`` are valid dotted Python identifiers (they
          land in a ``from X import Y`` statement that ``repr()`` can't guard).
    Postconditions:
        - Returns valid Python source that defines a module-level ``app`` and
          always ``compile()``s.
    """
    if not (team_name and team_module):
        raise ValueError("team_name and team_module are required")
    # team_module/app_attr go into a `from X import Y` statement where repr()
    # can't apply, so validate them as safe identifiers to foreclose code
    # injection via a hostile TEAM_MODULE / TEAM_APP_ATTR. team_name only ever
    # appears inside string literals and is embedded with repr() (``!r``) below.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", team_module):
        raise ValueError(f"unsafe team_module for wrapper generation: {team_module!r}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", app_attr):
        raise ValueError(f"unsafe app_attr for wrapper generation: {app_attr!r}")

    # A single logger up top (DRY) instead of `import logging` in each except.
    # Every worker re-runs this wrapper on import, so re-initialise OTel and
    # re-instrument the app each time.
    body = (
        "import logging\n"
        "_log = logging.getLogger('team_service')\n"
        "try:\n"
        "    from shared_observability import init_otel, instrument_fastapi_app\n"
        "except Exception:\n"
        "    _log.warning('shared_observability import failed', exc_info=True)\n"
        "    def init_otel(*_a, **_k):\n"
        "        return None\n"
        "    def instrument_fastapi_app(*_a, **_k):\n"
        "        return None\n"
        "try:\n"
        f"    init_otel(service_name={team_name!r}, team_key={team_name!r})\n"
        "except Exception:\n"
        "    _log.warning('shared_observability init_otel failed', exc_info=True)\n"
    )

    # Each worker is its own process. Under fork (uvicorn's default on Linux) the
    # supervisor's faulthandler + excepthooks are inherited, so this re-arm is a
    # cheap no-op; under a spawn/forkserver start-method the worker is a fresh
    # interpreter and this is what arms them. Either way the memory watchdog must
    # be (re)started here — threads do not survive fork — so an impending OOM
    # kill is logged before the worker vanishes and a native fault dumps a stack.
    body += (
        "try:\n"
        "    from shared_observability import install_fault_diagnostics, start_memory_watchdog\n"
        "    install_fault_diagnostics()\n"
        f"    start_memory_watchdog({team_name!r})\n"
        "except Exception:\n"
        "    _log.warning('fault diagnostics / memory watchdog unavailable', exc_info=True)\n"
    )

    if app_attr == "router":
        body += (
            "from fastapi import FastAPI\n"
            f"from {team_module} import {app_attr} as _router\n"
            f"app = FastAPI(title={(team_name + ' API')!r})\n"
            "app.include_router(_router)\n"
        )
    else:
        body += f"from {team_module} import {app_attr} as app\n"

    body += (
        "try:\n"
        f"    instrument_fastapi_app(app, team_key={team_name!r})\n"
        "except Exception:\n"
        "    _log.warning('instrument_fastapi_app failed', exc_info=True)\n"
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
        "    _log.warning('prometheus instrumentator unavailable', exc_info=True)\n"
    )
    return body


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
    body = build_wrapper_body(TEAM_NAME, TEAM_MODULE, TEAM_APP_ATTR)
    wrapper_path.write_text(body, encoding="utf-8")
    return "_team_wrapper:app"


def _startup_recovery() -> None:
    """Mark any jobs still stuck as 'running' or 'pending' as interrupted.

    On startup, no jobs from a previous process can genuinely be running —
    they are leftovers from a crash or kill where the shutdown hook didn't fire.
    """
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team=TEAM_NAME)
        marked = client.mark_all_active_jobs_interrupted(
            f"{TEAM_NAME} service restarted — marking orphaned jobs"
        )
        if marked:
            logger.info(
                "Startup recovery: marked %d orphaned job(s) as interrupted for %s",
                len(marked) if isinstance(marked, list) else 1,
                TEAM_NAME,
            )
    except Exception:
        logger.warning("Startup recovery failed for %s", TEAM_NAME, exc_info=True)


if __name__ == "__main__":
    logger.info("Starting %s on port %d (module=%s)", TEAM_NAME, TEAM_PORT, TEAM_MODULE)
    # Arm fault diagnostics in the supervisor process first; forked workers
    # inherit faulthandler, and the generated wrapper re-arms it per worker.
    try:
        from shared_observability import install_fault_diagnostics

        install_fault_diagnostics(logger)
    except Exception:
        logger.warning("fault diagnostics unavailable", exc_info=True)
    _startup_recovery()
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
