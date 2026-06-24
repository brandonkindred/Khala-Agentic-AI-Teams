"""Shared FastAPI app construction for agent teams.

Re-exports :func:`create_team_app`, which collapses the repeated
``init_otel`` + ``FastAPI(lifespan=…)`` + Postgres-schema lifespan +
``instrument_fastapi_app`` boilerplate found in every team's ``api/main.py``.
See ``factory.py`` for the contract.
"""

from __future__ import annotations

from shared_app.factory import create_team_app

__all__ = ["create_team_app"]
