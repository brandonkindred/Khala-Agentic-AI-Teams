"""Test helpers for ``shared.postgres``.

Small utilities that are useful only in tests — kept out of the main
package so production imports don't pull them in. Includes live-Postgres
truncate helpers (``truncate_team_tables``, ``truncate_all_teams``,
``drop_team_tables``), the ``real_postgres_schema`` pytest fixture
factory, and re-exports the in-memory fake scaffold from
``shared.postgres.fake`` for store unit tests that must not hit Postgres.
"""

from __future__ import annotations

import logging
from typing import Iterable

import pytest

from shared.postgres.client import get_conn, is_postgres_enabled
from shared.postgres.runner import register_team_schemas
from shared.postgres.schema import TeamSchema

logger = logging.getLogger(__name__)


def truncate_team_tables(schema: TeamSchema) -> int:
    """Truncate every table named in ``schema.table_names``.

    All truncates run inside a single transaction with
    ``RESTART IDENTITY CASCADE`` so that sequences reset and any
    foreign-key dependents are wiped together. Returns the number of
    tables truncated.

    Raises ``RuntimeError`` when Postgres is disabled (matches
    ``ensure_team_schema``'s policy of failing loudly on misuse).
    """
    if not is_postgres_enabled():
        raise RuntimeError(f"truncate_team_tables called for team={schema.team} but POSTGRES_HOST is not set.")
    if not schema.table_names:
        return 0

    # Quote identifiers so unusual table names can't break the SQL; we
    # still reject anything with a double quote to be safe.
    quoted = [_quote_ident(name) for name in schema.table_names]
    sql = f"TRUNCATE TABLE {', '.join(quoted)} RESTART IDENTITY CASCADE"

    with get_conn(schema.database) as conn, conn.cursor() as cur:
        cur.execute(sql)

    logger.debug(
        "truncate_team_tables: team=%s truncated=%d tables=%s",
        schema.team,
        len(schema.table_names),
        schema.table_names,
    )
    return len(schema.table_names)


def drop_team_tables(schema: TeamSchema) -> int:
    """Drop every table named in ``schema.table_names``.

    Unlike :func:`truncate_team_tables` (which empties rows but leaves the
    table present), this removes the tables themselves — for tests that need
    to simulate a genuinely fresh/empty database (e.g. proving a schema gets
    (re-)created before some other code path reads from it). Returns the
    number of tables dropped.

    Raises ``RuntimeError`` when Postgres is disabled (matches
    ``ensure_team_schema``'s policy of failing loudly on misuse).
    """
    if not is_postgres_enabled():
        raise RuntimeError(f"drop_team_tables called for team={schema.team} but POSTGRES_HOST is not set.")
    if not schema.table_names:
        return 0

    # Validate every identifier before opening a connection, matching
    # truncate_team_tables — a bad name must fail fast, not after a pool wait.
    quoted = [_quote_ident(name) for name in schema.table_names]

    with get_conn(schema.database) as conn, conn.cursor() as cur:
        for ident in quoted:
            cur.execute(f"DROP TABLE IF EXISTS {ident} CASCADE")

    logger.debug(
        "drop_team_tables: team=%s dropped=%d tables=%s",
        schema.team,
        len(schema.table_names),
        schema.table_names,
    )
    return len(schema.table_names)


def truncate_all_teams(schemas: Iterable[TeamSchema]) -> int:
    """Truncate every team's tables in a single call.

    Convenience wrapper for top-level test fixtures that want to wipe
    the entire shared Postgres between integration-test runs.
    """
    total = 0
    for schema in schemas:
        if not schema.table_names:
            continue
        total += truncate_team_tables(schema)
    return total


def _real_postgres_schema_body(schema: TeamSchema):
    """Generator body shared by every ``real_postgres_schema(schema)`` fixture.

    Split out from :func:`real_postgres_schema` so the skip/register/truncate
    sequence is directly unit-testable (drive the generator with ``next()``)
    without going through pytest's fixture machinery.
    """
    if not is_postgres_enabled():
        pytest.skip(f"real Postgres tests require POSTGRES_HOST (team={schema.team})")
    register_team_schemas(schema)
    yield
    truncate_team_tables(schema)


def real_postgres_schema(schema: TeamSchema, *, scope: str = "module"):
    """Build a pytest fixture that provisions ``schema`` against a live Postgres.

    Registers ``schema`` once per fixture instance (per ``scope``), yields, then
    truncates its tables on teardown via :func:`truncate_team_tables` — so tests
    within the scope run against real DDL without accumulating rows across runs.
    Skips the test (rather than raising) when ``POSTGRES_HOST`` is unset, matching
    every other real-Postgres fixture in this repo.

    A drop-in replacement for the per-team hand-rolled version of this pattern
    (see ``branding_team/tests/test_store_real_postgres.py``'s ``_branding_schema``
    fixture): any team's ``conftest.py`` or test module can do
    ``_my_schema = real_postgres_schema(SCHEMA)`` instead of re-deriving the
    skip/register/truncate boilerplate.

    Not itself wired into any team's tests yet — callers opt in explicitly.
    """

    def _fixture():
        yield from _real_postgres_schema_body(schema)

    _fixture.__name__ = f"real_postgres_schema_{schema.team}"
    return pytest.fixture(scope=scope, autouse=True)(_fixture)


def _quote_ident(name: str) -> str:
    if '"' in name:
        raise ValueError(f"refusing to quote identifier containing a double-quote: {name!r}")
    return f'"{name}"'


# Re-export the in-memory fake scaffold so callers can import from either
# ``shared.postgres.testing`` or ``shared.postgres.fake``.
from shared.postgres.fake import (  # noqa: E402
    FakeConn,
    FakeCursor,
    install_fake_postgres,
    unwrap_json,
)

__all__ = [
    "FakeConn",
    "FakeCursor",
    "drop_team_tables",
    "install_fake_postgres",
    "real_postgres_schema",
    "truncate_all_teams",
    "truncate_team_tables",
    "unwrap_json",
]
