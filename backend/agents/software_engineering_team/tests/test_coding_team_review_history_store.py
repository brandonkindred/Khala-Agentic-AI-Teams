"""Live-Postgres tests for the code-review history store.

Skipped unless ``POSTGRES_HOST`` is set (mirrors ``agent_console`` store tests).
Covers record/list, status transitions, the missing-row no-op, the PR filter,
ON CONFLICT idempotency, and the empty-result path.
"""

from __future__ import annotations

import pytest

from shared.postgres import TeamSchema, is_postgres_enabled, register_team_schemas
from shared.postgres.testing import truncate_team_tables
from software_engineering_team.postgres import SCHEMA as SE_SCHEMA
from software_engineering_team.review_history_store import (
    append_review_transcript_entry,
    get_review_transcript,
    list_reviews,
    record_review_start,
    update_review,
)

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres store tests",
)

# code_review_runs now lives in SE's merged schema (see
# software_engineering_team/postgres/__init__.py). Truncate only this table,
# not SE's full table_names list — se_agent_traces/se_events/se_learnings may
# hold state other, concurrently-running SE tests depend on.
_CODE_REVIEW_RUNS_SCHEMA = TeamSchema(
    team=SE_SCHEMA.team, statements=[], table_names=["code_review_runs"]
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SE_SCHEMA)
    truncate_team_tables(_CODE_REVIEW_RUNS_SCHEMA)


def test_record_and_list() -> None:
    record_review_start("j1", "o", "r", 7, "https://x/pull/7", "alice")
    rows = list_reviews("o", "r")
    assert len(rows) == 1
    assert rows[0]["job_id"] == "j1"
    assert rows[0]["status"] == "pending"
    assert rows[0]["pr_number"] == 7
    assert rows[0]["pr_url"] == "https://x/pull/7"
    assert rows[0]["author"] == "alice"
    assert rows[0]["completed_at"] is None


def test_update_transitions_to_completed() -> None:
    record_review_start("j1", "o", "r", 7, "u", "alice")
    update_review("j1", status="running", status_text="Reviewing pull request")
    update_review(
        "j1",
        status="completed",
        status_text="done",
        review_summary={
            "total_issues": 2,
            "inline_comments": 1,
            "comment_findings": 1,
            "event": "COMMENT",
        },
        completed=True,
    )
    row = list_reviews("o", "r", 7)[0]
    assert row["status"] == "completed"
    assert row["status_text"] == "done"
    assert row["review_summary"]["event"] == "COMMENT"
    assert row["completed_at"] is not None
    assert row["error"] is None


def test_update_failed_records_error() -> None:
    record_review_start("j1", "o", "r", 7, "u", "alice")
    update_review("j1", status="failed", error="kaboom", completed=True)
    row = list_reviews("o", "r")[0]
    assert row["status"] == "failed"
    assert row["error"] == "kaboom"
    assert row["completed_at"] is not None


def test_update_missing_row_is_noop() -> None:
    # No record_review_start: the update must not raise and must not create a row.
    update_review("ghost", status="failed", error="x", completed=True)
    assert list_reviews("o", "r") == []


def test_pr_filter() -> None:
    record_review_start("a", "o", "r", 1, "u", "alice")
    record_review_start("b", "o", "r", 2, "u", "alice")
    record_review_start("c", "o", "r", 1, "u", "alice")
    assert {row["job_id"] for row in list_reviews("o", "r", 1)} == {"a", "c"}
    assert len(list_reviews("o", "r")) == 3
    assert list_reviews("o", "r", 99) == []


def test_record_start_is_idempotent_on_conflict() -> None:
    record_review_start("j1", "o", "r", 7, "u", "alice")
    record_review_start("j1", "o", "r", 7, "u", "bob")  # duplicate job_id ignored
    rows = list_reviews("o", "r")
    assert len(rows) == 1
    assert rows[0]["author"] == "alice"  # first write wins (ON CONFLICT DO NOTHING)


def test_transcript_entries_append_in_order() -> None:
    record_review_start("j1", "o", "r", 7, "u", "alice")
    append_review_transcript_entry(
        "j1",
        {
            "stage": "chunk_review",
            "target": "a.py",
            "model": "m",
            "prompt": "p1",
            "response": "r1",
            "started_at": "2024-01-01T00:00:00+00:00",
            "duration_ms": 10,
        },
    )
    append_review_transcript_entry(
        "j1",
        {
            "stage": "synthesis",
            "target": "",
            "model": "m",
            "prompt": "p2",
            "response": "r2",
            "started_at": "2024-01-01T00:00:01+00:00",
            "duration_ms": 5,
        },
    )
    entries = get_review_transcript("j1")
    assert [e["stage"] for e in entries] == ["chunk_review", "synthesis"]
    assert entries[0]["prompt"] == "p1"
    assert entries[1]["response"] == "r2"


def test_transcript_missing_returns_none() -> None:
    record_review_start("j1", "o", "r", 7, "u", "alice")
    # No append_review_transcript_entry call: the row is never created.
    assert get_review_transcript("j1") is None


def test_transcript_for_unknown_job_is_noop() -> None:
    # No FK row (record_review_start was never called for "ghost"): the FK
    # constraint on job_id must make this a no-op like every other best-effort
    # write, not raise.
    append_review_transcript_entry("ghost", {"stage": "x"})
    assert get_review_transcript("ghost") is None
