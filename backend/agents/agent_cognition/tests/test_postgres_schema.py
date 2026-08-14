"""Schema tests for the Agent Cognition Core.

Two layers:

* **Shape tests** run without a database and assert the load-bearing
  constraints exist in the DDL text (the acceptance criteria for Step 1).
* **Idempotency tests** are skipped unless ``POSTGRES_HOST`` is set; they
  prove the schema applies and re-applies cleanly against live Postgres,
  mirroring ``agent_platform.console`` / ``product_delivery`` store tests.
"""

from __future__ import annotations

import pytest

from agent_cognition.postgres import SCHEMA
from shared.postgres import TeamSchema

EXPECTED_TABLES = [
    "agent_cognition_events",
    "agent_cognition_summaries",
    "agent_cognition_rules",
    "agent_cognition_rule_proposals",
    "agent_cognition_runs",
    "agent_cognition_graph_watermarks",
]


# ---------------------------------------------------------------------------
# Shape tests (no Postgres required).
# ---------------------------------------------------------------------------
def test_schema_identity() -> None:
    assert isinstance(SCHEMA, TeamSchema)
    assert SCHEMA.team == "agent_cognition"
    assert SCHEMA.database is None


def test_schema_declares_all_tables() -> None:
    assert SCHEMA.table_names == EXPECTED_TABLES
    # Every declared table has a matching CREATE TABLE statement.
    joined = "\n".join(SCHEMA.statements)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined


def test_graph_watermark_table_and_keyset_indexes_present() -> None:
    # The knowledge-graph sync worker tracks per-agent ingestion progress in
    # agent_cognition_graph_watermarks with symmetric keyset cursors over events
    # (recorded_at, id) and summaries (updated_at, id); the table and both
    # supporting keyset indexes must be declared here.
    joined = "".join(SCHEMA.statements)
    watermark_ddl = next(
        s
        for s in SCHEMA.statements
        if "CREATE TABLE IF NOT EXISTS agent_cognition_graph_watermarks" in s
    )
    assert "agent_id                 TEXT PRIMARY KEY" in watermark_ddl
    for col in (
        "last_event_recorded_at",
        "last_event_id",
        "last_summary_updated_at",
        "last_summary_id",
        "ingested_count",
    ):
        assert col in watermark_ddl
    assert (
        "idx_agent_cognition_events_agent_recorded" in joined
        and "agent_cognition_events(agent_id, recorded_at, id)" in joined
    )
    assert (
        "idx_agent_cognition_summaries_agent_updated" in joined
        and "agent_cognition_summaries(agent_id, updated_at, id)" in joined
    )


def test_all_ddl_is_idempotent() -> None:
    # Pattern B requires startup DDL to be safe to re-run. CREATE/ALTER-ADD
    # statements guard with IF NOT EXISTS; the migration cleanup DROPs (the dead
    # pre-rename summary cursor index/column) guard with IF EXISTS — both forms
    # re-run safely. "IF NOT EXISTS" does not contain "IF EXISTS", so an unguarded
    # statement still fails this check.
    for stmt in SCHEMA.statements:
        assert "IF NOT EXISTS" in stmt or "IF EXISTS" in stmt, stmt


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


def test_runs_terminal_gc_index_present() -> None:
    # The central scheduler's terminal-run GC filters
    # ``status = ANY(terminal) AND completed_at < cutoff``; a composite
    # (status, completed_at) index must back it so the hourly pass doesn't
    # full-scan the retained ledger.
    joined = "".join(SCHEMA.statements)
    assert (
        "idx_agent_cognition_runs_status_completed" in joined
        and "agent_cognition_runs(status, completed_at)" in joined
    )


def test_summaries_have_version_and_stale_columns() -> None:
    summaries_ddl = next(
        s for s in SCHEMA.statements if "CREATE TABLE IF NOT EXISTS agent_cognition_summaries" in s
    )
    assert "version" in summaries_ddl
    assert "stale" in summaries_ddl
    assert "covers_through" in summaries_ddl


def test_summaries_have_events_pruned_column() -> None:
    # Durable recompute-vs-amend marker owned by the memory store. Present in
    # the CREATE for fresh clusters and back-filled via an idempotent ALTER.
    joined = "".join(SCHEMA.statements)
    assert "events_pruned" in joined
    assert (
        "ALTER TABLE agent_cognition_summaries" in joined
        and "ADD COLUMN IF NOT EXISTS events_pruned" in joined
    )


def test_summaries_have_updated_at_column() -> None:
    # Content-write time for the knowledge-graph keyset cursor. Present in the
    # CREATE for fresh clusters and back-filled via an idempotent ALTER — the
    # ALTER is load-bearing: without it, fetch_summaries_updated_after's
    # `SELECT ..., updated_at` projection errors on upgraded clusters.
    joined = "".join(SCHEMA.statements)
    assert "updated_at" in joined
    assert (
        "ALTER TABLE agent_cognition_summaries" in joined
        and "ADD COLUMN IF NOT EXISTS updated_at" in joined
    )


def test_superseded_summary_cursor_ddl_dropped() -> None:
    # The summary graph cursor moved created_at → updated_at. The pre-rename
    # index and watermark column are dropped (IF EXISTS) so upgraded clusters
    # don't carry a dead index (per-write cost) or an orphan column.
    joined = "".join(SCHEMA.statements)
    assert "DROP INDEX IF EXISTS idx_agent_cognition_summaries_agent_created" in joined
    assert "DROP COLUMN IF EXISTS last_summary_created_at" in joined


def test_retention_guard_columns_present() -> None:
    # Arrival-time guard for prune_events: events.recorded_at +
    # summaries.computed_at, each in its CREATE and a back-filling ALTER.
    joined = "".join(SCHEMA.statements)
    assert "recorded_at" in joined
    assert "ADD COLUMN IF NOT EXISTS recorded_at" in joined
    assert "computed_at" in joined
    assert "ADD COLUMN IF NOT EXISTS computed_at" in joined


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
    from shared.postgres import is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres schema test")

    from shared.postgres.testing import truncate_team_tables

    # Apply twice — the second call must be a no-op (idempotent DDL).
    assert register_team_schemas(SCHEMA) is True
    assert register_team_schemas(SCHEMA) is True
    # Tables exist and are truncatable.
    assert truncate_team_tables(SCHEMA) == len(EXPECTED_TABLES)
