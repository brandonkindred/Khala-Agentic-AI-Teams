"""Generic team microservice entrypoint.

Reads configuration from environment variables:
  TEAM_MODULE                    — dotted import path, e.g. "branding_team.api.main"
  TEAM_APP_ATTR                  — attribute name on the module (default "app")
  TEAM_PORT                      — listen port (default 8090)
  TEAM_NAME                      — job-service team name for shutdown hooks
  TEAM_TEMPORAL_WORKER_MODULE    — optional Temporal worker module path
  TEAM_TEMPORAL_WORKER_FUNC      — optional Temporal worker start function name
  TEAM_WORKERS                   — uvicorn worker processes (default 2; set 1 to
                                   shrink the per-team memory footprint)
  TEAM_SKIP_ACTIVE_JOB_INTERRUPT — when truthy (1/true/yes), skip startup/shutdown
                                   mark_all_active_jobs_interrupted. Use for teams
                                   whose in-flight work is Temporal-owned and must
                                   survive API process restarts.
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
_SKIP_ACTIVE_JOB_INTERRUPT_VALUES = frozenset({"1", "true", "yes"})
TEAM_SKIP_ACTIVE_JOB_INTERRUPT = (
    os.environ.get("TEAM_SKIP_ACTIVE_JOB_INTERRUPT", "").strip().lower() in _SKIP_ACTIVE_JOB_INTERRUPT_VALUES
)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    """Parse a positive int from env var *name*, defensively, then clamp.

    Postconditions:
        - Returns an int clamped to ``[minimum, maximum]`` (``maximum`` unbounded
          when None) on *every* path — including the *default* fallback used when
          the var is unset/blank/non-numeric — so the result always honors the
          bounds even if a caller passes an out-of-range default. Never raises on
          any input value (it assumes the module ``logger`` is initialized, which
          it always is by import time). Swapped bounds (``minimum > maximum``) are
          normalized rather than producing an undefined clamp.
    """
    # Defensive: a caller passing minimum > maximum would otherwise clamp to an
    # ill-defined value; normalize so the [minimum, maximum] window is coherent.
    if maximum is not None and minimum > maximum:
        minimum, maximum = maximum, minimum
    raw = os.environ.get(name)
    value = default
    if raw is not None and raw.strip():
        try:
            parsed = float(raw)
            value = int(parsed)
            if parsed != value:
                # e.g. "2.5" → 2: a fractional worker/count is almost certainly a
                # mistake, so flag the truncation rather than swallow it silently.
                logger.warning("Fractional value for %s: %r truncated to %d", name, raw, value)
        except (TypeError, ValueError, OverflowError):
            # Surface the misconfiguration: a set-but-unparseable value silently
            # running on defaults is exactly the surprise this warning prevents.
            logger.warning("Invalid value for %s: %r; using default %d", name, raw, default)
            value = default
    if value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


# Each uvicorn worker is a full Python interpreter loading the whole app, so on a
# memory-constrained host fewer workers means a materially smaller footprint.
# Default 2 preserves prior behavior; the docker stack sets TEAM_WORKERS=1. Capped
# at 16 so a misconfigured value can't fork-bomb the host into resource exhaustion.
_MAX_TEAM_WORKERS = 16
TEAM_WORKERS = _env_int("TEAM_WORKERS", 2, minimum=1, maximum=_MAX_TEAM_WORKERS)


def _shutdown_hook() -> None:
    """Mark active jobs as interrupted on service shutdown.

    When the whole stack is being torn down, job-service often disappears
    first and this call races the shutdown — treat connection errors as a
    single-line WARNING instead of a full traceback, since there's nothing
    the team service can do about it. Other exceptions still get the full
    stack trace so real bugs aren't hidden.

    Teams with Temporal-owned work (``TEAM_SKIP_ACTIVE_JOB_INTERRUPT``) skip
    this: durable workflows outlive the API process and must not be marked
    interrupted on a normal restart.
    """
    if TEAM_SKIP_ACTIVE_JOB_INTERRUPT:
        logger.info(
            "Skipping shutdown active-job interrupt for %s (TEAM_SKIP_ACTIVE_JOB_INTERRUPT)",
            TEAM_NAME,
        )
        return
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


def build_wrapper_body(
    team_name: str,
    team_module: str,
    app_attr: str,
    temporal_module: str = "",
    temporal_func: str = "",
) -> str:
    """Assemble the per-worker ``_team_wrapper.py`` source for a team service.

    Pure (no side effects): returns the wrapper source as a string so the
    generated code can be compiled/asserted in tests. When imported by each
    uvicorn worker the module re-initialises OpenTelemetry, arms fault
    diagnostics + the memory watchdog, imports/builds the FastAPI ``app``,
    instruments it, exposes ``/metrics``, and registers the process-local LLM
    usage flusher (before any Temporal worker, with ``atexit`` shutdown). When
    ``temporal_module`` and ``temporal_func`` are both provided it also registers
    every schema the app exposes via ``app.state.postgres_schemas`` and then
    starts the team's Temporal worker *in this worker process* (gated on
    ``TEMPORAL_ADDRESS``),
    so the module-level Temporal client lives in the same process that serves
    requests — workers are forked after the supervisor starts, so a worker
    started in the supervisor would never be visible here. Registering every
    schema *before* the worker starts closes a cold-start race: the worker must
    not pick up an activity that writes to a table that its own schema hasn't
    created yet — one schema's registration failure is logged but does not
    skip the rest, or the worker start.

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
        - ``temporal_module``/``temporal_func`` are optional. They are embedded
          via ``repr()`` and resolved at runtime with ``importlib``/``getattr``,
          so (unlike ``team_module``) they need no identifier validation and
          cannot inject code. When either is empty no Temporal block is emitted.
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
        "    from shared.observability import init_otel, instrument_fastapi_app\n"
        "except Exception:\n"
        "    _log.warning('shared.observability import failed', exc_info=True)\n"
        "    def init_otel(*_a, **_k):\n"
        "        return None\n"
        "    def instrument_fastapi_app(*_a, **_k):\n"
        "        return None\n"
        "try:\n"
        f"    init_otel(service_name={team_name!r}, team_key={team_name!r})\n"
        "except Exception:\n"
        "    _log.warning('shared.observability init_otel failed', exc_info=True)\n"
    )

    # Each worker is its own process. Under fork (uvicorn's default on Linux) the
    # supervisor's faulthandler + excepthooks are inherited, so this re-arm is a
    # cheap no-op; under a spawn/forkserver start-method the worker is a fresh
    # interpreter and this is what arms them. Either way the memory watchdog must
    # be (re)started here — threads do not survive fork — so an impending OOM
    # kill is logged before the worker vanishes and a native fault dumps a stack.
    body += (
        "try:\n"
        "    from shared.observability import install_fault_diagnostics, start_memory_watchdog\n"
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

    # Capture LLM token usage in this worker process. The observer registry is
    # process-local, so registering only in the unified-api gateway misses every
    # call made here (request handlers and Temporal activities). Register before
    # the Temporal worker starts so in-process activities are captured; the
    # function is idempotent with create_team_app's lifespan. atexit covers
    # router-only teams that have no FastAPI lifespan shutdown.
    body += (
        "try:\n"
        "    import atexit as _atexit\n"
        "    from llm_service.usage_flusher import register_usage_flusher as _ruf\n"
        "    from llm_service.usage_flusher import shutdown as _usage_shutdown\n"
        "    _ruf()\n"
        "    _atexit.register(_usage_shutdown)\n"
        "except Exception:\n"
        "    _log.warning('llm usage flusher registration failed', exc_info=True)\n"
    )

    # Start the team's Temporal worker in THIS worker process. uvicorn forks
    # its workers after the supervisor boots, and the connected client is held
    # in a module-level global, so a worker started in the supervisor is never
    # visible to request handlers. Gated on TEMPORAL_ADDRESS; the start fn is
    # idempotent and self-disables when Temporal is off. Names are embedded via
    # repr() (injection-safe) and resolved with importlib at runtime.
    if temporal_module and temporal_func:
        # Register every one of the team's Postgres schemas BEFORE starting the
        # Temporal worker, but only when TEMPORAL_ADDRESS is set — i.e. only when
        # the worker will actually start. The worker begins picking up (possibly
        # replayed) activities as soon as it starts, and a best-effort Postgres
        # write from an activity would hit an undefined-table error — silently
        # dropping the row — if the worker outran schema creation on a fresh
        # database (the lifespan's registration doesn't run until uvicorn starts
        # serving, after this import-time block). Gating on TEMPORAL_ADDRESS keeps
        # thread/local mode side-effect-free: no worker start ⇒ no race ⇒ leave DDL
        # to the lifespan, honoring shared.postgres's "DDL only from the lifespan"
        # contract. The app was imported above, so its schemas are on
        # app.state.postgres_schemas (primary + any extras). register_team_schemas
        # is a no-op when POSTGRES_HOST is unset, and CREATE TABLE IF NOT EXISTS is
        # idempotent, so re-running it in the lifespan later is harmless. Each
        # schema registers in its own try so one schema's failure is logged but
        # doesn't block the remaining schemas or the worker start. Names are
        # embedded via repr() (injection-safe) and resolved with importlib at
        # runtime.
        body += (
            "try:\n"
            "    import os as _os\n"
            "    if _os.environ.get('TEMPORAL_ADDRESS', '').strip():\n"
            "        try:\n"
            "            _schemas = getattr(getattr(app, 'state', None), 'postgres_schemas', None) or ()\n"
            "            if _schemas:\n"
            "                from shared.postgres import register_team_schemas as _rts\n"
            "                for _schema in _schemas:\n"
            "                    try:\n"
            "                        if _rts(_schema):\n"
            f"                            _log.info('Postgres schema registered before Temporal worker for %s', {team_name!r})\n"
            "                    except Exception:\n"
            "                        _log.warning('pre-Temporal Postgres schema registration failed', exc_info=True)\n"
            "        except Exception:\n"
            "            _log.warning('pre-Temporal Postgres schema registration failed', exc_info=True)\n"
            "        import importlib as _il\n"
            f"        _twfn = getattr(_il.import_module({temporal_module!r}), {temporal_func!r})\n"
            "        if _twfn():\n"
            f"            _log.info('Temporal worker started (per worker) for %s', {team_name!r})\n"
            "except Exception:\n"
            "    _log.warning('Temporal worker startup failed', exc_info=True)\n"
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
      * The team's Temporal worker (when TEAM_TEMPORAL_WORKER_MODULE/_FUNC are
        set), started inside the worker process so its client is visible to
        request handlers.

    The wrapper is re-imported by each uvicorn worker on fork, so per-worker
    instrumentation state is fine with workers>1.
    """
    import pathlib

    # Initialize OpenTelemetry *before* importing the team module so any
    # tracer/meter references captured at import time see the real providers.
    try:
        from shared.observability import init_otel

        init_otel(service_name=TEAM_NAME, team_key=TEAM_NAME)
    except Exception:
        logger.warning("shared.observability init_otel unavailable", exc_info=True)

    # Validate the team module can be imported (fail fast with a clear error).
    try:
        importlib.import_module(TEAM_MODULE)
    except Exception:
        logger.exception("FATAL: cannot import team module %s", TEAM_MODULE)
        raise

    wrapper_path = pathlib.Path("/app/_team_wrapper.py")
    # Guaranteed to exist in the container image, but create it for robustness
    # (e.g. local runs) so the write below can't fail on a missing directory.
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    body = build_wrapper_body(TEAM_NAME, TEAM_MODULE, TEAM_APP_ATTR, TEMPORAL_MODULE, TEMPORAL_FUNC)
    wrapper_path.write_text(body, encoding="utf-8")
    return "_team_wrapper:app"


def _startup_recovery() -> None:
    """Mark any jobs still stuck as 'running' or 'pending' as interrupted.

    On startup, no jobs from a previous process can genuinely be running —
    they are leftovers from a crash or kill where the shutdown hook didn't fire.

    Temporal-owned teams skip this (``TEAM_SKIP_ACTIVE_JOB_INTERRUPT``): their
    workflows keep running across API restarts and job-store rows must not flip
    to ``interrupted`` underneath them.
    """
    if TEAM_SKIP_ACTIVE_JOB_INTERRUPT:
        logger.info(
            "Skipping startup active-job interrupt for %s (TEAM_SKIP_ACTIVE_JOB_INTERRUPT)",
            TEAM_NAME,
        )
        return
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team=TEAM_NAME)
        marked = client.mark_all_active_jobs_interrupted(f"{TEAM_NAME} service restarted — marking orphaned jobs")
        if marked:
            logger.info(
                "Startup recovery: marked %d orphaned job(s) as interrupted for %s",
                len(marked) if isinstance(marked, list) else 1,
                TEAM_NAME,
            )
    except Exception:
        logger.warning("Startup recovery failed for %s", TEAM_NAME, exc_info=True)


if __name__ == "__main__":
    logger.info(
        "Starting %s on port %d (module=%s, workers=%d)",
        TEAM_NAME,
        TEAM_PORT,
        TEAM_MODULE,
        TEAM_WORKERS,
    )
    # Arm fault diagnostics in the supervisor process first; forked workers
    # inherit faulthandler, and the generated wrapper re-arms it per worker.
    try:
        from shared.observability import install_fault_diagnostics

        install_fault_diagnostics(logger)
    except Exception:
        logger.warning("fault diagnostics unavailable", exc_info=True)
    _startup_recovery()
    # The Temporal worker is started per uvicorn worker process from the
    # generated wrapper (see build_wrapper_body) — not here in the supervisor,
    # whose forked workers would never see a client connected pre-fork.
    atexit.register(_shutdown_hook)
    app_import = _resolve_app()
    uvicorn.run(
        app_import,
        host="0.0.0.0",
        port=TEAM_PORT,
        workers=TEAM_WORKERS,
        log_level="info",
    )
