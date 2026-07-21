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

import planning_team.postgres.writer as writer
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


def test_record_planning_run_noop_without_postgres(monkeypatch) -> None:
    # Explicitly disable Postgres rather than relying on the runner's ambient env:
    # a CI job that also runs the live-Postgres tests below has POSTGRES_HOST set,
    # which would make this assertion fail (and leak a row) if left implicit.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
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
    """An operational failure mid-write (e.g. a cancelled/stalled statement) is
    swallowed, not raised — with Postgres reported enabled so the failure comes
    from the write path itself, not the disabled-Postgres no-op."""
    monkeypatch.setattr(writer, "is_postgres_enabled", lambda: True)

    @contextmanager
    def _fake_get_conn(*args, **kwargs):
        yield object()

    class _BoomCursor:
        def execute(self, *args, **kwargs):
            raise RuntimeError("connection lost")

    @contextmanager
    def _fake_probe_cursor(*args, **kwargs):
        yield _BoomCursor()

    monkeypatch.setattr(writer, "get_conn", _fake_get_conn)
    monkeypatch.setattr(writer, "probe_cursor", _fake_probe_cursor)

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


def test_record_planning_run_bounds_the_insert_with_a_statement_timeout(monkeypatch) -> None:
    """The audit INSERT runs under probe_cursor's transaction-local
    statement_timeout, so a post-connect stall or an ON CONFLICT lock wait can't
    block the caller indefinitely."""
    monkeypatch.setattr(writer, "is_postgres_enabled", lambda: True)
    captured = {}

    @contextmanager
    def _fake_get_conn(*args, **kwargs):
        yield object()

    @contextmanager
    def _fake_probe_cursor(conn, *, timeout_s):
        captured["timeout_s"] = timeout_s

        class _NoOpCursor:
            def execute(self, *args, **kwargs):
                pass

        yield _NoOpCursor()

    monkeypatch.setattr(writer, "get_conn", _fake_get_conn)
    monkeypatch.setattr(writer, "probe_cursor", _fake_probe_cursor)

    assert (
        record_planning_run(
            "job-1",
            client_name=None,
            summary="s",
            handoff_summary="hs",
            open_questions=[],
            resolved_questions=[],
        )
        is True
    )
    assert captured["timeout_s"] == writer._AUDIT_WRITE_TIMEOUT_S


def test_record_planning_run_bounds_the_whole_operation(monkeypatch) -> None:
    """probe_cursor only bounds the statement once a connection exists — a wedged
    pool acquisition (or a fully dead socket) never even reaches it. Simulate that
    by hanging inside get_conn() itself and prove record_planning_run still
    returns False promptly, bounded by _AUDIT_OPERATION_BUDGET_S end-to-end rather
    than blocking for the full simulated hang."""
    import time

    monkeypatch.setattr(writer, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(writer, "_AUDIT_OPERATION_BUDGET_S", 0.2)

    @contextmanager
    def _wedged_get_conn(*args, **kwargs):
        time.sleep(1.5)  # simulates a stalled pool acquisition / dead socket
        yield object()

    monkeypatch.setattr(writer, "get_conn", _wedged_get_conn)

    start = time.monotonic()
    result = record_planning_run(
        "job-1",
        client_name=None,
        summary="s",
        handoff_summary="hs",
        open_questions=[],
        resolved_questions=[],
    )
    elapsed = time.monotonic() - start

    assert result is False
    # Returned at ~budget, NOT after the 1.5s simulated hang (proves the
    # end-to-end bound actually works, not just the statement-level one).
    assert elapsed < 1.0


def test_record_planning_run_swallows_bounded_probe_setup_failure(monkeypatch) -> None:
    """A failure in the bounded_probe machinery itself (not just in the write
    callable) is swallowed by the outer guard too — never raised."""
    monkeypatch.setattr(writer, "is_postgres_enabled", lambda: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("event loop setup failed")

    monkeypatch.setattr(writer, "bounded_probe", _boom)

    assert (
        record_planning_run(
            "job-1",
            client_name=None,
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
    from shared.postgres import get_conn, is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    from planning_team.postgres import SCHEMA
    from shared.postgres.testing import truncate_team_tables

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
    from shared.postgres import get_conn, is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    from planning_team.postgres import SCHEMA
    from shared.postgres.testing import truncate_team_tables

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
