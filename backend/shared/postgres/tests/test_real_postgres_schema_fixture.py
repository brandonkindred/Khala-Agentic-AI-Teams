"""End-to-end coverage for the ``real_postgres_schema`` fixture factory.

Unlike ``test_shared_postgres.py``'s mocked coverage of
``_real_postgres_schema_body``, this drives the fixture through pytest's
actual fixture machinery against a live Postgres — proving schema
registration and query-ability within the module work together, not just
in isolation. Skips when ``POSTGRES_HOST`` is unset, matching every other
real-Postgres test in this repo.

CI runs this suite under ``pytest -n 4`` (xdist): a module-scoped autouse
fixture can be instantiated independently per worker, and worker
processes share the same live Postgres. This file therefore avoids any
assumption about test execution order or a total row count on the shared
demo table — each test uses its own ``uuid4``-suffixed row id and only
ever asserts about that row, so concurrent workers can't collide.
"""

from __future__ import annotations

import uuid

import pytest

from shared.postgres import TeamSchema, get_conn
from shared.postgres.testing import real_postgres_schema

pytestmark = pytest.mark.integration

_SCHEMA = TeamSchema(
    team="shared_postgres_fixture_demo",
    statements=[
        """CREATE TABLE IF NOT EXISTS shared_postgres_fixture_demo (
            id TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
    ],
    table_names=["shared_postgres_fixture_demo"],
)

_schema_fixture = real_postgres_schema(_SCHEMA, scope="module")


def test_fixture_registers_schema_and_table_is_queryable() -> None:
    row_id = f"row_{uuid.uuid4().hex[:12]}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shared_postgres_fixture_demo (id, value) VALUES (%s, %s)",
            (row_id, "hello"),
        )
        cur.execute(
            "SELECT value FROM shared_postgres_fixture_demo WHERE id = %s",
            (row_id,),
        )
        assert cur.fetchone() == ("hello",)
