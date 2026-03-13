"""
Unified API Server - Consolidates all Strands Agent team APIs.

This server mounts all agent team APIs under namespaced prefixes,
providing a single entry point for the entire platform.

Route Prefixes:
- /api/blogging          - Blog research, review, draft, publication
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
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Add agents directory to path for imports
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
    teams: List[TeamHealth]


class TeamInfo(BaseModel):
    """Information about a mounted team API."""

    name: str
    prefix: str
    description: str
    tags: List[str]
    enabled: bool


class ApiInfoResponse(BaseModel):
    """API information response."""

    name: str
    version: str
    description: str
    teams: List[TeamInfo]
    docs_url: str


class SecurityErrorResponse(BaseModel):
    """Response body when the security gateway rejects a request (403)."""

    detail: str
    security_findings: List[str]


# Track mounted routers for health checks
_mounted_teams: Dict[str, bool] = {}

# Team keys that have async jobs: on shutdown, call their mark_all_running_jobs_failed(reason).
# Maps team_key -> (module_dot_path, function_name).
SHUTDOWN_HOOKS: Dict[str, tuple] = {
    "blogging": ("blogging.shared.blog_job_store", "mark_all_running_jobs_failed"),
    "software_engineering": ("software_engineering_team.shared.job_store", "mark_all_running_jobs_failed"),
    "personal_assistant": ("personal_assistant_team.shared.pa_job_store", "mark_all_running_jobs_failed"),
    "agent_provisioning": ("agent_provisioning_team.shared.job_store", "mark_all_running_jobs_failed"),
    "ai_systems": ("ai_systems_team.shared.job_store", "mark_all_running_jobs_failed"),
    "soc2_compliance": ("soc2_compliance_team.api.main", "mark_all_running_jobs_failed"),
    "social_marketing": ("social_media_marketing_team.api.main", "mark_all_running_jobs_failed"),
    "accessibility_audit": ("accessibility_audit_team.api.main", "mark_all_running_jobs_failed"),
    "nutrition_meal_planning": ("nutrition_meal_planning_team.shared.job_store", "mark_all_running_jobs_failed"),
    "planning_v3": ("planning_v3_team.shared.job_store", "mark_all_running_jobs_failed"),
}


def _run_shutdown_hooks(reason: str) -> None:
    """Call each mounted team's mark_all_running_jobs_failed(reason). Used by lifespan and atexit."""
    for team_key, (module_path, func_name) in SHUTDOWN_HOOKS.items():
        if not _mounted_teams.get(team_key):
            continue
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            fn(reason)
        except Exception as e:
            logger.warning("Shutdown hook for team %s failed: %s", team_key, e)


def _try_mount_blogging(app: FastAPI) -> bool:
    """Mount blogging team API."""
    try:
        from blogging.api.main import app as blogging_app

        config = TEAM_CONFIGS["blogging"]
        app.mount(config.prefix, blogging_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Blogging API: %s", e)
        return False


def _try_mount_software_engineering(app: FastAPI) -> bool:
    """Mount software engineering team API.

    The team uses 'software_engineering_team.shared' (not 'shared') so it
    does not conflict with blogging's shared package when both are loaded.
    """
    try:
        from software_engineering_team.api.main import app as se_app

        config = TEAM_CONFIGS["software_engineering"]
        app.mount(config.prefix, se_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Software Engineering API: %s", e)
        return False


def _try_mount_personal_assistant(app: FastAPI) -> bool:
    """Mount personal assistant team API."""
    try:
        from personal_assistant_team.api.main import app as pa_app

        config = TEAM_CONFIGS["personal_assistant"]
        app.mount(config.prefix, pa_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Personal Assistant API: %s", e)
        return False


def _try_mount_market_research(app: FastAPI) -> bool:
    """Mount market research team API."""
    try:
        from market_research_team.api.main import app as mr_app

        config = TEAM_CONFIGS["market_research"]
        app.mount(config.prefix, mr_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Market Research API: %s", e)
        return False


def _try_mount_soc2_compliance(app: FastAPI) -> bool:
    """Mount SOC2 compliance team API."""
    try:
        from soc2_compliance_team.api.main import app as soc2_app

        config = TEAM_CONFIGS["soc2_compliance"]
        app.mount(config.prefix, soc2_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount SOC2 Compliance API: %s", e)
        return False


def _try_mount_social_marketing(app: FastAPI) -> bool:
    """Mount social media marketing team API."""
    try:
        from social_media_marketing_team.api.main import app as smm_app

        config = TEAM_CONFIGS["social_marketing"]
        app.mount(config.prefix, smm_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Social Marketing API: %s", e)
        return False


def _try_mount_branding(app: FastAPI) -> bool:
    """Mount branding team API."""
    try:
        from branding_team.api.main import app as branding_app

        config = TEAM_CONFIGS["branding"]
        app.mount(config.prefix, branding_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Branding API: %s", e)
        return False


def _try_mount_agent_provisioning(app: FastAPI) -> bool:
    """Mount agent provisioning team API."""
    try:
        from agent_provisioning_team.api.main import app as ap_app

        config = TEAM_CONFIGS["agent_provisioning"]
        app.mount(config.prefix, ap_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Agent Provisioning API: %s", e)
        return False


def _try_mount_accessibility_audit(app: FastAPI) -> bool:
    """Mount accessibility audit team API."""
    try:
        from accessibility_audit_team.api.main import router as a11y_router
        from fastapi import FastAPI as SubApp

        # Create sub-app for accessibility audit
        a11y_app = SubApp(
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
    try:
        from ai_systems_team.api.main import app as ai_systems_app

        config = TEAM_CONFIGS["ai_systems"]
        app.mount(config.prefix, ai_systems_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount AI Systems API: %s", e)
        return False


def _try_mount_investment(app: FastAPI) -> bool:
    """Mount investment team API."""
    try:
        from investment_team.api.main import app as investment_app

        config = TEAM_CONFIGS["investment"]
        app.mount(config.prefix, investment_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Investment API: %s", e)
        return False


def _try_mount_nutrition_meal_planning(app: FastAPI) -> bool:
    """Mount nutrition & meal planning team API."""
    try:
        from nutrition_meal_planning_team.api.main import app as nmp_app

        config = TEAM_CONFIGS["nutrition_meal_planning"]
        app.mount(config.prefix, nmp_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Nutrition & Meal Planning API: %s", e)
        return False


def _try_mount_planning_v3(app: FastAPI) -> bool:
    """Mount Planning V3 team API."""
    try:
        from planning_v3_team.api.main import app as planning_v3_app

        config = TEAM_CONFIGS["planning_v3"]
        app.mount(config.prefix, planning_v3_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Planning V3 API: %s", e)
        return False


def _try_mount_coding_team(app: FastAPI) -> bool:
    """Mount Coding Team API."""
    try:
        from coding_team.api.main import app as coding_team_app

        config = TEAM_CONFIGS["coding_team"]
        app.mount(config.prefix, coding_team_app)
        logger.info("Mounted %s at %s", config.name, config.prefix)
        return True
    except ImportError as e:
        logger.warning("Could not mount Coding Team API: %s", e)
        return False


def mount_all_teams(app: FastAPI) -> Dict[str, bool]:
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
    _temporal_worker_starters: Dict[str, tuple] = {
        "software_engineering": ("software_engineering_team.temporal.worker", "start_se_temporal_worker_thread"),
        "blogging": ("blogging.temporal.worker", "start_blogging_temporal_worker_thread"),
        "personal_assistant": ("personal_assistant_team.temporal.worker", "start_pa_temporal_worker_thread"),
        "ai_systems": ("ai_systems_team.temporal.worker", "start_ai_systems_temporal_worker_thread"),
        "planning_v3": ("planning_v3_team.temporal.worker", "start_planning_v3_temporal_worker_thread"),
        "agent_provisioning": ("agent_provisioning_team.temporal.worker", "start_agent_provisioning_temporal_worker_thread"),
        "nutrition_meal_planning": ("nutrition_meal_planning_team.temporal.worker", "start_nutrition_temporal_worker_thread"),
    }
    for team_key, (mod_path, func_name) in _temporal_worker_starters.items():
        if not _mounted_teams.get(team_key):
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

- **Blogging** (`/api/blogging`) - Research, review, draft, copy-edit, publication
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
async def list_teams() -> Dict[str, Any]:
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
