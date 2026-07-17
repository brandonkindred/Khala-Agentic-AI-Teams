"""Schema tests for the Planning team.

* Shape tests run without a database and assert the load-bearing
  constraints exist in the DDL text.
* Idempotency tests are skipped unless ``POSTGRES_HOST`` is set; they prove
  the schema applies and re-applies cleanly against live Postgres, mirroring
  ``agent_cognition`` / ``product_delivery`` store tests.
"""

from __future__ import annotations

import pytest

from planning_team.postgres import SCHEMA
from shared_postgres import TeamSchema

EXPECTED_TABLES = ["planning_runs"]


# ---------------------------------------------------------------------------
# Shape tests (no Postgres required).
# ---------------------------------------------------------------------------
def test_schema_identity() -> None:
    assert isinstance(SCHEMA, TeamSchema)
    assert SCHEMA.team == "planning"
    assert SCHEMA.database is None


def test_schema_declares_all_tables() -> None:
    assert SCHEMA.table_names == EXPECTED_TABLES
    joined = "\n".join(SCHEMA.statements)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined


def test_planning_runs_columns_present() -> None:
    ddl = next(s for s in SCHEMA.statements if "CREATE TABLE IF NOT EXISTS planning_runs" in s)
    assert "job_id             TEXT PRIMARY KEY" in ddl
    for col in (
        "client_name",
        "summary",
        "handoff_summary",
        "open_questions",
        "resolved_questions",
        "created_at",
    ):
        assert col in ddl


def test_created_at_index_present() -> None:
    joined = "\n".join(SCHEMA.statements)
    assert "idx_planning_runs_created" in joined and "planning_runs(created_at)" in joined


def test_all_ddl_is_idempotent() -> None:
    for stmt in SCHEMA.statements:
        assert "IF NOT EXISTS" in stmt or "IF EXISTS" in stmt, stmt


def test_registry_wiring() -> None:
    from shared_postgres.registry import TEAM_POSTGRES_MODULES

    assert TEAM_POSTGRES_MODULES["planning"] == "planning_team.postgres"


# ---------------------------------------------------------------------------
# Idempotency test (live Postgres only).
# ---------------------------------------------------------------------------
def test_schema_applies_idempotently() -> None:
    from shared_postgres import is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres schema test")

    from shared_postgres.testing import truncate_team_tables

    assert register_team_schemas(SCHEMA) is True
    assert register_team_schemas(SCHEMA) is True
    assert truncate_team_tables(SCHEMA) == len(EXPECTED_TABLES)
