"""TeamSchema dataclass — the data contract each team exports.

Every team that owns Postgres tables creates a ``postgres/__init__.py``
module that exports a single ``SCHEMA: TeamSchema`` constant. The module
must be pure data: importing it should have **no side effects** (no
connection attempts, no DDL execution). DDL runs only when a FastAPI
lifespan explicitly calls ``register_team_schemas(SCHEMA)``.

This is the main contrast with ``shared_temporal``'s Pattern A, which
launches a daemon worker thread as an import side effect. Schema DDL is
synchronous blocking I/O and must not fire from a unit test or linter
import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TeamSchema:
    """The Postgres schema owned by one team.

    Attributes:
        team: Team slug used in log lines and the registry. Lowercase,
            underscore-separated, e.g. ``"branding"``, ``"job_service"``.
        database: Optional Postgres database name. ``None`` means use the
            default ``POSTGRES_DB`` env var. Legacy services like
            ``job_service`` override this to ``"khala_jobs"``.
        statements: List of DDL strings to execute idempotently at
            startup. Each should be a ``CREATE TABLE IF NOT EXISTS`` /
            ``CREATE INDEX IF NOT EXISTS`` / ``ALTER TABLE ...`` string.
            Statements run in order, each in its own transaction, so a
            failure in one doesn't abort the rest.
        table_names: Explicit list of tables the team owns. Used by
            ``shared_postgres.testing.truncate_team_tables`` to wipe
            state between tests. Kept as an explicit declaration
            instead of regex-parsing ``statements`` so there is no
            ambiguity around comments, whitespace, or unusual DDL.
    """

    team: str
    statements: list[str] = field(default_factory=list)
    database: Optional[str] = None
    table_names: list[str] = field(default_factory=list)
