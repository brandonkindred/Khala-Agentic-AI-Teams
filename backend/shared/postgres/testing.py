"""Test helpers for ``shared.postgres``.

Small utilities that are useful only in tests — kept out of the main
package so production imports don't pull them in. The key primitive is
``truncate_team_tables``: given a ``TeamSchema`` with an explicit
``table_names`` list, wipe every table so the next test starts clean.
"""

from __future__ import annotations

import logging
from typing import Iterable

from shared.postgres.client import get_conn, is_postgres_enabled
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
        raise RuntimeError(
            f"truncate_team_tables called for team={schema.team} but POSTGRES_HOST is not set."
        )
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
        raise RuntimeError(
            f"drop_team_tables called for team={schema.team} but POSTGRES_HOST is not set."
        )
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


def _quote_ident(name: str) -> str:
    if '"' in name:
        raise ValueError(f"refusing to quote identifier containing a double-quote: {name!r}")
    return f'"{name}"'
