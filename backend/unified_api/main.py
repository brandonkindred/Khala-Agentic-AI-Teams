"""
Unified API Server — reverse-proxy router for Khala team microservices.

Each agent team runs in its own container.  This server:
  1. Proxies ``/api/{team}/*`` requests to the team's container.
  2. Hosts lightweight team-assistant conversational sub-apps.
  3. Runs the security gateway middleware on every team request.
  4. Exposes ``/health``, ``/teams``, and ``/`` info endpoints.

No team code is imported or run in-process.
"""

import asyncio
import logging
import os
import sys
from concurrent import futures
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add agents directory to path (needed for team_assistant, integrations, etc.)
_project_root = Path(__file__).resolve().parent.parent
_agents_dir = _project_root / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from unified_api.config import TEAM_CONFIGS, get_enabled_teams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("unified_api")

# Initialize OpenTelemetry providers as early as possible so every module
# imported below — including the team proxy, security gateway, and any
# assistant sub-apps — uses the real tracer/meter providers.
try:
    from shared_observability import init_otel

    init_otel(service_name="unified-api", team_key="unified_api")
except Exception:
    logger.warning("shared_observability init_otel failed", exc_info=True)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TeamHealth(BaseModel):
    name: str
    prefix: str
    status: str
    enabled: bool


class UnifiedHealthResponse(BaseModel):
    status: str
    version: str
    teams: list[TeamHealth]


class TeamInfo(BaseModel):
    name: str
    prefix: str
    description: str
    tags: list[str]
    enabled: bool


class ApiInfoResponse(BaseModel):
    name: str
    version: str
    description: str
    teams: list[TeamInfo]
    docs_url: str


class SecurityErrorResponse(BaseModel):
    """Response body when the security gateway rejects a request (403)."""

    detail: str
    security_findings: list[str]


# ---------------------------------------------------------------------------
# Team proxy routing (env var → upstream URL)
# ---------------------------------------------------------------------------

TEAM_SERVICE_URL_ENVS: dict[str, str] = {
    "blogging": "BLOGGING_SERVICE_URL",
    "software_engineering": "SOFTWARE_ENGINEERING_SERVICE_URL",
    "personal_assistant": "PERSONAL_ASSISTANT_SERVICE_URL",
    "market_research": "MARKET_RESEARCH_SERVICE_URL",
    "soc2_compliance": "SOC2_COMPLIANCE_SERVICE_URL",
    "social_marketing": "SOCIAL_MARKETING_SERVICE_URL",
    "branding": "BRANDING_SERVICE_URL",
    "agent_provisioning": "AGENT_PROVISIONING_SERVICE_URL",
    "accessibility_audit": "ACCESSIBILITY_AUDIT_SERVICE_URL",
    "ai_systems": "AI_SYSTEMS_SERVICE_URL",
    "investment": "INVESTMENT_SERVICE_URL",
    "nutrition_meal_planning": "NUTRITION_MEAL_PLANNING_SERVICE_URL",
    "planning_v3": "PLANNING_V3_SERVICE_URL",
    "coding_team": "CODING_TEAM_SERVICE_URL",
    "sales_team": "SALES_TEAM_SERVICE_URL",
    "road_trip_planning": "ROAD_TRIP_PLANNING_SERVICE_URL",
    "agentic_team_provisioning": "AGENTIC_TEAM_PROVISIONING_SERVICE_URL",
    "startup_advisor": "STARTUP_ADVISOR_SERVICE_URL",
    "user_agent_founder": "USER_AGENT_FOUNDER_SERVICE_URL",
    "deepthought": "DEEPTHOUGHT_SERVICE_URL",
}

# Track which teams were successfully registered (for health endpoint).
_registered_teams: dict[str, bool] = {}

# Track upstream liveness per team (updated by background health checker).
_team_liveness: dict[str, str] = {}  # team_key -> "healthy" | "unhealthy" | "unknown"

# In-process teams whose Postgres schema registration failed at startup.
# Health reports these as "unhealthy" so operators see the broken
# persistence instead of a green light beside endpoints that 503.
_in_process_schema_failures: set[str] = set()

# Background health check interval in seconds.
_HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "30"))


async def _check_team_health(team_key: str, service_url: str) -> str:
    """Probe a team's /health endpoint. Returns 'healthy' or 'unhealthy'."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{service_url.rstrip('/')}/health")
            return "healthy" if resp.status_code == 200 else "unhealthy"
    except Exception:
        return "unhealthy"


async def _health_check_loop() -> None:
    """Periodically probe all registered teams' health endpoints.

    Also retries schema registration for in-process teams whose startup
    DDL failed — Codex flagged that doing this from `/health` makes the
    handler mutate state on every probe (no throttling, no isolation).
    Running it here gives both: a natural cooldown via the loop interval
    and a dedicated executor (`_PROBE_EXECUTOR`) instead of the default
    threadpool.
    """
    while True:
        await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
        for team_key in list(_registered_teams.keys()):
            if not _registered_teams.get(team_key):
                continue
            env_var = TEAM_SERVICE_URL_ENVS.get(team_key)
            url = (os.environ.get(env_var, "").strip() if env_var else "") if env_var else ""
            if url:
                status = await _check_team_health(team_key, url)
                _team_liveness[team_key] = status
        # Background schema retries for in-process teams that failed at
        # startup. The interval (default 30s) is the cooldown — no
        # tighter retry loop, no per-/health-call execution. Runs in
        # the dedicated `_PROBE_EXECUTOR` so DDL doesn't compete with
        # other `to_thread` work in the default executor.
        if _in_process_schema_failures:
            db_live = await _probe_postgres_live()
            if db_live:
                loop = asyncio.get_running_loop()
                for team_key in list(_in_process_schema_failures):
                    try:
                        await loop.run_in_executor(
                            _PROBE_EXECUTOR,
                            _retry_in_process_schema_registration,
                            team_key,
                        )
                    except Exception:
                        logger.warning(
                            "Background schema retry failed for %s",
                            team_key,
                            exc_info=True,
                        )


def _register_proxy_routes(app: FastAPI) -> dict[str, bool]:
    """Register a catch-all proxy route for every enabled team whose service URL is configured."""
    from unified_api.team_proxy import proxy_request

    results: dict[str, bool] = {}
    enabled = get_enabled_teams()

    for team_key, config in enabled.items():
        # In-process teams are served by `app.include_router(...)`; no
        # upstream container, so no proxy. They still count as
        # "registered" for discovery purposes since the route is live.
        if config.in_process:
            results[team_key] = True
            continue
        env_var = TEAM_SERVICE_URL_ENVS.get(team_key)
        url = (os.environ.get(env_var, "").strip() if env_var else "") if env_var else ""
        if not url:
            logger.warning("Team %s has no service URL configured (%s); skipping", team_key, env_var)
            results[team_key] = False
            continue

        @app.api_route(
            config.prefix + "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            name=f"{team_key}_proxy",
            tags=config.tags,
        )
        async def _proxy(
            request: Request,
            path: str,
            _url: str = url,
            _team_key: str = team_key,
            _timeout: float = config.timeout_seconds,
        ) -> Any:
            return await proxy_request(request, _url, path, team_key=_team_key, timeout=_timeout)

        logger.info(
            "Proxying %s -> %s (timeout=%.0fs, cell=%s)", config.prefix, url, config.timeout_seconds, config.cell
        )
        results[team_key] = True

    return results


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: register own Postgres schemas, mount assistant sub-apps,
    then register proxy routes.

    Order matters: assistant sub-apps must be mounted before proxy catch-all routes,
    otherwise the proxy's ``/{path:path}`` pattern swallows assistant requests.
    """
    global _registered_teams
    logger.info("Starting Unified API Server...")

    # Reset stale failure markers from a previous lifespan run (e.g.
    # uvicorn `--reload`, in-process test fixtures that boot the app
    # multiple times). Without this, a transient Postgres outage on the
    # first boot would mark the team unhealthy forever.
    _in_process_schema_failures.clear()

    # 0. Register Postgres schemas for modules that run in-process here
    #    (unified_api itself + the team_assistant conversation store that we
    #    mount as sub-apps). No-op when POSTGRES_HOST is unset.
    try:
        from shared_postgres import register_team_schemas
        from unified_api.postgres import SCHEMA as UNIFIED_API_SCHEMA

        register_team_schemas(UNIFIED_API_SCHEMA)
    except Exception:
        logger.exception("unified_api postgres schema registration failed")

    try:
        from shared_postgres import register_team_schemas
        from team_assistant.postgres import SCHEMA as TEAM_ASSISTANT_SCHEMA

        register_team_schemas(TEAM_ASSISTANT_SCHEMA)
    except Exception:
        logger.exception("team_assistant postgres schema registration failed")

    try:
        from agent_console.postgres import SCHEMA as AGENT_CONSOLE_SCHEMA
        from shared_postgres import register_team_schemas

        register_team_schemas(AGENT_CONSOLE_SCHEMA)
    except Exception:
        logger.exception("agent_console postgres schema registration failed")

    # Gate the entire product_delivery startup block on the team's
    # `enabled` flag. Disabling the team must also disable its startup
    # side effects (schema DDL, failure logs, health markers) — not
    # just the routes.
    if TEAM_CONFIGS["product_delivery"].enabled:
        try:
            from product_delivery.postgres import SCHEMA as PRODUCT_DELIVERY_SCHEMA
            from shared_postgres import ensure_team_schema, is_postgres_enabled

            # Use `ensure_team_schema` directly (rather than the
            # `register_team_schemas` boolean wrapper) so we can detect
            # partial DDL: if a single CREATE/ALTER statement fails it's
            # logged-and-skipped and `applied < total` — the team's still
            # mounted but its persistence is broken.
            if is_postgres_enabled():
                applied = ensure_team_schema(PRODUCT_DELIVERY_SCHEMA)
                total = len(PRODUCT_DELIVERY_SCHEMA.statements)
                if applied < total:
                    logger.warning(
                        "product_delivery: %d/%d DDL statements applied; marking unhealthy",
                        applied,
                        total,
                    )
                    _in_process_schema_failures.add("product_delivery")
            else:
                # Postgres disabled → every persistence call will 503.
                # Don't add to `_in_process_schema_failures` (which
                # tracks broken state, not opt-out): the health handler
                # sees `is_postgres_enabled()` is False and reports
                # `unavailable` instead of `unhealthy`, so the unified
                # API doesn't flag overall health degraded for an
                # intentionally-undeployed feature.
                logger.warning("product_delivery: Postgres disabled; persistence endpoints will return 503")
        except Exception:
            logger.exception("product_delivery postgres schema registration failed")
            _in_process_schema_failures.add("product_delivery")

    # 1. Mount team assistant conversational sub-apps (before proxy routes).
    try:
        from team_assistant.api import create_assistant_app
        from team_assistant.config import TEAM_ASSISTANT_CONFIGS

        assistant_count = 0
        for team_key, assistant_config in TEAM_ASSISTANT_CONFIGS.items():
            team_cfg = TEAM_CONFIGS.get(team_key)
            if team_cfg:
                assistant_app = create_assistant_app(assistant_config)
                assistant_app.add_middleware(
                    CORSMiddleware,
                    allow_origins=["*"],
                    allow_credentials=True,
                    allow_methods=["*"],
                    allow_headers=["*"],
                )
                app.mount(f"{team_cfg.prefix}/assistant", assistant_app)
                assistant_count += 1
        logger.info("Mounted %d team assistant sub-apps", assistant_count)
    except Exception:
        logger.warning("Could not mount team assistant sub-apps", exc_info=True)

    # 2. Register proxy routes for all team containers (after assistant mounts).
    _registered_teams = _register_proxy_routes(app)
    ok = sum(1 for v in _registered_teams.values() if v)
    total = len(get_enabled_teams())
    logger.info("Registered %d/%d team proxy routes", ok, total)

    # 3. Start background health checker for upstream team liveness.
    health_task = asyncio.create_task(_health_check_loop())
    logger.info("Started background health checker (interval=%ds)", _HEALTH_CHECK_INTERVAL)

    # 4. Start the Agent Console sandbox idle reaper.
    sandbox_reaper_task: asyncio.Task | None = None
    try:
        from agent_provisioning_team.sandbox import run_idle_reaper

        sandbox_reaper_task = asyncio.create_task(run_idle_reaper())
        logger.info("Started Agent Console sandbox idle reaper")
    except Exception:
        logger.warning("Agent Console sandbox reaper failed to start", exc_info=True)

    # 5. Start the Agent Console run pruner (Phase 3).
    run_pruner_task: asyncio.Task | None = None
    try:
        from agent_console.prune import run_pruner

        run_pruner_task = asyncio.create_task(run_pruner())
        logger.info("Started Agent Console run pruner")
    except Exception:
        logger.warning("Agent Console run pruner failed to start", exc_info=True)

    yield

    if run_pruner_task is not None:
        run_pruner_task.cancel()
    if sandbox_reaper_task is not None:
        sandbox_reaper_task.cancel()
    health_task.cancel()

    # Close Postgres connection pools owned by shared_postgres.
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("shared_postgres close_pool failed", exc_info=True)

    # Shut down the dedicated health-probe executor so worker threads
    # don't outlive the app — important under reload / test processes
    # that recreate app state. ``cancel_futures=True`` drops queued
    # work; in-flight DB pings finish naturally because they were
    # already capped by the inner connection timeout.
    _PROBE_EXECUTOR.shutdown(wait=False, cancel_futures=True)

    logger.info("Shutting down Unified API Server...")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Khala Unified API",
    description=(
        "Reverse-proxy router for all Khala team microservices. "
        "Each team runs in its own container; this server routes requests, "
        "hosts team assistant chat, and enforces the security gateway."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security gateway
from unified_api.middleware import SecurityGatewayMiddleware

app.add_middleware(SecurityGatewayMiddleware)

# OpenTelemetry FastAPI instrumentation — server spans for every request,
# trace IDs injected into logs, and outbound httpx calls nested under the
# request span automatically.
try:
    from shared_observability import instrument_fastapi_app

    instrument_fastapi_app(app, team_key="unified_api")
except Exception:
    logger.warning("OpenTelemetry FastAPI instrumentation unavailable", exc_info=True)

# Prometheus metrics — exposes GET /metrics for scraping. SecurityGatewayMiddleware
# only intercepts /api/{team}/* paths, so /metrics bypasses it automatically.
try:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/health"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False, tags=["observability"])
except Exception:
    logger.warning("prometheus instrumentator unavailable", exc_info=True)

# Integrations API (Slack config, etc.)
from unified_api.routes.agent_console_diff import router as agent_console_diff_router
from unified_api.routes.agent_console_saved_inputs import (
    router as agent_console_saved_inputs_router,
)
from unified_api.routes.agents import router as agents_router
from unified_api.routes.analytics import router as analytics_router
from unified_api.routes.integrations import router as integrations_router
from unified_api.routes.llm_tools import router as llm_tools_router
from unified_api.routes.llm_usage import router as llm_usage_router
from unified_api.routes.sandboxes import router as sandboxes_router

app.include_router(integrations_router)
app.include_router(llm_tools_router)
app.include_router(llm_usage_router)
app.include_router(analytics_router)
app.include_router(agents_router)
app.include_router(sandboxes_router)
app.include_router(agent_console_saved_inputs_router)
app.include_router(agent_console_diff_router)
# Honor the in-process team's `enabled` flag: an operator that disables
# the team via TEAM_CONFIGS expects /api/product-delivery/* to stop
# answering, not just disappear from /teams. Gate the *import* too —
# Codex flagged that an unconditional import can take down unified_api
# at startup with an import-time failure (missing transitive dep,
# broken module, etc.) even when the team is disabled. With the gate,
# disabling product_delivery in config skips the module graph
# entirely.
if TEAM_CONFIGS["product_delivery"].enabled:
    from unified_api.routes.product_delivery import register_pd_exception_handlers
    from unified_api.routes.product_delivery import router as product_delivery_router

    app.include_router(product_delivery_router)
    register_pd_exception_handlers(app)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/", response_model=ApiInfoResponse, tags=["root"])
async def root() -> ApiInfoResponse:
    """Unified API info and list of available teams."""
    teams = [
        TeamInfo(
            name=config.name,
            prefix=config.prefix,
            description=config.description,
            tags=config.tags,
            enabled=config.enabled and _registered_teams.get(key, False),
        )
        for key, config in TEAM_CONFIGS.items()
    ]
    return ApiInfoResponse(
        name="Khala Unified API",
        version="1.0.0",
        description="Reverse-proxy router for all Khala team microservices",
        teams=teams,
        docs_url="/docs",
    )


# Dedicated, bounded executor for the `/health` Postgres probe.
# Codex flagged that `asyncio.to_thread` cannot interrupt the underlying
# psycopg call on `wait_for` timeout: under a Postgres outage every
# probe leaves a worker blocked in `pool.connection()` until the pool's
# own timeout elapses, which can quickly exhaust the default executor
# (and starve every other `to_thread`-using path in the app — file
# I/O, integrations, etc.).
#
# Two-pronged fix:
#   1. Run the probe in its own small executor so a flooded /health
#      can't drag down unrelated work.
#   2. Cap the connection-acquisition wait via psycopg's own timeout
#      knob (`pool.connection(timeout=…)`) so the worker itself can't
#      block longer than the budget — the thread always exits cleanly.
_PROBE_EXECUTOR = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="pd-health-probe")
_PROBE_DB_TIMEOUT_S = 1.5


async def _probe_postgres_live() -> bool:
    """Run ``SELECT 1`` against the shared pool with a short timeout.

    Used by the in-process team health branch so a runtime Postgres
    outage (pool exhausted, host unreachable, FK chain broken) flips
    those teams to ``unhealthy`` immediately instead of leaving the
    startup-time success result frozen until the next process restart.
    Anything that doesn't return ``True`` quickly is treated as a
    fail — better to flap to ``unhealthy`` briefly than miss a real
    outage. Runs in a dedicated 2-worker executor with an inner
    psycopg-level timeout so a stalled DB can't accumulate orphaned
    workers in the default threadpool.
    """

    def _ping() -> bool:
        from shared_postgres import client as _pg_client
        from shared_postgres import is_postgres_enabled

        if not is_postgres_enabled():
            return False
        try:
            # Bound the connection acquisition itself so the worker
            # thread can't block longer than `_PROBE_DB_TIMEOUT_S`. We
            # reach through `client._get_or_create_pool` (rather than
            # `get_conn()`) because the public helper doesn't expose a
            # timeout knob today and the probe must be hard-bounded.
            pool = _pg_client._get_or_create_pool()
            with pool.connection(timeout=_PROBE_DB_TIMEOUT_S) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                return row is not None and row[0] == 1
        except Exception:
            return False

    loop = asyncio.get_running_loop()
    try:
        # Outer wait_for gives the await an upper bound even if the
        # inner psycopg timeout fires later than expected. Worker
        # cleanup is guaranteed by the inner timeout; this is just
        # belt-and-suspenders for the await side.
        return await asyncio.wait_for(
            loop.run_in_executor(_PROBE_EXECUTOR, _ping),
            timeout=_PROBE_DB_TIMEOUT_S + 0.5,
        )
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


def _is_postgres_enabled_cached() -> bool:
    """``shared_postgres.is_postgres_enabled()`` without the import dance.

    Just reads the env var directly so the health handler doesn't pay
    an import on every call. Postgres-disabled environments use this
    to drop in-process teams to ``unavailable`` rather than ``unhealthy``.
    """
    return bool(os.environ.get("POSTGRES_HOST", "").strip())


def _retry_in_process_schema_registration(team_key: str) -> bool:
    """Re-run schema registration for a team after a transient outage.

    Called from `/health` when the live DB probe succeeds for a team
    that was added to `_in_process_schema_failures` at startup —
    typically because Postgres wasn't reachable when the lifespan
    fired but is reachable now. Uses `ensure_team_schema` (rather than
    the boolean `register_team_schemas` wrapper) so we can detect
    *partial* DDL — that helper logs-and-skips per-statement errors
    and would otherwise return success after applying only some
    statements, flipping `/health` to `healthy` while required tables
    or indexes are still missing. We only clear the failure flag when
    `applied == total`.

    Synchronous (DDL is sync); the caller wraps this in
    ``asyncio.to_thread`` so it doesn't block the event loop.
    """
    try:
        if team_key == "product_delivery":
            from product_delivery.postgres import SCHEMA as PRODUCT_DELIVERY_SCHEMA
            from shared_postgres import ensure_team_schema

            applied = ensure_team_schema(PRODUCT_DELIVERY_SCHEMA)
            total = len(PRODUCT_DELIVERY_SCHEMA.statements)
            if applied < total:
                logger.warning(
                    "product_delivery retry: %d/%d DDL statements applied; "
                    "still unhealthy (some required tables or indexes are missing)",
                    applied,
                    total,
                )
                return False
            _in_process_schema_failures.discard(team_key)
            logger.info(
                "product_delivery: schema re-registration succeeded (%d/%d); clearing health flag",
                applied,
                total,
            )
            return True
        # Other in-process teams (agent_console, team_assistant, etc.)
        # don't currently track their schema-failure flag through this
        # set, so there's nothing to retry. Add cases here as they
        # adopt the pattern.
        return False
    except Exception:
        logger.warning("Schema re-registration retry failed for %s", team_key, exc_info=True)
        return False


@app.get("/health", response_model=UnifiedHealthResponse, tags=["health"])
async def health() -> UnifiedHealthResponse:
    """Unified health check — reports proxy registration and upstream liveness per team.

    Read-only: Codex flagged that triggering DDL retries from a public
    GET endpoint is operationally surprising and lets every health
    probe (load balancer, monitoring) repeatedly attempt
    `CREATE TABLE/INDEX`. Schema retries now run on a throttled
    background task (`_health_check_loop`) so this handler only reads
    state — no DDL, no `_retry_in_process_schema_registration`.
    """
    teams = []
    all_healthy = True
    # Lazily probe the live DB only if at least one in-process team
    # would otherwise report `healthy` — avoids paying the round trip
    # when every in-process team is already disabled or has a startup
    # schema failure recorded.
    db_live: bool | None = None
    for key, config in TEAM_CONFIGS.items():
        registered = _registered_teams.get(key, False)
        liveness = _team_liveness.get(key, "unknown")
        # Tracks whether `unavailable` here is *intentional* (the
        # in-process Postgres-disabled case) or *misconfigured* (proxy
        # team without a service URL). Only the misconfigured case
        # should flip overall health to `degraded` — Codex flagged
        # that the previous "any unavailable degrades" rule masked
        # real proxy misconfigurations and the new "no unavailable
        # degrades" rule overcorrected.
        intentionally_unavailable = False
        if config.in_process:
            # No upstream container, but the in-process router still
            # depends on Postgres for product_delivery / agent_console.
            # Four states matter:
            #   * disabled → routes are unmounted; report "unavailable"
            #     (intentional — operator opt-out via TEAM_CONFIGS).
            #   * Postgres not configured → "unavailable" (intentional —
            #     the env explicitly opted out by not setting
            #     `POSTGRES_HOST`).
            #   * schema registration failed at startup AND the
            #     background retry hasn't healed it yet → "unhealthy"
            #     (broken).
            #   * runtime DB probe fails → "unhealthy" (active outage).
            #   * otherwise → "healthy".
            if not config.enabled or not _is_postgres_enabled_cached():
                # Either operator opt-out (config-disabled team) or
                # env opt-out (no `POSTGRES_HOST`). Both are
                # intentional and should not flip overall health.
                status = "unavailable"
                intentionally_unavailable = True
            elif key in _in_process_schema_failures:
                # Background loop may have already cleared this; if
                # we're still in the set, persistence is still broken.
                # Read-only: don't trigger a retry from this handler.
                status = "unhealthy"
            else:
                if db_live is None:
                    db_live = await _probe_postgres_live()
                status = "healthy" if db_live else "unhealthy"
        elif registered and liveness == "healthy":
            status = "healthy"
        elif registered and liveness == "unknown":
            status = "healthy"  # Not yet checked — assume healthy
        elif registered:
            status = "unhealthy"
        else:
            # Proxy team that isn't registered — typically because its
            # service URL is missing. That's a deployment misconfig,
            # not an opt-out, so it should degrade overall health.
            status = "unavailable"
        if config.enabled and (status == "unhealthy" or (status == "unavailable" and not intentionally_unavailable)):
            all_healthy = False
        teams.append(TeamHealth(name=config.name, prefix=config.prefix, status=status, enabled=config.enabled))
    return UnifiedHealthResponse(
        status="healthy" if all_healthy else "degraded",
        version="1.0.0",
        teams=teams,
    )


@app.get("/teams", tags=["root"])
async def list_teams() -> dict[str, Any]:
    """List all available teams with their proxy status."""
    teams = {}
    for key, config in TEAM_CONFIGS.items():
        registered = _registered_teams.get(key, False)
        # In-process teams piggy-back on the unified API's `/docs` —
        # they don't expose a `/api/<team>/docs` endpoint themselves,
        # so don't advertise one (it would 404).
        per_team_docs = registered and not config.in_process
        teams[key] = {
            "name": config.name,
            "prefix": config.prefix,
            "description": config.description,
            "registered": registered,
            "enabled": config.enabled,
            "docs_url": f"{config.prefix}/docs" if per_team_docs else None,
        }
    return {"teams": teams}


# ---------------------------------------------------------------------------
# Generic job management (proxies to job-service for any team)
# ---------------------------------------------------------------------------

_JOB_SERVICE_URL = os.environ.get("JOB_SERVICE_URL", "http://job-service:8085")


@app.get("/api/jobs/{team}", tags=["jobs"])
async def list_team_jobs(team: str, running_only: bool = False) -> dict[str, Any]:
    """List all jobs for a team via the job service."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        url = f"{_JOB_SERVICE_URL}/jobs/{team}"
        if running_only:
            url += "?statuses=pending&statuses=running"
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


@app.delete("/api/jobs/{team}/{job_id}", tags=["jobs"])
async def delete_job(team: str, job_id: str) -> dict[str, Any]:
    """Delete a job for any team. Works regardless of job status."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(f"{_JOB_SERVICE_URL}/jobs/{team}/{job_id}")
        resp.raise_for_status()
        return resp.json()


@app.post("/api/jobs/{team}/{job_id}/cancel", tags=["jobs"])
async def cancel_job(team: str, job_id: str) -> dict[str, Any]:
    """Force-cancel a running or pending job by setting its status to cancelled."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(
            f"{_JOB_SERVICE_URL}/jobs/{team}/{job_id}",
            json={"heartbeat": False, "fields": {"status": "cancelled", "error": "Cancelled by user"}},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/jobs/{team}/{job_id}/interrupt", tags=["jobs"])
async def interrupt_job(team: str, job_id: str) -> dict[str, Any]:
    """Mark a job as interrupted (e.g. after detecting it's stale)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(
            f"{_JOB_SERVICE_URL}/jobs/{team}/{job_id}",
            json={"heartbeat": False, "fields": {"status": "interrupted", "error": "Marked interrupted by user"}},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/jobs/{team}/{job_id}/resume", tags=["jobs"])
async def resume_job(team: str, job_id: str) -> dict[str, Any]:
    """Reset a failed/interrupted/cancelled job back to running so its team can pick it up."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(
            f"{_JOB_SERVICE_URL}/jobs/{team}/{job_id}",
            json={"heartbeat": True, "fields": {"status": "running", "error": None}},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/jobs/{team}/{job_id}/restart", tags=["jobs"])
async def restart_job(team: str, job_id: str) -> dict[str, Any]:
    """Reset a job to pending so its team re-executes it from scratch."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(
            f"{_JOB_SERVICE_URL}/jobs/{team}/{job_id}",
            json={"heartbeat": True, "fields": {"status": "pending", "error": None}},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/jobs/{team}/mark-all-interrupted", tags=["jobs"])
async def mark_all_interrupted(team: str) -> dict[str, Any]:
    """Mark all running/pending jobs for a team as interrupted."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_JOB_SERVICE_URL}/jobs/{team}/mark-all-running-interrupted",
            json={"reason": "Bulk interrupted by user"},
        )
        resp.raise_for_status()
        return resp.json()
