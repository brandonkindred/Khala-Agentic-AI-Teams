"""Shared Postgres schema registration for team microservices.

Mirrors ``shared_temporal`` in structure but uses **Pattern B**
(explicit lifespan call) rather than Pattern A (import side effect),
because schema DDL is synchronous blocking I/O. See the README for
details.

Typical usage in a team's ``api/main.py`` lifespan::

    from shared_postgres import close_pool, register_team_schemas
    from my_team.postgres import SCHEMA

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        try:
            register_team_schemas(SCHEMA)
        except Exception:
            logger.exception("postgres schema registration failed")
        yield
        close_pool()
"""

from shared_postgres.client import close_pool, get_conn, is_postgres_enabled
from shared_postgres.registry import TEAM_POSTGRES_MODULES, register_all_team_schemas
from shared_postgres.runner import ensure_team_schema, register_team_schemas
from shared_postgres.schema import TeamSchema

__all__ = [
    "TEAM_POSTGRES_MODULES",
    "TeamSchema",
    "close_pool",
    "ensure_team_schema",
    "get_conn",
    "is_postgres_enabled",
    "register_all_team_schemas",
    "register_team_schemas",
]
