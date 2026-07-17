"""Tests for the Planning team's best-effort ``planning_runs`` audit writer.

* No-op / contract tests run without a database — the default test env has
  ``POSTGRES_HOST`` unset.
* Round-trip / idempotency tests are skipped inline unless ``POSTGRES_HOST``
  is set, matching ``planning_team.tests.test_postgres_schema``'s style
  (not a module-wide ``pytestmark``, which would also skip the no-op tests
  above that must run precisely when Postgres is off).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from planning_team.postgres.writer import record_planning_run

# ---------------------------------------------------------------------------
# No-op / contract tests (no Postgres required).
# ---------------------------------------------------------------------------


def test_record_planning_run_requires_job_id() -> None:
    with pytest.raises(ValueError, match="job_id"):
        record_planning_run(
            "",
            client_name=None,
            summary="s",
            handoff_summary="hs",
            open_questions=[],
            resolved_questions=[],
        )


def test_record_planning_run_noop_without_postgres() -> None:
    # Default test env has POSTGRES_HOST unset -> guarded no-op returns False.
    assert (
        record_planning_run(
            "job-1",
            client_name="Acme",
            summary="s",
            handoff_summary="hs",
            open_questions=[{"id": "q1"}],
            resolved_questions=[],
        )
        is False
    )


def test_record_planning_run_swallows_write_errors(monkeypatch) -> None:
    """A transient DB failure mid-write is swallowed, not raised."""

    class _BoomCursor:
        def execute(self, *args, **kwargs):
            raise RuntimeError("connection lost")

    @contextmanager
    def _fake_pg_cursor(*args, **kwargs):
        yield _BoomCursor()

    monkeypatch.setattr("planning_team.postgres.writer.pg_cursor", _fake_pg_cursor)

    assert (
        record_planning_run(
            "job-1",
            client_name="Acme",
            summary="s",
            handoff_summary="hs",
            open_questions=[],
            resolved_questions=[],
        )
        is False
    )


# ---------------------------------------------------------------------------
# Live-Postgres tests (skipped unless POSTGRES_HOST is set).
# ---------------------------------------------------------------------------


def test_record_planning_run_round_trip() -> None:
    from shared_postgres import get_conn, is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    from planning_team.postgres import SCHEMA
    from shared_postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    try:
        job_id = "writer-test-job-1"
        open_qs = [{"id": "q1", "question_text": "Scope?"}]
        resolved_qs = [{"question_id": "q1", "selected_option_id": "o1"}]

        assert (
            record_planning_run(
                job_id,
                client_name="Acme",
                summary="Planning completed; handoff package ready.",
                handoff_summary="Handoff package produced by Planning.",
                open_questions=open_qs,
                resolved_questions=resolved_qs,
            )
            is True
        )

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT client_name, summary, handoff_summary, open_questions, "
                "resolved_questions FROM planning_runs WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()

        assert row is not None
        client_name, summary, handoff_summary, open_questions, resolved_questions = row
        assert client_name == "Acme"
        assert summary == "Planning completed; handoff package ready."
        assert handoff_summary == "Handoff package produced by Planning."
        assert open_questions == open_qs
        assert resolved_questions == resolved_qs
    finally:
        truncate_team_tables(SCHEMA)


def test_record_planning_run_idempotent_on_conflict() -> None:
    from shared_postgres import get_conn, is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    from planning_team.postgres import SCHEMA
    from shared_postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    try:
        job_id = "writer-test-job-2"
        first = record_planning_run(
            job_id,
            client_name="A",
            summary="s1",
            handoff_summary="hs1",
            open_questions=[],
            resolved_questions=[],
        )
        second = record_planning_run(
            job_id,
            client_name="B",
            summary="s2",
            handoff_summary="hs2",
            open_questions=[{"id": "changed"}],
            resolved_questions=[],
        )
        assert first is True
        assert second is True  # ON CONFLICT DO NOTHING: no error on retry

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM planning_runs WHERE job_id = %s", (job_id,))
            (count,) = cur.fetchone()
        assert count == 1  # second call did not insert a duplicate row
    finally:
        truncate_team_tables(SCHEMA)
