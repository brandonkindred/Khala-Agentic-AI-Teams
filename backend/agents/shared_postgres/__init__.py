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
from shared_postgres.metrics import timed_query
from shared_postgres.registry import TEAM_POSTGRES_MODULES, register_all_team_schemas
from shared_postgres.runner import ensure_team_schema, register_team_schemas
from shared_postgres.schema import TeamSchema


def _import_json_adapter():
    """Lazy re-export of ``psycopg.types.json.Json``.

    Imported at call sites as ``from shared_postgres import Json`` so
    stores that insert dicts into JSONB columns don't have to learn the
    psycopg submodule path. Lazy so importing this package still works
    when ``psycopg`` isn't installed (e.g. linter runs, docs builds).
    """
    from psycopg.types.json import Json  # noqa: PLC0415

    return Json


def _import_dict_row():
    """Lazy re-export of ``psycopg.rows.dict_row``."""
    from psycopg.rows import dict_row  # noqa: PLC0415

    return dict_row


def __getattr__(name: str):
    if name == "Json":
        return _import_json_adapter()
    if name == "dict_row":
        return _import_dict_row()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "TEAM_POSTGRES_MODULES",
    "Json",
    "TeamSchema",
    "close_pool",
    "dict_row",
    "ensure_team_schema",
    "get_conn",
    "is_postgres_enabled",
    "register_all_team_schemas",
    "register_team_schemas",
    "timed_query",
]
