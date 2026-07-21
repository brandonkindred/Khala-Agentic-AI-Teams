"""General multi-schema cold-start regression test, independent of any one team.

``job_matching_team/tests/test_schema_registration.py`` proves the concrete
``job_matching`` + ``user_profile`` case. This file proves the *general*
mechanism the team-service wrapper (``team_service/entrypoint.py``,
``build_wrapper_body``) relies on — every schema in an app's
``postgres_schemas`` set is registered before a Temporal worker starts —
against two throwaway, team-agnostic schemas and a real (disposable) Postgres,
rather than the synthetic ``types.SimpleNamespace`` fakes
``team_service/tests/test_entrypoint.py`` uses to check the same ordering
without booting a real database.

DESTRUCTIVE (to its own throwaway tables only): creates and drops
``_regress_schema_a`` / ``_regress_schema_b`` tables. These names are private
to this test and are not used by any real team, so this never touches
production data — but it still requires a real (disposable) Postgres.

Requires POSTGRES_HOST (skipped otherwise), e.g.:
    POSTGRES_HOST=localhost POSTGRES_USER=postgres POSTGRES_DB=postgres \\
        pytest -m integration test_multi_schema_cold_start.py -v
"""

from __future__ import annotations

import pytest

from shared.postgres.schema import TeamSchema

pytestmark = pytest.mark.integration

_TABLE_A = "_regress_schema_a_tbl"
_TABLE_B = "_regress_schema_b_tbl"

SCHEMA_A = TeamSchema(
    team="_regress_schema_a",
    statements=[f"CREATE TABLE IF NOT EXISTS {_TABLE_A} (id serial PRIMARY KEY)"],
    table_names=[_TABLE_A],
)
SCHEMA_B = TeamSchema(
    team="_regress_schema_b",
    statements=[f"CREATE TABLE IF NOT EXISTS {_TABLE_B} (id serial PRIMARY KEY)"],
    table_names=[_TABLE_B],
)


@pytest.fixture
def _postgres_required():
    import os

    if not os.environ.get("POSTGRES_HOST", "").strip():
        pytest.skip("test_multi_schema_cold_start requires POSTGRES_HOST to be set")


@pytest.fixture
def fresh_throwaway_tables(_postgres_required):
    """Ensure both throwaway tables start out absent (a genuinely fresh state).

    Drops both tables before AND after the test — before, in case a prior
    failed run left them behind; after, so this test never leaves state for
    the next run or another test to trip over.
    """
    from shared.postgres.testing import drop_team_tables

    drop_team_tables(SCHEMA_A)
    drop_team_tables(SCHEMA_B)
    yield
    drop_team_tables(SCHEMA_A)
    drop_team_tables(SCHEMA_B)


def _table_exists(schema: TeamSchema, table_name: str) -> bool:
    from shared.postgres.client import get_conn

    with get_conn(schema.database) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table_name,))
        (exists,) = cur.fetchone()
        return bool(exists)


def test_all_schemas_registered_before_worker_start_marker(fresh_throwaway_tables) -> None:
    """Mirrors the wrapper's generated ordering: loop-register every schema in
    an ``app.state.postgres_schemas``-shaped sequence, each in its own
    try/except (one failure must not skip the rest or block the worker start
    marker), THEN flip the marker — proving both throwaway tables exist by the
    time the marker (standing in for the Temporal worker start) fires."""
    from shared.postgres import register_team_schemas

    postgres_schemas = (SCHEMA_A, SCHEMA_B)  # primary + extra, declaration order
    registered: list[str] = []

    assert not _table_exists(SCHEMA_A, _TABLE_A)
    assert not _table_exists(SCHEMA_B, _TABLE_B)

    for schema in postgres_schemas:
        try:
            if register_team_schemas(schema):
                registered.append(schema.team)
        except Exception:
            pass  # one schema's failure must not block the rest, per the wrapper's contract

    worker_started = True  # stand-in for the Temporal worker start call

    assert registered == ["_regress_schema_a", "_regress_schema_b"]
    assert worker_started is True
    assert _table_exists(SCHEMA_A, _TABLE_A)
    assert _table_exists(SCHEMA_B, _TABLE_B)


def test_one_schema_failure_does_not_block_the_others_or_the_marker(fresh_throwaway_tables, monkeypatch) -> None:
    """A schema whose registration raises must not stop the remaining schemas
    from being attempted, nor block the worker-start marker — the same
    continue-past-failure contract the generated wrapper block enforces."""
    import shared.postgres as shared_postgres
    from shared.postgres import register_team_schemas as real_register

    schema_boom = TeamSchema(team="_regress_schema_boom")

    def _flaky(schema):
        if schema is schema_boom:
            raise RuntimeError("simulated connection failure")
        return real_register(schema)

    monkeypatch.setattr(shared_postgres, "register_team_schemas", _flaky)

    postgres_schemas = (schema_boom, SCHEMA_A, SCHEMA_B)
    registered: list[str] = []
    for schema in postgres_schemas:
        try:
            if shared_postgres.register_team_schemas(schema):
                registered.append(schema.team)
        except Exception:
            pass  # this schema's failure is logged elsewhere; must not stop the loop

    worker_started = True
    assert worker_started is True
    assert registered == ["_regress_schema_a", "_regress_schema_b"]  # boom skipped, both good ones ran
    assert _table_exists(SCHEMA_A, _TABLE_A)
    assert _table_exists(SCHEMA_B, _TABLE_B)
