"""Schema tests for the Agent Cognition Core.

Two layers:

* **Shape tests** run without a database and assert the load-bearing
  constraints exist in the DDL text (the acceptance criteria for Step 1).
* **Idempotency tests** are skipped unless ``POSTGRES_HOST`` is set; they
  prove the schema applies and re-applies cleanly against live Postgres,
  mirroring ``agent_console`` / ``product_delivery`` store tests.
"""

from __future__ import annotations

import pytest

from agent_cognition.postgres import SCHEMA
from shared_postgres import TeamSchema

EXPECTED_TABLES = [
    "agent_cognition_events",
    "agent_cognition_summaries",
    "agent_cognition_rules",
    "agent_cognition_rule_proposals",
    "agent_cognition_runs",
]


# ---------------------------------------------------------------------------
# Shape tests (no Postgres required).
# ---------------------------------------------------------------------------
def test_schema_identity() -> None:
    assert isinstance(SCHEMA, TeamSchema)
    assert SCHEMA.team == "agent_cognition"
    assert SCHEMA.database is None


def test_schema_declares_all_five_tables() -> None:
    assert SCHEMA.table_names == EXPECTED_TABLES
    # Every declared table has a matching CREATE TABLE statement.
    joined = "\n".join(SCHEMA.statements)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined


def test_all_ddl_is_idempotent() -> None:
    # Pattern B requires startup DDL to be safe to re-run.
    for stmt in SCHEMA.statements:
        assert "IF NOT EXISTS" in stmt, stmt


def test_events_idempotency_key_present() -> None:
    joined = "".join(SCHEMA.statements)
    assert (
        "uq_agent_cognition_events_writeback" in joined
        and "agent_cognition_events(agent_id, source_run_id, source_seq)" in joined
    )


def test_summary_period_key_present() -> None:
    joined = "".join(SCHEMA.statements)
    assert (
        "uq_agent_cognition_summaries_period" in joined
        and "agent_cognition_summaries(agent_id, scale, period_start)" in joined
    )


def test_runs_composite_primary_key_present() -> None:
    runs_ddl = next(s for s in SCHEMA.statements if "agent_cognition_runs" in s)
    assert "PRIMARY KEY (agent_id, source_run_id)" in runs_ddl


def test_summaries_have_version_and_stale_columns() -> None:
    summaries_ddl = next(
        s for s in SCHEMA.statements if "CREATE TABLE IF NOT EXISTS agent_cognition_summaries" in s
    )
    assert "version" in summaries_ddl
    assert "stale" in summaries_ddl
    assert "covers_through" in summaries_ddl


def test_evidence_columns_present_on_rules_and_proposals() -> None:
    rules_ddl = next(
        s for s in SCHEMA.statements if "CREATE TABLE IF NOT EXISTS agent_cognition_rules" in s
    )
    proposals_ddl = next(
        s
        for s in SCHEMA.statements
        if "CREATE TABLE IF NOT EXISTS agent_cognition_rule_proposals" in s
    )
    assert "evidence" in rules_ddl and "needs_review" in rules_ddl
    assert "evidence" in proposals_ddl and "action" in proposals_ddl


# ---------------------------------------------------------------------------
# Idempotency tests (live Postgres only).
# ---------------------------------------------------------------------------
def test_schema_applies_idempotently() -> None:
    from shared_postgres import is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres schema test")

    from shared_postgres.testing import truncate_team_tables

    # Apply twice — the second call must be a no-op (idempotent DDL).
    assert register_team_schemas(SCHEMA) is True
    assert register_team_schemas(SCHEMA) is True
    # Tables exist and are truncatable.
    assert truncate_team_tables(SCHEMA) == len(EXPECTED_TABLES)
