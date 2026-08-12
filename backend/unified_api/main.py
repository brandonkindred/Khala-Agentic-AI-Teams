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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add agents directory to path (needed for team_assistant, integrations, etc.)
_project_root = Path(__file__).resolve().parent.parent
_agents_dir = _project_root / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from shared.env_config import env_float
from unified_api.bounded_executor import get_or_recreate_executor
from unified_api.config import (
    TEAM_CONFIGS,
    UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER,
    UNIFIED_API_SANDBOX_TEMPORAL_WORKER,
    UNIFIED_API_TEAM_ASSISTANTS_ENABLED,
    get_enabled_teams,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("unified_api")

# Initialize OpenTelemetry providers as early as possible so every module
# imported below — including the team proxy, security gateway, and any
# assistant sub-apps — uses the real tracer/meter providers.
try:
    from shared.observability import init_otel

    init_otel(service_name="unified-api", team_key="unified_api")
except Exception:
    logger.warning("shared.observability init_otel failed", exc_info=True)


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
    "planning": "PLANNING_SERVICE_URL",
    "coding_team": "CODING_TEAM_SERVICE_URL",
    "sales_team": "SALES_TEAM_SERVICE_URL",
    "road_trip_planning": "ROAD_TRIP_PLANNING_SERVICE_URL",
    "agentic_team_provisioning": "AGENTIC_TEAM_PROVISIONING_SERVICE_URL",
    "startup_advisor": "STARTUP_ADVISOR_SERVICE_URL",
    "user_agent_founder": "USER_AGENT_FOUNDER_SERVICE_URL",
    "deepthought": "DEEPTHOUGHT_SERVICE_URL",
    "job_matching": "JOB_MATCHING_SERVICE_URL",
}


@dataclass(frozen=True)
class AssistantMountSpec:
    """Everything needed to mount one team's assistant sub-app, deferred.

    Built by ``_build_assistant_registry`` without ever constructing the
    FastAPI sub-app or calling ``app.mount`` — that step (``mount_assistant_app``)
    is invoked later, on demand, once the request-path lazy-mount hook exists.
    """

    team_key: str
    mount_path: str
    assistant_config: Any


# Registry of assistant mount specs, keyed by team_key. Populated at startup
# by _maybe_register_team_assistants (registration only — no sub-app is
# constructed or mounted here); consumed on demand by _ensure_assistant_mounted.
_ASSISTANT_REGISTRY: dict[str, AssistantMountSpec] = {}

# team_keys whose assistant sub-app has actually been constructed and mounted
# (the idempotency marker consulted by _ensure_assistant_mounted).
_MOUNTED_ASSISTANTS: set[str] = set()

# One asyncio.Lock per team_key, created lazily. Concurrent first-requests for
# different teams don't serialize against each other; concurrent first-requests
# for the SAME team queue behind one mount.
_ASSISTANT_MOUNT_LOCKS: dict[str, asyncio.Lock] = {}

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

# Per-team schema retry budget inside `_health_check_loop`. Codex
# flagged that an unbounded `await loop.run_in_executor(...)` could let
# one stalled DDL attempt block every other team's liveness update —
# exactly during the outages where timely health refresh matters most.
# The timeout cancels the *await*; the worker thread is independently
# bounded by the pool-connection timeout, so it always cleans up.
_SCHEMA_RETRY_TIMEOUT_S = float(os.getenv("SCHEMA_RETRY_TIMEOUT_S", "10"))


async def _check_team_health(team_key: str, service_url: str) -> str:
    """Probe a team's /health endpoint. Returns 'healthy' or 'unhealthy'."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{service_url.rstrip('/')}/health")
            return "healthy" if resp.status_code == 200 else "unhealthy"
    except Exception:
        return "unhealthy"


async def _start_sandbox_reaper_with_retry() -> None:
    """Start the durable ``SandboxReaperWorkflow``, retrying with backoff.

    ``start_sandbox_reaper_workflow`` can fail transiently right at boot (the
    Agent Provisioning Temporal worker's daemon thread may still be connecting
    its client). Run as a background task rather than a single blocking
    lifespan step, so a lost race doesn't leave the reaper permanently unstarted
    and doesn't serialize app startup behind the Temporal client becoming ready.

    Preconditions:
        * Called only when ``sandbox_temporal_enabled()`` is true.
    Postconditions:
        * Returns once the reaper workflow is confirmed started (or already
          running). Retries indefinitely with exponential backoff (capped at
          60s) on any other failure; propagates ``asyncio.CancelledError``
          untouched so app shutdown can cancel this task cleanly.

    Passes a short ``client_ready_timeout_s`` to ``start_sandbox_reaper_workflow``
    so its internal client-readiness poll (default 10s,
    ``shared.temporal.runner.CLIENT_READY_TIMEOUT_S``) doesn't stack underneath
    this loop's own backoff — this loop already retries the whole call, so it
    should own all the waiting; each attempt should fail fast if the client
    isn't ready *yet* rather than block for up to 10s before this loop's own
    delay even applies.
    """
    from agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch import start_sandbox_reaper_workflow

    delay = 2.0
    while True:
        try:
            # start_workflow_sync blocks briefly on client-ready; keep it off the loop.
            await asyncio.to_thread(start_sandbox_reaper_workflow, client_ready_timeout_s=1.0)
            logger.info("Started Agent Console sandbox idle reaper (Temporal workflow)")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("SandboxReaperWorkflow failed to start; retrying in %.0fs", delay, exc_info=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def _start_sandbox_reaper_task() -> asyncio.Task:
    """Start the Agent Console sandbox idle reaper, in whichever mode is active.

    Extracted from the lifespan body so this branch (and specifically the
    Temporal-mode sandbox worker boot below) has its own directly-testable
    seam, mirroring ``_start_sandbox_reaper_with_retry``'s own extraction.

    Preconditions:
        * None.
    Postconditions:
        * Returns the created background ``asyncio.Task``. When Temporal is
          enabled, this process's own sandbox-only Temporal worker
          (``start_agent_provisioning_sandbox_temporal_worker_thread``) is
          started FIRST, before the reaper workflow — sandbox
          workflows/activities run on their own ``SANDBOX_TASK_QUEUE`` (never
          the shared ``TASK_QUEUE`` the standalone agent-provisioning-service
          team container also polls), so a sandbox activity can never execute
          in that other process against a different, process-local
          ``Lifecycle`` singleton than the one this API's
          status/list/metrics/note_activity routes read. Otherwise, falls
          back to the in-process ``run_idle_reaper()`` asyncio task
          (thread mode).
    """
    from agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch import sandbox_temporal_enabled

    if sandbox_temporal_enabled():
        from agent_team_studio.agent_provisioning_team.temporal.worker import (
            start_agent_provisioning_sandbox_temporal_worker_thread,
        )

        start_agent_provisioning_sandbox_temporal_worker_thread()
        logger.info("Starting Agent Console sandbox idle reaper (Temporal workflow)")
        return asyncio.create_task(_start_sandbox_reaper_with_retry())

    from agent_team_studio.agent_provisioning_team.sandbox import run_idle_reaper

    logger.info("Started Agent Console sandbox idle reaper (in-process)")
    return asyncio.create_task(run_idle_reaper())


async def _maybe_start_sandbox_reaper() -> asyncio.Task | None:
    """Start the Agent Console sandbox idle reaper, unless disabled via
    UNIFIED_API_SANDBOX_TEMPORAL_WORKER.

    Preconditions:
        * None.
    Postconditions:
        * Returns the background asyncio.Task when
          UNIFIED_API_SANDBOX_TEMPORAL_WORKER is true (default) and startup
          succeeds.
        * Returns None when the flag is false, or when
          _start_sandbox_reaper_task raises (logged as a warning; startup is
          not aborted, matching every other lifespan startup step).
    """
    if not UNIFIED_API_SANDBOX_TEMPORAL_WORKER:
        logger.info("Agent Console sandbox reaper disabled (UNIFIED_API_SANDBOX_TEMPORAL_WORKER=false)")
        return None
    try:
        return await _start_sandbox_reaper_task()
    except Exception:
        logger.warning("Agent Console sandbox reaper failed to start", exc_info=True)
        return None


def _build_assistant_registry() -> dict[str, AssistantMountSpec]:
    """Build assistant mount specs for every configured team, without
    constructing or mounting any sub-app.

    Preconditions:
        * None.
    Postconditions:
        * Returns one AssistantMountSpec per team_key present in both
          TEAM_ASSISTANT_CONFIGS and TEAM_CONFIGS. No FastAPI sub-app is
          created and app.mount is never called — this is registration
          only; a future first-request hook (mount_assistant_app) does the
          actual mount.
    """
    from team_assistant.config import TEAM_ASSISTANT_CONFIGS

    registry: dict[str, AssistantMountSpec] = {}
    for team_key, assistant_config in TEAM_ASSISTANT_CONFIGS.items():
        team_cfg = TEAM_CONFIGS.get(team_key)
        if team_cfg:
            registry[team_key] = AssistantMountSpec(
                team_key=team_key,
                mount_path=f"{team_cfg.prefix}/assistant",
                assistant_config=assistant_config,
            )
    return registry


def mount_assistant_app(app: FastAPI, spec: AssistantMountSpec) -> None:
    """Construct and mount one team's assistant sub-app from its registry spec.

    Preconditions:
        * spec came from _ASSISTANT_REGISTRY (or _build_assistant_registry).
    Postconditions:
        * The assistant sub-app for spec.team_key is mounted at
          spec.mount_path with the standard permissive CORS middleware.

    Called by _ensure_assistant_mounted, the first-request lazy-mount hook —
    never call this directly, it does not check or update _MOUNTED_ASSISTANTS
    and is not safe to call twice for the same team_key.
    """
    from team_assistant.api import create_assistant_app

    assistant_app = create_assistant_app(spec.assistant_config)
    assistant_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount(spec.mount_path, assistant_app)


async def _ensure_assistant_mounted(team_key: str) -> bool:
    """Idempotently, thread/async-safely mount team_key's assistant sub-app on
    its first request.

    Preconditions:
        * None.
    Postconditions:
        * Returns True if the team's assistant sub-app is mounted (whether by
          this call or an earlier one). Returns False if team_key has no
          registry entry (assistants disabled, or no assistant configured for
          that team), or if mounting raised — the failure is logged and
          swallowed so the request path never 500s on this, and the NEXT
          request for team_key retries the mount (self-healing; team_key is
          only added to _MOUNTED_ASSISTANTS on success).
        * On first successful mount, the newly-added route is moved to the
          front of app.routes so it takes priority over that team's
          already-registered proxy catch-all route (`{prefix}/{path:path}`)
          — Starlette matches app.routes in list order and the catch-all,
          registered at lifespan startup, would otherwise always shadow a
          Mount appended later. The reorder is scoped to this one team's own
          anchored Mount pattern and cannot affect unrelated routes.

    No `await` occurs between the mount and the reorder (mount_assistant_app
    and everything it calls are synchronous), so this critical section runs
    atomically w.r.t. the event loop even across different teams' locks.
    """
    if team_key in _MOUNTED_ASSISTANTS:
        return True
    spec = _ASSISTANT_REGISTRY.get(team_key)
    if spec is None:
        return False
    lock = _ASSISTANT_MOUNT_LOCKS.setdefault(team_key, asyncio.Lock())
    async with lock:
        if team_key in _MOUNTED_ASSISTANTS:
            return True
        try:
            mount_assistant_app(app, spec)
            mounted_route = app.routes[-1]
            app.routes.remove(mounted_route)
            app.routes.insert(0, mounted_route)
        except Exception:
            logger.warning("Could not mount assistant sub-app for %s", team_key, exc_info=True)
            return False
        _MOUNTED_ASSISTANTS.add(team_key)
        logger.info("Mounted assistant sub-app for %s at %s", team_key, spec.mount_path)
        return True


def _match_unmounted_assistant_prefix(path: str) -> str | None:
    """Return the team_key whose assistant mount_path is a prefix of path and
    is not yet mounted, or None.

    Preconditions:
        * None.
    Postconditions:
        * Boundary-safe: "/api/blogging/assistant-x" does not match the
          "/api/blogging/assistant" mount_path (plain str.startswith would
          false-match here). Exact-equal and "/"-separated child paths match.
    """
    for team_key, spec in _ASSISTANT_REGISTRY.items():
        if team_key in _MOUNTED_ASSISTANTS:
            continue
        if path == spec.mount_path or path.startswith(spec.mount_path + "/"):
            return team_key
    return None


class AssistantLazyMountMiddleware:
    """ASGI middleware: mounts a team's assistant sub-app on its first request.

    Defined here (not unified_api/middleware/) because it needs the real
    FastAPI `app` singleton to mount into — the `app` an ASGI middleware
    receives via __init__ is just the next inner ASGI layer, never the
    FastAPI instance itself — and because it is tightly coupled to this
    module's own registry/state (_ASSISTANT_REGISTRY, _MOUNTED_ASSISTANTS).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            team_key = _match_unmounted_assistant_prefix(scope.get("path") or "")
            if team_key is not None:
                await _ensure_assistant_mounted(team_key)
        await self.app(scope, receive, send)


def _maybe_register_team_assistants() -> int:
    """Register team assistant mount specs unless disabled via
    UNIFIED_API_TEAM_ASSISTANTS_ENABLED.

    Preconditions:
        * None.
    Postconditions:
        * Populates _ASSISTANT_REGISTRY (cleared first) and returns its
          size: 0 when the flag is false, or when _build_assistant_registry
          raises (logged as a warning; startup is not aborted, matching
          every other lifespan startup step). No sub-app is constructed or
          mounted — that happens later, on demand.
    """
    _ASSISTANT_REGISTRY.clear()
    if not UNIFIED_API_TEAM_ASSISTANTS_ENABLED:
        logger.info("Team assistant registration disabled (UNIFIED_API_TEAM_ASSISTANTS_ENABLED=false)")
        return 0
    try:
        _ASSISTANT_REGISTRY.update(_build_assistant_registry())
        logger.info("Registered %d team assistant mount specs (not yet mounted)", len(_ASSISTANT_REGISTRY))
        return len(_ASSISTANT_REGISTRY)
    except Exception:
        logger.warning("Could not register team assistant mount specs", exc_info=True)
        return 0


async def _health_check_loop() -> None:  # pragma: no cover - infinite bg loop, live lifespan only
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
                    # Bound each retry with `wait_for`. Codex flagged that
                    # awaiting the executor directly meant a single stalled
                    # DDL attempt (lock contention, slow connection
                    # acquisition during the same outage that caused the
                    # initial failure) could stall the loop and stale every
                    # other team's liveness update — exactly the moment
                    # operators need timely health refresh. The timeout
                    # cancels the await; the worker thread itself is bounded
                    # by the inner pool-connection timeout so it cleans up.
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(
                                _get_probe_executor(),
                                _retry_in_process_schema_registration,
                                team_key,
                            ),
                            timeout=_SCHEMA_RETRY_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Background schema retry for %s exceeded %.1fs; "
                            "leaving team in failure set for next loop pass",
                            team_key,
                            _SCHEMA_RETRY_TIMEOUT_S,
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


def _start_agent_studio_temporal_worker() -> None:
    """Start the in-process Agent Studio Temporal worker.

    Agent Studio is an in-process team (mounted on this app, not a separate
    ``team_service`` container), so its worker runs here and its activity threads
    share this process's :class:`AgentStudioService` singleton. Gated on
    ``UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER`` and on the team being enabled.
    Authoring CRUD (start conversation / send message / clone / save) no longer
    requires this worker: ``agent_team_studio.agent_studio.temporal.dispatch``
    falls back to calling :class:`AgentStudioService` directly, in-process, when
    Temporal isn't configured — so a missing ``TEMPORAL_ADDRESS`` here is a
    (fully-functional) mode switch, not a degraded state. The worker is a daemon
    thread (no shutdown handle needed); log-and-continue on failure, matching
    the other lifespan startup steps.

    Postconditions:
        - Logs at INFO and returns without starting a worker when
          ``UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER`` is false.
        - Logs at INFO when a worker actually started, or when ``start_team_worker``
          returns ``False`` (``TEMPORAL_ADDRESS`` unset → no worker) — Agent Studio
          serves authoring requests via direct in-process dispatch instead. Startup is
          not aborted either way (that would take down every other team for one
          in-process team's config).
    """
    if not UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER:
        logger.info("Agent Studio Temporal worker disabled (UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER=false)")
        return
    if not TEAM_CONFIGS["agent_studio"].enabled:
        return
    try:
        from agent_team_studio.agent_studio.temporal.worker import start_agent_studio_temporal_worker_thread

        started = start_agent_studio_temporal_worker_thread()
    except Exception:
        logger.warning("Agent Studio Temporal worker failed to start", exc_info=True)
        return
    if started:
        logger.info("Started Agent Studio Temporal worker")
    else:
        logger.info(
            "Agent Studio Temporal worker NOT started (TEMPORAL_ADDRESS unset); "
            "authoring requests will dispatch directly in-process instead."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: PLR0915 - linear startup orchestrator; each numbered step is one registration/boot  # pragma: no cover - startup requires live Postgres schema registration, Temporal worker boot, and sub-app mounting
    """Application lifespan: register own Postgres schemas, register assistant
    mount specs (no sub-apps mounted yet), then register proxy routes.
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
        from shared.postgres import register_team_schemas
        from unified_api.postgres import SCHEMA as UNIFIED_API_SCHEMA

        register_team_schemas(UNIFIED_API_SCHEMA)
    except Exception:
        logger.exception("unified_api postgres schema registration failed")

    try:
        from llm_service.usage_flusher import register_usage_flusher

        register_usage_flusher()
    except Exception:
        logger.warning("llm usage flusher registration failed", exc_info=True)

    try:
        from shared.postgres import register_team_schemas
        from team_assistant.postgres import SCHEMA as TEAM_ASSISTANT_SCHEMA

        register_team_schemas(TEAM_ASSISTANT_SCHEMA)
    except Exception:
        logger.exception("team_assistant postgres schema registration failed")

    try:
        from agent_console.postgres import SCHEMA as AGENT_CONSOLE_SCHEMA
        from shared.postgres import register_team_schemas

        register_team_schemas(AGENT_CONSOLE_SCHEMA)
    except Exception:
        logger.exception("agent_console postgres schema registration failed")

    try:
        from agent_registry.postgres import SCHEMA as AGENT_REGISTRY_SCHEMA
        from shared.postgres import register_team_schemas

        register_team_schemas(AGENT_REGISTRY_SCHEMA)
    except Exception:
        logger.exception("agent_registry postgres schema registration failed")

    # Gate on the team's `enabled` flag, same rationale as product_delivery
    # below: disabling agent_studio must also disable its startup side
    # effects (schema DDL, failure logs), not just leave the schema import
    # to run unconditionally regardless of config.
    if TEAM_CONFIGS["agent_studio"].enabled:
        try:
            from agent_team_studio.agent_studio.postgres import SCHEMA as AGENT_STUDIO_SCHEMA
            from shared.postgres import register_team_schemas

            register_team_schemas(AGENT_STUDIO_SCHEMA)
        except Exception:
            logger.exception("agent_studio postgres schema registration failed")

    # Gate on the team's `enabled` flag (same rationale as agent_studio above).
    if TEAM_CONFIGS["user_profile"].enabled:
        try:
            from shared.postgres import register_team_schemas
            from user_profile.postgres import SCHEMA as USER_PROFILE_SCHEMA

            register_team_schemas(USER_PROFILE_SCHEMA)
        except Exception:
            logger.exception("user_profile postgres schema registration failed")

    try:
        from agent_cognition.postgres import SCHEMA as AGENT_COGNITION_SCHEMA
        from shared.postgres import register_team_schemas

        register_team_schemas(AGENT_COGNITION_SCHEMA)
    except Exception:
        logger.exception("agent_cognition postgres schema registration failed")

    # Gate the entire product_delivery startup block on the team's
    # `enabled` flag. Disabling the team must also disable its startup
    # side effects (schema DDL, failure logs, health markers) — not
    # just the routes.
    if TEAM_CONFIGS["product_delivery"].enabled:
        try:
            from product_delivery.postgres import SCHEMA as PRODUCT_DELIVERY_SCHEMA
            from shared.postgres import ensure_team_schema, is_postgres_enabled

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

    # 1. Register team assistant mount specs (no sub-apps constructed or
    #    mounted yet — see _ASSISTANT_REGISTRY), unless disabled via
    #    UNIFIED_API_TEAM_ASSISTANTS_ENABLED.
    _maybe_register_team_assistants()

    # 2. Register proxy routes for all team containers.
    _registered_teams = _register_proxy_routes(app)
    ok = sum(1 for v in _registered_teams.values() if v)
    total = len(get_enabled_teams())
    logger.info("Registered %d/%d team proxy routes", ok, total)

    # 3. Start background health checker for upstream team liveness.
    health_task = asyncio.create_task(_health_check_loop())
    logger.info("Started background health checker (interval=%ds)", _HEALTH_CHECK_INTERVAL)

    # 4. Start the Agent Console sandbox idle reaper, unless disabled via
    #    UNIFIED_API_SANDBOX_TEMPORAL_WORKER. When Temporal is enabled it runs
    #    as a durable, single-instance SandboxReaperWorkflow served by this
    #    process's own sandbox-only Temporal worker (survives restarts);
    #    otherwise it's an in-process asyncio task (thread mode). The Temporal
    #    branch is a background retry loop, not a single blocking attempt: the
    #    worker's client can legitimately still be connecting at this point
    #    (see shared.temporal.runner._await_client), and a lost race here must
    #    not mean the reaper never starts for the life of the process. See
    #    _start_sandbox_reaper_task's docstring for why the sandbox worker must
    #    be booted here rather than shared with this team's general worker.
    sandbox_reaper_task = await _maybe_start_sandbox_reaper()

    # 5. Start the Agent Console run pruner (Phase 3).
    run_pruner_task: asyncio.Task | None = None
    try:
        from agent_console.prune import run_pruner

        run_pruner_task = asyncio.create_task(run_pruner())
        logger.info("Started Agent Console run pruner")
    except Exception:
        logger.warning("Agent Console run pruner failed to start", exc_info=True)

    # 6. Start the Agent Cognition knowledge-graph sync worker, gated on
    #    NEO4J_BOLT_URL so the module (and the graphiti_core import chain it
    #    eventually reaches) is never pulled into sys.modules when the
    #    knowledge-graph layer is unused. Once started, the worker also
    #    self-disables (returns without looping) when POSTGRES_HOST is unset.
    from shared.neo4j import is_neo4j_enabled

    graph_sync_task: asyncio.Task | None = None
    if is_neo4j_enabled():
        try:
            from agent_cognition.graph.sync_worker import run_graph_sync

            graph_sync_task = asyncio.create_task(run_graph_sync())
            logger.info("Started Agent Cognition graph sync worker")
        except Exception:
            logger.warning("Agent Cognition graph sync worker failed to start", exc_info=True)

    # 7. Start the Agent Cognition scheduler (rollups → reflection → pruning). It
    #    self-disables when POSTGRES_HOST is unset and never activates a rule.
    cognition_scheduler_task: asyncio.Task | None = None
    try:
        from agent_cognition.scheduler import run_cognition_scheduler

        cognition_scheduler_task = asyncio.create_task(run_cognition_scheduler())
        logger.info("Started Agent Cognition scheduler")
    except Exception:
        logger.warning("Agent Cognition scheduler failed to start", exc_info=True)

    # 8. Start the Agent Studio Temporal worker (in-process team; see helper).
    _start_agent_studio_temporal_worker()

    yield

    if cognition_scheduler_task is not None:
        cognition_scheduler_task.cancel()
    if graph_sync_task is not None:
        graph_sync_task.cancel()
    if run_pruner_task is not None:
        run_pruner_task.cancel()
    if sandbox_reaper_task is not None:
        sandbox_reaper_task.cancel()
    health_task.cancel()

    # Close the Graphiti client (and its Neo4j driver) owned by shared.neo4j.
    try:
        from shared.neo4j import close_graphiti

        await close_graphiti()
    except Exception:
        logger.warning("shared.neo4j close_graphiti failed", exc_info=True)

    try:
        from llm_service.usage_flusher import shutdown as usage_flush_shutdown

        usage_flush_shutdown()
    except Exception:
        logger.warning("llm usage flusher shutdown failed", exc_info=True)

    # Close Postgres connection pools owned by shared.postgres.
    try:
        from shared.postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("shared.postgres close_pool failed", exc_info=True)

    # Shut down the dedicated health-probe executor so worker threads
    # don't outlive the app. The shutdown helper also clears the
    # module-level slot so the next lifespan startup (under reload /
    # test harnesses that recreate app state in-process) sees a fresh
    # executor on first probe via `_get_probe_executor()`. Codex
    # flagged that the previous "shutdown but never recreate" pattern
    # silently broke probe + schema retry on subsequent app starts.
    _shutdown_probe_executor()

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

# Lazy-mount team assistant sub-apps on first request. Registered here (after
# CORS, before Security) so that — since Starlette's add_middleware makes the
# LAST-added middleware outermost/first-to-run — Security still runs before
# any mount attempt (a request Security 403s never triggers a wasted mount),
# while this still runs before the router resolves the route.
app.add_middleware(AssistantLazyMountMiddleware)

# Security gateway
from unified_api.middleware import SecurityGatewayMiddleware

app.add_middleware(SecurityGatewayMiddleware)

# OpenTelemetry FastAPI instrumentation — server spans for every request,
# trace IDs injected into logs, and outbound httpx calls nested under the
# request span automatically.
try:
    from shared.observability import instrument_fastapi_app

    # Anchored exclusions: this app hosts the /api/se/metrics business alias, whose
    # path contains "metrics". Excluding only the exact scrape/health endpoints keeps
    # the alias traced while still skipping the Prometheus /metrics endpoint.
    instrument_fastapi_app(
        app,
        team_key="unified_api",
        excluded_urls="^/health$,^/healthz$,^/ready$,^/metrics$",
    )
except Exception:
    logger.warning("OpenTelemetry FastAPI instrumentation unavailable", exc_info=True)

# Prometheus metrics — exposes GET /metrics for scraping. SecurityGatewayMiddleware
# only intercepts /api/{team}/* paths, so /metrics bypasses it automatically.
try:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        # Anchored so the /api/se/metrics alias is scraped while the scrape endpoint isn't.
        excluded_handlers=["^/metrics$", "^/health$"],
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
from unified_api.routes.cognition import router as cognition_router
from unified_api.routes.integrations import router as integrations_router
from unified_api.routes.llm_config import router as llm_config_router
from unified_api.routes.llm_tools import router as llm_tools_router
from unified_api.routes.llm_usage import router as llm_usage_router
from unified_api.routes.sandboxes import router as sandboxes_router

app.include_router(integrations_router)
app.include_router(llm_config_router)
app.include_router(llm_tools_router)
app.include_router(llm_usage_router)
app.include_router(analytics_router)
app.include_router(agents_router)
app.include_router(sandboxes_router)
app.include_router(agent_console_saved_inputs_router)
app.include_router(agent_console_diff_router)
app.include_router(cognition_router)
# Honor the in-process team's `enabled` flag and gate the import too (same
# rationale as product_delivery below): disabling the team via TEAM_CONFIGS
# must make /api/user-profile/* stop answering AND keep user_profile out of
# the module graph, not just disappear from /teams.
if TEAM_CONFIGS["user_profile"].enabled:
    from unified_api.routes.user_profile import router as user_profile_router

    app.include_router(user_profile_router)
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

# Honor the in-process team's `enabled` flag and gate the import (same
# rationale as product_delivery above): disabling agent_studio in config
# must make /api/agent-studio/* stop answering. The import is also wrapped
# so an import-time failure (missing transitive dep, broken module) logs
# and skips mounting agent_studio rather than taking down the whole
# unified API at startup.
if TEAM_CONFIGS["agent_studio"].enabled:
    try:
        from unified_api.routes.agent_studio import router as agent_studio_router
    except Exception:  # pragma: no cover - defensive import-failure guard
        logger.warning("Failed to import agent_studio routes; skipping mount", exc_info=True)
    else:
        app.include_router(agent_studio_router)


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
_PROBE_DB_TIMEOUT_S = 1.5
# The probe executor is created lazily via `_get_probe_executor()` so
# that lifespan teardown (which calls `.shutdown()`) doesn't strand a
# subsequent app start with a dead executor — Codex flagged that
# tests / reload harnesses that recreate app state in-process were
# left with no way to schedule probe or retry work after the first
# shutdown. ``_get_probe_executor`` recreates it on demand.
_PROBE_EXECUTOR: futures.ThreadPoolExecutor | None = None


def _get_probe_executor() -> futures.ThreadPoolExecutor:
    """Lazily create or recreate the dedicated probe executor.

    Codex flagged that the previous module-level executor was shut
    down on lifespan exit but never recreated. In-process restarts
    (test harnesses, ``uvicorn --reload``) would then schedule probe
    work onto a closed executor and silently fail. With this lazy
    accessor every probe / retry call enters with a live executor —
    or creates a fresh one if the previous lifespan tore it down.
    Shares its lazy-create/recreate-after-shutdown logic with
    ``github_events_handler._get_dispatch_executor`` via
    :func:`unified_api.bounded_executor.get_or_recreate_executor`.
    """
    global _PROBE_EXECUTOR
    _PROBE_EXECUTOR = get_or_recreate_executor(_PROBE_EXECUTOR, max_workers=2, thread_name_prefix="pd-health-probe")
    return _PROBE_EXECUTOR


def _shutdown_probe_executor() -> None:
    """Shut down the probe executor and clear the slot for re-creation."""
    global _PROBE_EXECUTOR
    if _PROBE_EXECUTOR is not None:
        _PROBE_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        _PROBE_EXECUTOR = None


async def _probe_postgres_live() -> bool:  # pragma: no cover - requires a live Postgres connection
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
        # Delegates to the shared, hard-bounded probe so there is one
        # ``SELECT 1`` implementation across the platform (the LLM-config
        # and GitHub-integration routes use the same helper). It returns
        # False when Postgres is disabled, the host is unreachable, or the
        # bounded acquisition times out — exactly this branch's contract.
        from shared.postgres import check_connection

        return check_connection(timeout_s=_PROBE_DB_TIMEOUT_S)

    loop = asyncio.get_running_loop()
    try:
        # Outer wait_for gives the await an upper bound even if the
        # inner psycopg timeout fires later than expected. Worker
        # cleanup is guaranteed by the inner timeout; this is just
        # belt-and-suspenders for the await side.
        return await asyncio.wait_for(
            loop.run_in_executor(_get_probe_executor(), _ping),
            timeout=_PROBE_DB_TIMEOUT_S + 0.5,
        )
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


def _is_postgres_enabled_cached() -> bool:
    """``shared.postgres.is_postgres_enabled()`` without the import dance.

    Just reads the env var directly so the health handler doesn't pay
    an import on every call. Postgres-disabled environments use this
    to drop in-process teams to ``unavailable`` rather than ``unhealthy``.
    """
    return bool(os.environ.get("POSTGRES_HOST", "").strip())


def _expected_tables_for(team_key: str) -> list[str]:
    """Return the list of tables an in-process team is expected to own.

    Sourced from each team's ``TeamSchema.table_names`` so this stays
    in lockstep with the schema-registry truth, no separate list.
    """
    if team_key == "product_delivery":
        from product_delivery.postgres import SCHEMA as PRODUCT_DELIVERY_SCHEMA

        return list(PRODUCT_DELIVERY_SCHEMA.table_names)
    # Other in-process teams (agent_console, team_assistant, …) don't
    # currently surface table-presence checks in /health. Add cases
    # here as they adopt the pattern.
    return []


async def _verify_in_process_schema_present(team_key: str) -> bool:  # pragma: no cover - needs live Postgres
    """Check that the team's expected tables still exist in Postgres.

    Codex flagged that ``SELECT 1`` alone proves connectivity but
    *not* that the team's schema is intact — a manual ``DROP TABLE``,
    a botched migration, or a database swap could leave product-delivery
    endpoints returning storage errors while ``/health`` stayed green.
    We re-confirm the table set every time this branch fires; if any
    expected table is missing, the team flips to ``unhealthy`` and is
    added back to ``_in_process_schema_failures`` so the background
    loop's retry path attempts re-registration.

    Runs the synchronous psycopg query inside the dedicated probe
    executor (same pool as ``_probe_postgres_live`` and the schema
    retry) so it can't starve the default ``to_thread`` executor.
    """
    expected = _expected_tables_for(team_key)
    if not expected:
        # No declared tables → nothing to verify. Treat as healthy
        # (e.g. a future in-process team that doesn't own any tables).
        return True

    def _check() -> bool:
        from shared.postgres import client as _pg_client
        from shared.postgres import is_postgres_enabled, probe_cursor

        if not is_postgres_enabled():
            return False
        try:
            pool = _pg_client._get_or_create_pool()
            # Bound the query itself (not just slot acquisition) via the shared probe
            # helper's transaction-local statement_timeout, so a post-connect mid-query
            # stall releases this pooled connection within the budget — same guarantee as
            # check_connection, instead of an unbounded SELECT on the shared pool.
            with (
                pool.connection(timeout=_PROBE_DB_TIMEOUT_S) as conn,
                probe_cursor(conn, timeout_s=_PROBE_DB_TIMEOUT_S) as cur,
            ):
                # `to_regclass` returns NULL for missing tables — fast,
                # one round-trip, and it doesn't lock anything.
                placeholders = ", ".join(["to_regclass(%s) IS NOT NULL"] * len(expected))
                cur.execute(f"SELECT {placeholders}", expected)
                row = cur.fetchone()
                return row is not None and all(row)
        except Exception:
            return False

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_get_probe_executor(), _check),
            timeout=_PROBE_DB_TIMEOUT_S + 0.5,
        )
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


def _retry_in_process_schema_registration(team_key: str) -> bool:  # pragma: no cover - needs live Postgres
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
            from shared.postgres import ensure_team_schema

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
            elif key in _in_process_schema_failures:  # pragma: no cover - only reached with POSTGRES_HOST set
                # Background loop may have already cleared this; if
                # we're still in the set, persistence is still broken.
                # Read-only: don't trigger a retry from this handler.
                status = "unhealthy"
            else:  # pragma: no cover - only reached with POSTGRES_HOST set (live DB probe branch)
                if db_live is None:
                    db_live = await _probe_postgres_live()
                if not db_live:
                    status = "unhealthy"
                else:
                    # `SELECT 1` proves connectivity — but Codex
                    # flagged that it doesn't prove the team's tables
                    # are still present (a manual `DROP TABLE`,
                    # botched migration, or DB swap can leave
                    # endpoints 503-ing while /health stayed green).
                    # Verify the team's expected tables exist; if any
                    # are missing, mark the team unhealthy and queue
                    # a background-loop retry to re-register.
                    if await _verify_in_process_schema_present(key):
                        status = "healthy"
                    else:
                        logger.warning(
                            "%s: Postgres reachable but expected tables missing; "
                            "marking unhealthy and queueing schema retry",
                            key,
                        )
                        _in_process_schema_failures.add(key)
                        status = "unhealthy"
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
    """List all configured teams with their mount/proxy status.

    Preconditions: none.
    Postconditions: returns ``200`` with a ``teams`` dict keyed by team key;
        each entry reports ``name``, ``prefix``, ``description``, whether the
        team is currently ``mounted``, its configured ``enabled`` flag, and a
        ``docs_url`` (``None`` when the team isn't mounted or is in-process,
        since those don't expose a per-team ``/docs`` endpoint).
    """
    teams = {}
    for key, config in TEAM_CONFIGS.items():
        mounted = _registered_teams.get(key, False)
        # In-process teams piggy-back on the unified API's `/docs` —
        # they don't expose a `/api/<team>/docs` endpoint themselves,
        # so don't advertise one (it would 404).
        per_team_docs = mounted and not config.in_process
        teams[key] = {
            "name": config.name,
            "prefix": config.prefix,
            "description": config.description,
            "mounted": mounted,
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


@app.get("/api/se/metrics", tags=["software", "observability"])
async def se_metrics_alias(window_days: float = 30.0) -> dict[str, Any]:
    """Alias for the SE team's DORA metrics, proxied to its ``/dora`` route.

    The SDLC review specified ``GET /api/se/metrics`` while the SE team itself
    mounts under ``/api/software-engineering``; this thin alias satisfies that
    contract by forwarding to the SE service's ``/dora`` route. This alias prefix
    (``/api/se``) is registered in the security gateway's scanned prefixes so it
    shares the proxied path's security posture.
    """
    env_var = TEAM_SERVICE_URL_ENVS.get("software_engineering")
    base = (os.environ.get(env_var, "").strip() if env_var else "") or ""
    if not base:
        raise HTTPException(status_code=503, detail="software engineering service URL not configured")
    # Operability knob parsed via the shared typed reader (missing/garbage → 15s);
    # a non-positive value is then reset to the default, since a <=0 timeout would
    # make httpx fail instantly.
    timeout = env_float("SE_METRICS_ALIAS_TIMEOUT", 15.0)
    if timeout <= 0:
        timeout = 15.0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base.rstrip('/')}/dora", params={"window_days": window_days})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        # Forward the upstream failure as a gateway error rather than a 500 with a
        # leaked traceback.
        logger.warning("SE metrics alias: upstream returned %s", exc.response.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"software engineering service returned {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("SE metrics alias: upstream unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="software engineering service unreachable") from exc
    except ValueError as exc:
        # A 200 with a non-JSON body (e.g. an HTML error page) makes ``resp.json()``
        # raise ``json.JSONDecodeError`` (a ``ValueError`` subclass); surface a 502
        # rather than an unhandled 500 with a leaked traceback.
        logger.warning("SE metrics alias: non-JSON body from upstream: %s", exc)
        raise HTTPException(status_code=502, detail="invalid JSON from software engineering service") from exc
    return data


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
