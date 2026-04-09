"""
Factory for creating standardized team FastAPI applications.

``create_team_app()`` returns a FastAPI app with:
- Standard ``/health`` endpoint (composable health checks)
- Standard ``/ready`` endpoint (readiness probe)
- Standard ``/meta`` endpoint (team metadata and capabilities)
- CORS middleware pre-configured
- Optional standard job lifecycle router

Usage::

    from team_contract import create_team_app

    app = create_team_app(
        name="blogging",
        version="1.0.0",
        description="Blog content pipeline",
        capabilities=["full-pipeline", "research", "medium-stats"],
    )

    # Add team-specific routes
    @app.post("/full-pipeline")
    def full_pipeline(request: FullPipelineRequest):
        ...
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .health import HealthCheckRegistry

logger = logging.getLogger(__name__)


class MetaResponse(BaseModel):
    """Standard team metadata response."""

    name: str
    version: str
    description: str
    capabilities: List[str]


class ReadyResponse(BaseModel):
    """Standard readiness response."""

    ready: bool


def create_team_app(
    name: str,
    *,
    version: str = "1.0.0",
    description: str = "",
    capabilities: Optional[List[str]] = None,
    health_checks: Optional[HealthCheckRegistry] = None,
    include_job_router: bool = False,
) -> FastAPI:
    """Create a FastAPI app with standard team contract endpoints.

    Parameters
    ----------
    name:
        Team identifier (e.g. "blogging", "software_engineering").
    version:
        Semantic version of this team's API.
    description:
        Human-readable description of what this team does.
    capabilities:
        List of capability strings this team supports (e.g. ["full-pipeline", "research"]).
    health_checks:
        Optional pre-configured health check registry. If None, a default
        registry is created (health endpoint always returns "ok").
    include_job_router:
        If True, includes the standard job lifecycle router at ``/jobs``.
    """
    caps = capabilities or []
    registry = health_checks or HealthCheckRegistry()

    # Initialize OpenTelemetry providers before constructing the app so the
    # FastAPI instrumentor we install below sees fully-configured tracers.
    try:
        from shared_observability import init_otel, instrument_fastapi_app
    except Exception:  # pragma: no cover - shared_observability always ships with agents
        init_otel = None  # type: ignore[assignment]
        instrument_fastapi_app = None  # type: ignore[assignment]
    if init_otel is not None:
        try:
            init_otel(service_name=f"{name}-team", team_key=name)
        except Exception:
            logger.warning("shared_observability init_otel failed for %s", name, exc_info=True)

    app = FastAPI(
        title=f"Strands {name.replace('_', ' ').title()} Team",
        description=description or f"Agent team: {name}",
        version=version,
    )

    if instrument_fastapi_app is not None:
        try:
            instrument_fastapi_app(app, team_key=name)
        except Exception:
            logger.warning(
                "OpenTelemetry FastAPI instrumentation failed for %s", name, exc_info=True
            )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store registry on app for teams to add checks after creation
    app.state.health_registry = registry

    @app.get("/health", tags=["health"])
    async def health() -> Dict[str, Any]:
        """Standard health check with composable sub-checks."""
        return await registry.run_all()

    @app.get("/ready", response_model=ReadyResponse, tags=["health"])
    async def ready() -> ReadyResponse:
        """Readiness probe — can this team accept new work?"""
        result = await registry.run_all()
        return ReadyResponse(ready=result["status"] != "error")

    @app.get("/meta", response_model=MetaResponse, tags=["meta"])
    async def meta() -> MetaResponse:
        """Team metadata and capabilities."""
        return MetaResponse(
            name=name,
            version=version,
            description=description or f"Agent team: {name}",
            capabilities=caps,
        )

    if include_job_router:
        from .job_router import create_job_router

        app.include_router(create_job_router(name))

    logger.info("Created team app: %s v%s (capabilities=%s)", name, version, caps)
    return app
