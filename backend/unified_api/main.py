"""
Unified API Server - Consolidates all Strands Agent team APIs.

This server mounts all agent team APIs under namespaced prefixes,
providing a single entry point for the entire platform.

Route Prefixes:
- /api/blogging          - Blog research, planning, draft, copy-edit, publication
- /api/software-engineering - Full dev team simulation
- /api/personal-assistant   - Personal assistant (email, calendar, tasks)
- /api/market-research      - Market research and UX synthesis
- /api/soc2-compliance      - SOC2 compliance audit
- /api/social-marketing     - Social media campaign planning
- /api/branding             - Brand strategy and design
- /api/agent-provisioning   - Agent environment provisioning
- /api/ai-systems           - Spec-driven AI agent system factory
- /api/investment           - Investment analysis and portfolio management
"""

import atexit
import importlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add agents directory to path for imports
_project_root = Path(__file__).resolve().parent.parent
_agents_dir = _project_root / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Add StudioGrid src directory to path (it lives outside the agents/ tree)
_studiogrid_src = _project_root / "studiogrid" / "src"
if str(_studiogrid_src) not in sys.path:
    sys.path.insert(0, str(_studiogrid_src))

from unified_api.config import TEAM_CONFIGS, get_enabled_teams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("unified_api")


class TeamHealth(BaseModel):
    """Health status for a single team."""

    name: str
    prefix: str
    status: str
    enabled: bool


class UnifiedHealthResponse(BaseModel):
    """Unified health check response."""

    status: str
    version: str
    teams: list[TeamHealth]


class TeamInfo(BaseModel):
    """Information about a mounted team API."""

    name: str
    prefix: str
    description: str
    tags: list[str]
    enabled: bool


class ApiInfoResponse(BaseModel):
    """API information response."""

    name: str
    version: str
    description: str
    teams: list[TeamInfo]
    docs_url: str


class SecurityErrorResponse(BaseModel):
    """Response body when the security gateway rejects a request (403)."""

    detail: str
    security_findings: list[str]


# Track mounted routers for health checks
_mounted_teams: dict[str, bool] = {}

# Team keys that have async jobs: on shutdown, mark all running jobs as failed via the job service.
# Maps team_key -> team name used in the job service.
SHUTDOWN_HOOKS: dict[str, str] = {
    "blogging": "blogging_team",
    "software_engineering": "software_engineering_team",
    "personal_assistant": "personal_assistant_team",
    "agent_provisioning": "agent_provisioning_team",
    "ai_systems": "ai_systems_team",
    "soc2_compliance": "soc2_compliance_team",
    "social_marketing": "social_media_marketing_team",
    "accessibility_audit": "accessibility_audit_team",
    "nutrition_meal_planning": "nutrition_meal_planning_team",
    "planning_v3": "planning_v3_team",
    "sales_team": "sales_team",
    "road_trip_planning": "road_trip_planning_team",
}


def _run_shutdown_hooks(reason: str) -> None:
    """Mark all running jobs as failed for each mounted team via the job service. Used by lifespan and atexit."""
    try:
        from job_service_client import JobServiceClient
    except ImportError:
        logger.warning("job_service_client not available; skipping shutdown hooks")
        return
    for team_key, team_name in SHUTDOWN_HOOKS.items():
        if not _mounted_teams.get(team_key):
            continue
        # Proxied teams handle their own shutdown in their container.
        if _is_proxied(team_key):
            continue
        try:
            client = JobServiceClient(team=team_name)
            client.mark_all_active_jobs_failed(reason)
        except Exception as e:
            logger.warning("Shutdown hook for team %s failed: %s", team_key, e)


# Maps team_key -> env var name for teams that can be proxied to an external container.
# When the env var is set, requests are forwarded via HTTP instead of mounting the team in-process.
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
    "studio_grid": "STUDIO_GRID_SERVICE_URL",
    "sales_team": "SALES_TEAM_SERVICE_URL",
    "road_trip_planning": "ROAD_TRIP_PLANNING_SERVICE_URL",
    "agentic_team_provisioning": "AGENTIC_TEAM_PROVISIONING_SERVICE_URL",
    "startup_advisor": "STARTUP_ADVISOR_SERVICE_URL",
}


def _is_proxied(team_key: str) -> bool:
    """Return True if *team_key* is configured to proxy to an external service."""
    env_var = TEAM_SERVICE_URL_ENVS.get(team_key)
    return bool(env_var and os.environ.get(env_var, "").strip())


def try_mount_or_proxy(
    app: FastAPI,
    team_key: str,
    import_path: str,
    app_attr: str = "app",
    service_url_env: str | None = None,
) -> bool:
    """Mount a team API directly or proxy to its external microservice.

    When *service_url_env* is provided and the env var is set, a catch-all
    route is registered that forwards all requests to the team's container.
    Otherwise the team's FastAPI app is imported and mounted in-process.
    """
    config = TEAM_CONFIGS[team_key]

    # Proxy mode: forward to external container.
    if service_url_env:
        url = os.environ.get(service_url_env, "").strip()
        if url:
            from unified_api.team_proxy import proxy_request

            @app.api_route(
                config.prefix + "/{path:path}",
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                name=f"{team_key}_proxy",
                tags=config.tags,
            )
            async def _proxy(request: Request, path: str, _url: str = url) -> Any:
                return await proxy_request(request, _url, path)

            logger.info("Proxying %s -> %s", config.prefix, url)
            return True

    # Direct mount: import team's FastAPI app in-process.
    try:
        mod = importlib.import_module(import_path)
        team_app = getattr(mod, app_attr)
        app.mount(config.prefix, team_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount %s API: %s", config.name, e)
        return False


def _try_mount_blogging(app: FastAPI) -> bool:
    """Mount blogging team API — proxy to container or direct mount."""
    return try_mount_or_proxy(app, "blogging", "blogging.api.main", service_url_env="BLOGGING_SERVICE_URL")


def _try_mount_software_engineering(app: FastAPI) -> bool:
    """Mount software engineering team API."""
    return try_mount_or_proxy(
        app,
        "software_engineering",
        "software_engineering_team.api.main",
        service_url_env="SOFTWARE_ENGINEERING_SERVICE_URL",
    )


def _try_mount_personal_assistant(app: FastAPI) -> bool:
    """Mount personal assistant team API."""
    return try_mount_or_proxy(
        app,
        "personal_assistant",
        "personal_assistant_team.api.main",
        service_url_env="PERSONAL_ASSISTANT_SERVICE_URL",
    )


def _try_mount_market_research(app: FastAPI) -> bool:
    """Mount market research team API."""
    return try_mount_or_proxy(
        app,
        "market_research",
        "market_research_team.api.main",
        service_url_env="MARKET_RESEARCH_SERVICE_URL",
    )


def _try_mount_soc2_compliance(app: FastAPI) -> bool:
    """Mount SOC2 compliance team API."""
    return try_mount_or_proxy(
        app,
        "soc2_compliance",
        "soc2_compliance_team.api.main",
        service_url_env="SOC2_COMPLIANCE_SERVICE_URL",
    )


def _try_mount_social_marketing(app: FastAPI) -> bool:
    """Mount social media marketing team API."""
    return try_mount_or_proxy(
        app,
        "social_marketing",
        "social_media_marketing_team.api.main",
        service_url_env="SOCIAL_MARKETING_SERVICE_URL",
    )


def _try_mount_branding(app: FastAPI) -> bool:
    """Mount branding team API."""
    return try_mount_or_proxy(
        app,
        "branding",
        "branding_team.api.main",
        service_url_env="BRANDING_SERVICE_URL",
    )


def _try_mount_agent_provisioning(app: FastAPI) -> bool:
    """Mount agent provisioning team API."""
    return try_mount_or_proxy(
        app,
        "agent_provisioning",
        "agent_provisioning_team.api.main",
        service_url_env="AGENT_PROVISIONING_SERVICE_URL",
    )


def _try_mount_accessibility_audit(app: FastAPI) -> bool:
    """Mount accessibility audit team API.

    This team exports a router (not an app), so the local-mount path wraps it
    in a sub-app.  When proxied, the proxy handles everything transparently.
    """
    if _is_proxied("accessibility_audit"):
        return try_mount_or_proxy(
            app,
            "accessibility_audit",
            "",
            service_url_env="ACCESSIBILITY_AUDIT_SERVICE_URL",
        )
    try:
        from accessibility_audit_team.api.main import router as a11y_router

        a11y_app = FastAPI(
            title="Accessibility Audit API",
            description="WCAG 2.2 and Section 508 accessibility auditing for web and mobile",
        )
        a11y_app.include_router(a11y_router)

        config = TEAM_CONFIGS["accessibility_audit"]
        app.mount(config.prefix, a11y_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Accessibility Audit API: %s", e)
        return False


def _try_mount_ai_systems(app: FastAPI) -> bool:
    """Mount AI systems team API."""
    return try_mount_or_proxy(
        app,
        "ai_systems",
        "ai_systems_team.api.main",
        service_url_env="AI_SYSTEMS_SERVICE_URL",
    )


def _try_mount_investment(app: FastAPI) -> bool:
    """Mount investment team API."""
    return try_mount_or_proxy(
        app,
        "investment",
        "investment_team.api.main",
        service_url_env="INVESTMENT_SERVICE_URL",
    )


def _try_mount_nutrition_meal_planning(app: FastAPI) -> bool:
    """Mount nutrition & meal planning team API."""
    return try_mount_or_proxy(
        app,
        "nutrition_meal_planning",
        "nutrition_meal_planning_team.api.main",
        service_url_env="NUTRITION_MEAL_PLANNING_SERVICE_URL",
    )


def _try_mount_planning_v3(app: FastAPI) -> bool:
    """Mount Planning V3 team API."""
    return try_mount_or_proxy(
        app,
        "planning_v3",
        "planning_v3_team.api.main",
        service_url_env="PLANNING_V3_SERVICE_URL",
    )


def _try_mount_coding_team(app: FastAPI) -> bool:
    """Mount Coding Team API."""
    return try_mount_or_proxy(
        app,
        "coding_team",
        "coding_team.api.main",
        service_url_env="CODING_TEAM_SERVICE_URL",
    )


def _try_mount_studio_grid(app: FastAPI) -> bool:
    """Mount StudioGrid design-system workflow API."""
    return try_mount_or_proxy(
        app,
        "studio_grid",
        "studiogrid.api.main",
        service_url_env="STUDIO_GRID_SERVICE_URL",
    )


def _try_mount_sales_team(app: FastAPI) -> bool:
    """Mount AI Sales Team API."""
    return try_mount_or_proxy(
        app,
        "sales_team",
        "sales_team.api.main",
        service_url_env="SALES_TEAM_SERVICE_URL",
    )


def _try_mount_road_trip_planning(app: FastAPI) -> bool:
    """Mount Road Trip Planning team API."""
    return try_mount_or_proxy(
        app,
        "road_trip_planning",
        "road_trip_planning_team.api.main",
        service_url_env="ROAD_TRIP_PLANNING_SERVICE_URL",
    )


def _try_mount_agentic_team_provisioning(app: FastAPI) -> bool:
    """Mount Agentic Team Provisioning API."""
    return try_mount_or_proxy(
        app,
        "agentic_team_provisioning",
        "agentic_team_provisioning.api.main",
        service_url_env="AGENTIC_TEAM_PROVISIONING_SERVICE_URL",
    )


def _try_mount_startup_advisor(app: FastAPI) -> bool:
    """Mount Startup Advisor API."""
    return try_mount_or_proxy(
        app,
        "startup_advisor",
        "startup_advisor.api.main",
        service_url_env="STARTUP_ADVISOR_SERVICE_URL",
    )


def mount_all_teams(app: FastAPI) -> dict[str, bool]:
    """Mount all enabled team APIs and return mount status."""
    mount_functions = {
        "blogging": _try_mount_blogging,
        "software_engineering": _try_mount_software_engineering,
        "personal_assistant": _try_mount_personal_assistant,
        "market_research": _try_mount_market_research,
        "soc2_compliance": _try_mount_soc2_compliance,
        "social_marketing": _try_mount_social_marketing,
        "branding": _try_mount_branding,
        "agent_provisioning": _try_mount_agent_provisioning,
        "accessibility_audit": _try_mount_accessibility_audit,
        "ai_systems": _try_mount_ai_systems,
        "investment": _try_mount_investment,
        "nutrition_meal_planning": _try_mount_nutrition_meal_planning,
        "planning_v3": _try_mount_planning_v3,
        "coding_team": _try_mount_coding_team,
        "studio_grid": _try_mount_studio_grid,
        "sales_team": _try_mount_sales_team,
        "road_trip_planning": _try_mount_road_trip_planning,
        "agentic_team_provisioning": _try_mount_agentic_team_provisioning,
        "startup_advisor": _try_mount_startup_advisor,
    }

    results = {}
    enabled_teams = get_enabled_teams()

    for team_key, mount_fn in mount_functions.items():
        if team_key in enabled_teams:
            results[team_key] = mount_fn(app)
        else:
            results[team_key] = False
            logger.info("Team %s is disabled, skipping mount", team_key)

    return results


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _mounted_teams
    logger.info("Starting Unified API Server...")
    _mounted_teams = mount_all_teams(app)

    mounted_count = sum(1 for v in _mounted_teams.values() if v)
    total_count = len(get_enabled_teams())
    logger.info("Mounted %d/%d team APIs", mounted_count, total_count)

    # Start each team's Temporal worker when mounted and TEMPORAL_ADDRESS is set.
    # Map: team_key -> (module_path, start_function_name)
    _temporal_worker_starters: dict[str, tuple] = {
        "software_engineering": ("software_engineering_team.temporal.worker", "start_se_temporal_worker_thread"),
        "blogging": ("blogging.temporal.worker", "start_blogging_temporal_worker_thread"),
        "personal_assistant": ("personal_assistant_team.temporal.worker", "start_pa_temporal_worker_thread"),
        "ai_systems": ("ai_systems_team.temporal.worker", "start_ai_systems_temporal_worker_thread"),
        "planning_v3": ("planning_v3_team.temporal.worker", "start_planning_v3_temporal_worker_thread"),
        "agent_provisioning": (
            "agent_provisioning_team.temporal.worker",
            "start_agent_provisioning_temporal_worker_thread",
        ),
        "nutrition_meal_planning": (
            "nutrition_meal_planning_team.temporal.worker",
            "start_nutrition_temporal_worker_thread",
        ),
        "soc2_compliance": ("soc2_compliance_team.temporal.worker", "start_soc2_temporal_worker_thread"),
        "social_marketing": (
            "social_media_marketing_team.temporal.worker",
            "start_social_marketing_temporal_worker_thread",
        ),
    }
    for team_key, (mod_path, func_name) in _temporal_worker_starters.items():
        if not _mounted_teams.get(team_key):
            continue
        # Proxied teams run their own Temporal worker in their container.
        if _is_proxied(team_key):
            logger.info("Temporal worker for %s runs in its own container; skipping", team_key)
            continue
        try:
            mod = importlib.import_module(mod_path)
            start_fn = getattr(mod, func_name)
            if start_fn():
                logger.info("Temporal worker started for team %s", team_key)
        except Exception as e:
            logger.warning("Could not start Temporal worker for %s: %s", team_key, e)

    yield

    logger.info("Shutting down Unified API Server...")
    _run_shutdown_hooks("Server shutting down")


# Create the unified FastAPI application
app = FastAPI(
    title="Strands Agents Unified API",
    description="""
Unified API server providing access to all Strands Agent team capabilities.

## Available Teams

- **Blogging** (`/api/blogging`) - Research, planning, draft, copy-edit, publication
- **Software Engineering** (`/api/software-engineering`) - Full dev team simulation
- **Personal Assistant** (`/api/personal-assistant`) - Email, calendar, tasks, deals
- **Market Research** (`/api/market-research`) - User discovery, UX synthesis
- **SOC2 Compliance** (`/api/soc2-compliance`) - Compliance audit workflow
- **Social Marketing** (`/api/social-marketing`) - Campaign planning
- **Branding** (`/api/branding`) - Brand strategy and design
- **Agent Provisioning** (`/api/agent-provisioning`) - Environment provisioning
- **Accessibility Audit** (`/api/accessibility-audit`) - WCAG 2.2 and Section 508 auditing
- **AI Systems** (`/api/ai-systems`) - Spec-driven AI agent system factory
- **Investment** (`/api/investment`) - Investment analysis and portfolio management
- **Nutrition & Meal Planning** (`/api/nutrition-meal-planning`) - Personal nutrition and meal planning with learning from feedback
- **AI Sales Team** (`/api/sales`) - Full B2B sales pod: prospecting, outreach, qualification, nurturing, proposals, closing

Each team's endpoints are available under their respective prefix.
Visit the team-specific `/docs` endpoint for detailed API documentation
(e.g., `/api/blogging/docs`).
""",
    version="1.0.0",
    lifespan=lifespan,
)

atexit.register(lambda: _run_shutdown_hooks("Server stopped or crashed"))

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security gateway: scan requests to team APIs before forwarding (when SECURITY_GATEWAY_ENABLED is true)
from unified_api.middleware import SecurityGatewayMiddleware

app.add_middleware(SecurityGatewayMiddleware)

# Integrations API (Slack config)
from unified_api.routes.integrations import router as integrations_router

app.include_router(integrations_router)


@app.get("/", response_model=ApiInfoResponse, tags=["root"])
async def root() -> ApiInfoResponse:
    """Get unified API information and list of available teams."""
    teams = []
    for key, config in TEAM_CONFIGS.items():
        teams.append(
            TeamInfo(
                name=config.name,
                prefix=config.prefix,
                description=config.description,
                tags=config.tags,
                enabled=config.enabled and _mounted_teams.get(key, False),
            )
        )

    return ApiInfoResponse(
        name="Strands Agents Unified API",
        version="1.0.0",
        description="Single entry point for all Strands Agent team APIs",
        teams=teams,
        docs_url="/docs",
    )


@app.get("/health", response_model=UnifiedHealthResponse, tags=["health"])
async def health() -> UnifiedHealthResponse:
    """
    Unified health check for all mounted team APIs.

    Returns the overall status and individual team statuses.
    """
    teams = []
    all_healthy = True

    for key, config in TEAM_CONFIGS.items():
        mounted = _mounted_teams.get(key, False)
        status = "healthy" if mounted else "unavailable"
        if config.enabled and not mounted:
            all_healthy = False

        teams.append(
            TeamHealth(
                name=config.name,
                prefix=config.prefix,
                status=status,
                enabled=config.enabled,
            )
        )

    return UnifiedHealthResponse(
        status="healthy" if all_healthy else "degraded",
        version="1.0.0",
        teams=teams,
    )


@app.get("/teams", tags=["root"])
async def list_teams() -> dict[str, Any]:
    """List all available teams with their mount status."""
    teams = {}
    for key, config in TEAM_CONFIGS.items():
        mounted = _mounted_teams.get(key, False)
        teams[key] = {
            "name": config.name,
            "prefix": config.prefix,
            "description": config.description,
            "mounted": mounted,
            "enabled": config.enabled,
            "docs_url": f"{config.prefix}/docs" if mounted else None,
        }
    return {"teams": teams}
