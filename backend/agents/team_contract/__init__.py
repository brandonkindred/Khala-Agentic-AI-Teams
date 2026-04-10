"""
Standard team contract package for Khala agent teams.

Provides a factory function ``create_team_app()`` that returns a FastAPI app
with standardized health, readiness, metadata, and job lifecycle endpoints
pre-registered. Teams import and extend rather than building from scratch.

Usage::

    from team_contract import create_team_app

    app = create_team_app("blogging", version="1.0.0", capabilities=["full-pipeline", "research"])

    @app.post("/full-pipeline")
    def full_pipeline(request: FullPipelineRequest):
        ...
"""

from .base_app import create_team_app
from .health import HealthCheck, HealthCheckRegistry
from .job_router import job_router

__all__ = [
    "create_team_app",
    "HealthCheck",
    "HealthCheckRegistry",
    "job_router",
]
