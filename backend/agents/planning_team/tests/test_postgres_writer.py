"""Tests for the Planning team's best-effort ``planning_runs`` audit writer.

* No-op tests run unconditionally (no Postgres required) and prove
  ``record_planning_run`` never raises for operational failures and returns
  ``False`` when Postgres is disabled — mirroring the acceptance criterion
  that the write is a pure no-op without ``POSTGRES_HOST``.
* Live-Postgres tests are skipped unless ``POSTGRES_HOST`` is set; they prove
  a row is actually written (and upserted, not conflict-failed, on retry),
  mirroring ``test_postgres_schema.py`` / ``product_delivery`` store tests.
"""

from __future__ import annotations

import pytest

from planning_team.postgres import SCHEMA
from planning_team.postgres.writer import record_planning_run

# ---------------------------------------------------------------------------
# No-op tests (no Postgres required).
# ---------------------------------------------------------------------------


def test_record_planning_run_is_noop_without_postgres(monkeypatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    result = record_planning_run(
        "job-1",
        client_name="Acme",
        summary="Planning completed; handoff package ready.",
        handoff_summary="Handoff package produced by Planning.",
        open_questions=[{"id": "q1", "question_text": "Scope?"}],
        resolved_questions=[],
    )

    assert result is False


def test_record_planning_run_tolerates_falsy_optional_fields(monkeypatch) -> None:
    """None/empty optional fields must not raise even when Postgres is disabled."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    result = record_planning_run(
        "job-2",
        client_name=None,
        summary="",
        handoff_summary="",
        open_questions=[],
        resolved_questions=[],
    )

    assert result is False


def test_record_planning_run_raises_on_empty_job_id() -> None:
    with pytest.raises(ValueError):
        record_planning_run(
            "",
            client_name=None,
            summary="",
            handoff_summary="",
            open_questions=[],
            resolved_questions=[],
        )


def test_record_planning_run_swallows_operational_failure(monkeypatch) -> None:
    """A write failure while Postgres is otherwise reachable is swallowed, not raised —
    the guarantee a finalize call site relies on to call this unconditionally."""
    import planning_team.postgres.writer as writer_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated connection failure")

    monkeypatch.setattr(writer_module, "pg_cursor", _boom)

    result = record_planning_run(
        "job-3",
        client_name="Acme",
        summary="s",
        handoff_summary="hs",
        open_questions=[],
        resolved_questions=[],
    )

    assert result is False


# ---------------------------------------------------------------------------
# Finalize call-site integration: the audit write must not alter the
# existing completion result (thread mode).
# ---------------------------------------------------------------------------


def test_run_workflow_background_completion_unchanged_by_audit_write(monkeypatch) -> None:
    """record_planning_run is a no-op here (POSTGRES_HOST unset); mark_job_completed's
    fields must be exactly what thread mode wrote before this change added the audit call."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    import planning_team.api.main as main_module

    handoff_package = {
        "summary": "Handoff package produced by Planning.",
        "open_questions": [{"id": "q1", "question_text": "Scope?"}],
        "resolved_questions": [],
    }
    fake_result = {
        "success": True,
        "handoff_package": handoff_package,
        "summary": "Planning completed; handoff package ready.",
    }

    completed_calls = []
    monkeypatch.setattr(main_module, "run_workflow", lambda **kw: fake_result)
    monkeypatch.setattr(main_module, "_get_llm", lambda: object())
    monkeypatch.setattr(main_module, "update_job", lambda job_id, **fields: None)
    monkeypatch.setattr(
        main_module,
        "mark_job_completed",
        lambda job_id, **fields: completed_calls.append((job_id, fields)),
    )
    monkeypatch.setattr(
        main_module,
        "mark_job_failed",
        lambda job_id, error: pytest.fail(f"unexpected failure: {error}"),
    )

    main_module.run_workflow_background(
        "job-thread-1", "/repo", "Acme", "brief", None, False, False
    )

    assert completed_calls == [
        (
            "job-thread-1",
            {
                "handoff_package": handoff_package,
                "summary": "Planning completed; handoff package ready.",
            },
        )
    ]


def test_run_workflow_background_survives_audit_write_failure(monkeypatch) -> None:
    """A failure inside the audit-write call must not cascade into the outer
    try/except and mark an already-COMPLETED job FAILED."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    import planning_team.api.main as main_module

    fake_result = {
        "success": True,
        "handoff_package": {"summary": "hp", "open_questions": [], "resolved_questions": []},
        "summary": "Planning completed; handoff package ready.",
    }

    monkeypatch.setattr(main_module, "run_workflow", lambda **kw: fake_result)
    monkeypatch.setattr(main_module, "_get_llm", lambda: object())
    monkeypatch.setattr(main_module, "update_job", lambda job_id, **fields: None)

    completed_calls = []
    monkeypatch.setattr(
        main_module,
        "mark_job_completed",
        lambda job_id, **fields: completed_calls.append((job_id, fields)),
    )
    failed_calls = []
    monkeypatch.setattr(
        main_module,
        "mark_job_failed",
        lambda job_id, error: failed_calls.append((job_id, error)),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated audit-write failure")

    monkeypatch.setattr(main_module, "record_planning_run", _boom)

    main_module.run_workflow_background(
        "job-thread-2", "/repo", "Acme", "brief", None, False, False
    )

    assert len(completed_calls) == 1
    assert failed_calls == []


def test_run_workflow_background_sources_questions_from_result_not_handoff(monkeypatch) -> None:
    """record_planning_run must receive the real open_questions/resolved_questions
    from result, not handoff_package's own (deliberately empty) copies of those
    same-named fields."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    import planning_team.api.main as main_module

    real_open_questions = [{"id": "q1", "question_text": "Scope?"}]
    real_resolved_questions = [{"question_id": "q0", "selected_option_id": "o1"}]
    fake_result = {
        "success": True,
        "handoff_package": {
            "summary": "hp",
            # Deliberately empty, mirroring orchestrator.py's real behavior.
            "open_questions": [],
            "resolved_questions": [],
        },
        "summary": "Planning completed; handoff package ready.",
        "open_questions": real_open_questions,
        "resolved_questions": real_resolved_questions,
    }

    monkeypatch.setattr(main_module, "run_workflow", lambda **kw: fake_result)
    monkeypatch.setattr(main_module, "_get_llm", lambda: object())
    monkeypatch.setattr(main_module, "update_job", lambda job_id, **fields: None)
    monkeypatch.setattr(main_module, "mark_job_completed", lambda job_id, **fields: None)
    monkeypatch.setattr(
        main_module, "mark_job_failed", lambda job_id, error: pytest.fail(f"unexpected: {error}")
    )

    record_calls = []
    monkeypatch.setattr(
        main_module,
        "record_planning_run",
        lambda job_id, **kw: record_calls.append((job_id, kw)),
    )

    main_module.run_workflow_background(
        "job-thread-3", "/repo", "Acme", "brief", None, False, False
    )

    assert len(record_calls) == 1
    _, kwargs = record_calls[0]
    assert kwargs["open_questions"] == real_open_questions
    assert kwargs["resolved_questions"] == real_resolved_questions


# ---------------------------------------------------------------------------
# Live-Postgres tests.
# ---------------------------------------------------------------------------


def test_record_planning_run_persists_row() -> None:
    from shared_postgres import get_conn, is_postgres_enabled, register_team_schemas
    from shared_postgres.testing import truncate_team_tables

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)

    open_questions = [{"id": "q1", "question_text": "Scope?", "options": []}]
    resolved_questions = [{"question_id": "q0", "selected_option_id": "o1"}]

    assert (
        record_planning_run(
            "job-live-1",
            client_name="Acme Corp",
            summary="Planning completed; handoff package ready.",
            handoff_summary="Handoff package produced by Planning.",
            open_questions=open_questions,
            resolved_questions=resolved_questions,
        )
        is True
    )

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, client_name, summary, handoff_summary, open_questions, resolved_questions "
            "FROM planning_runs WHERE job_id = %s",
            ("job-live-1",),
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == "job-live-1"
    assert row[1] == "Acme Corp"
    assert row[2] == "Planning completed; handoff package ready."
    assert row[3] == "Handoff package produced by Planning."
    assert row[4] == open_questions
    assert row[5] == resolved_questions


def test_record_planning_run_upserts_on_retry() -> None:
    """A Temporal retry calling this twice for the same job_id must update, not conflict-fail."""
    from shared_postgres import get_conn, is_postgres_enabled, register_team_schemas
    from shared_postgres.testing import truncate_team_tables

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)

    record_planning_run(
        "job-live-2",
        client_name="Acme Corp",
        summary="first attempt",
        handoff_summary="hp",
        open_questions=[],
        resolved_questions=[],
    )
    assert (
        record_planning_run(
            "job-live-2",
            client_name="Acme Corp",
            summary="second attempt",
            handoff_summary="hp",
            open_questions=[],
            resolved_questions=[],
        )
        is True
    )

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT summary FROM planning_runs WHERE job_id = %s", ("job-live-2",))
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "second attempt"


def test_record_planning_run_is_bounded_by_statement_timeout(monkeypatch) -> None:
    """A lock-contended write must be cancelled by the local statement_timeout, not
    hang indefinitely — pg_cursor's shared pool sets no default timeout, so without
    this bound a stalled/contended write could pin a worker forever (and never raise,
    so the except guard below would never even get a chance to help)."""
    import threading
    import time

    import planning_team.postgres.writer as writer_module
    from shared_postgres import get_conn, is_postgres_enabled, register_team_schemas
    from shared_postgres.testing import truncate_team_tables

    if not is_postgres_enabled():
        pytest.skip("POSTGRES_HOST not set; skipping live-Postgres writer test")

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)

    # A short bound so the test doesn't have to wait out the real default (5s).
    monkeypatch.setattr(writer_module, "statement_timeout_ms", lambda: 200)

    record_planning_run(
        "job-lock",
        client_name=None,
        summary="first",
        handoff_summary="",
        open_questions=[],
        resolved_questions=[],
    )

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def _hold_row_lock() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM planning_runs WHERE job_id = %s FOR UPDATE", ("job-lock",))
            lock_acquired.set()
            release_lock.wait(timeout=5)

    holder = threading.Thread(target=_hold_row_lock, daemon=True)
    holder.start()
    try:
        assert lock_acquired.wait(timeout=2), "lock-holder thread failed to acquire the row lock"

        start = time.monotonic()
        # ON CONFLICT DO UPDATE contends on the row FOR UPDATE holds; without the
        # statement_timeout this call would block until release_lock fires (~5s).
        result = record_planning_run(
            "job-lock",
            client_name=None,
            summary="second",
            handoff_summary="",
            open_questions=[],
            resolved_questions=[],
        )
        elapsed = time.monotonic() - start
    finally:
        release_lock.set()
        holder.join(timeout=5)

    assert result is False
    assert elapsed < 2.0, f"write should have been cancelled by statement_timeout, took {elapsed}s"
