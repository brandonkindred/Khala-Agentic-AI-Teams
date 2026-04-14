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
    """Periodically probe all registered teams' health endpoints."""
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


def _register_proxy_routes(app: FastAPI) -> dict[str, bool]:
    """Register a catch-all proxy route for every enabled team whose service URL is configured."""
    from unified_api.team_proxy import proxy_request

    results: dict[str, bool] = {}
    enabled = get_enabled_teams()

    for team_key, config in enabled.items():
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

    yield

    health_task.cancel()

    # Close Postgres connection pools owned by shared_postgres.
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("shared_postgres close_pool failed", exc_info=True)

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
from unified_api.routes.analytics import router as analytics_router
from unified_api.routes.integrations import router as integrations_router
from unified_api.routes.llm_tools import router as llm_tools_router
from unified_api.routes.llm_usage import router as llm_usage_router

app.include_router(integrations_router)
app.include_router(llm_tools_router)
app.include_router(llm_usage_router)
app.include_router(analytics_router)


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


@app.get("/health", response_model=UnifiedHealthResponse, tags=["health"])
async def health() -> UnifiedHealthResponse:
    """Unified health check — reports proxy registration and upstream liveness per team."""
    teams = []
    all_healthy = True
    for key, config in TEAM_CONFIGS.items():
        registered = _registered_teams.get(key, False)
        liveness = _team_liveness.get(key, "unknown")
        if registered and liveness == "healthy":
            status = "healthy"
        elif registered and liveness == "unknown":
            status = "healthy"  # Not yet checked — assume healthy
        elif registered:
            status = "unhealthy"
        else:
            status = "unavailable"
        if config.enabled and status in ("unavailable", "unhealthy"):
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
        teams[key] = {
            "name": config.name,
            "prefix": config.prefix,
            "description": config.description,
            "registered": registered,
            "enabled": config.enabled,
            "docs_url": f"{config.prefix}/docs" if registered else None,
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
