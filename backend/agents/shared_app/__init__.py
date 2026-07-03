"""Shared FastAPI app construction for agent teams.

Re-exports :func:`create_team_app`, which collapses the repeated
``init_otel`` + ``FastAPI(lifespan=…)`` + Postgres-schema lifespan +
``instrument_fastapi_app`` boilerplate found in every team's ``api/main.py``,
and :func:`bootstrap_syspath`, the idempotent ``sys.path`` insert shared by the
team ``api`` packages. See ``factory.py`` / ``paths.py`` for the contracts.
"""

from __future__ import annotations

from shared_app.factory import create_team_app
from shared_app.paths import bootstrap_syspath

__all__ = ["bootstrap_syspath", "create_team_app"]
