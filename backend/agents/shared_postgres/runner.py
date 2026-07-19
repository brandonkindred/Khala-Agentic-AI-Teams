"""Schema application runner.

``ensure_team_schema`` executes each DDL statement in its own
transaction so a broken ``CREATE INDEX`` doesn't abort the ``CREATE
TABLE`` that came before it. Per-statement errors are logged at
``ERROR`` (not ``WARNING``) so CI surfaces them.

``register_team_schemas`` is the no-op-safe wrapper teams call from
their FastAPI lifespan: it returns immediately when ``POSTGRES_HOST`` is
unset, so lifespans can unconditionally invoke it.
"""

from __future__ import annotations

import logging

from shared_postgres.client import get_conn, is_postgres_enabled
from shared_postgres.schema import TeamSchema

logger = logging.getLogger(__name__)


def ensure_team_schema(schema: TeamSchema) -> int:
    """Apply every DDL statement in ``schema`` idempotently.

    Returns the number of statements that applied cleanly. Raises
    ``RuntimeError`` only when Postgres is disabled (to make misuse
    obvious); individual DDL failures are logged and skipped.
    """
    if not is_postgres_enabled():
        raise RuntimeError(
            f"ensure_team_schema called for team={schema.team} but POSTGRES_HOST is not set."
        )

    applied = 0
    failed: list[str] = []

    for idx, statement in enumerate(schema.statements):
        try:
            with get_conn(schema.database) as conn, conn.cursor() as cur:
                cur.execute(statement)
            applied += 1
        except Exception as e:
            # Truncate the statement so log lines stay readable; keep
            # enough to identify which DDL tripped.
            preview = " ".join(statement.split())[:120]
            logger.error(
                "postgres schema DDL failed: team=%s database=%s stmt_index=%d preview=%r error=%s",
                schema.team,
                schema.database or "<default>",
                idx,
                preview,
                e,
            )
            failed.append(preview)

    logger.info(
        "postgres schema ensured: team=%s database=%s applied=%d/%d failed=%d",
        schema.team,
        schema.database or "<default>",
        applied,
        len(schema.statements),
        len(failed),
    )
    return applied


def register_team_schemas(schema: TeamSchema) -> bool:
    """FastAPI-lifespan-friendly wrapper around ``ensure_team_schema``.

    Returns ``True`` when the schema was applied, ``False`` when
    Postgres is disabled. Safe to call unconditionally from any team's
    startup hook — teams that don't run in Docker stay a no-op.
    """
    if not is_postgres_enabled():
        logger.info(
            "postgres disabled; skipping schema registration for team=%s",
            schema.team,
        )
        return False
    ensure_team_schema(schema)
    return True
