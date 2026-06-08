"""Live-Postgres tests for the code-review history store.

Skipped unless ``POSTGRES_HOST`` is set (mirrors ``agent_console`` store tests).
Covers record/list, status transitions, the missing-row no-op, the PR filter,
ON CONFLICT idempotency, and the empty-result path.
"""

from __future__ import annotations

import pytest

from coding_team.postgres import SCHEMA
from coding_team.review_history_store import list_reviews, record_review_start, update_review
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres store tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


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
            "body_findings": 1,
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
