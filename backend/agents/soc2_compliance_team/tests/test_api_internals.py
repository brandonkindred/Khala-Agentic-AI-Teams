"""Tests for ``api/main.py``'s internal helpers, not its HTTP endpoints
(``test_api.py`` covers the endpoint-level behavior):

* ``_now`` timestamp helper
* ``mark_all_running_jobs_failed`` (both success and failure paths)
* ``_run_audit_job`` (thread-mode job runner, both branches)
* ``_start_temporal_worker_backstop``
* ``_job_is_terminal`` / ``_update_job_terminal`` / ``_update_job_unless_terminal``
  (the terminal-status write guards)

(The ``run_audit`` Temporal vs thread dispatch branch is covered by
``test_temporal_dispatch.py``.)
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from soc2_compliance_team.api import main as api_main

client = TestClient(api_main.app)


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    return fake_job_client


# ---------------------------------------------------------------------------
# _now
# ---------------------------------------------------------------------------


def test_now_returns_iso_string() -> None:
    out = api_main._now()
    assert isinstance(out, str)
    # ISO 8601: at minimum contains "T" and ends with timezone info
    assert "T" in out
    # UTC offset: +00:00 OR 'Z'
    assert out.endswith("+00:00") or out.endswith("Z")


# ---------------------------------------------------------------------------
# mark_all_running_jobs_failed
# ---------------------------------------------------------------------------


def test_mark_all_running_jobs_failed_delegates_to_job_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeJM:
        def mark_all_active_jobs_failed(self, reason):
            captured["reason"] = reason

    monkeypatch.setattr(api_main, "_job_manager", _FakeJM())
    api_main.mark_all_running_jobs_failed("shutdown")
    assert captured["reason"] == "shutdown"


def test_mark_all_running_jobs_failed_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    class _FakeJM:
        def mark_all_active_jobs_failed(self, reason):
            raise RuntimeError("db down")

    monkeypatch.setattr(api_main, "_job_manager", _FakeJM())
    with caplog.at_level("WARNING"):
        api_main.mark_all_running_jobs_failed("any")
    assert any("mark_all_running_jobs_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _run_audit_job — direct (sync) invocation, both branches
# ---------------------------------------------------------------------------


def test_run_audit_job_marks_completed(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    from soc2_compliance_team.models import SOC2AuditResult

    fake_job_client.create_job("j1", status="pending")

    class _FakeOrch:
        def run(self, repo_path):
            return SOC2AuditResult(status="completed", repo_path=str(repo_path))

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _FakeOrch)

    api_main._run_audit_job("j1", "/some/repo")

    job = fake_job_client.get_job("j1")
    assert job["status"] == "completed"
    assert job["current_stage"] == "Completed"
    assert job["result"]["status"] == "completed"


def test_run_audit_job_failure_branch(
    monkeypatch: pytest.MonkeyPatch, fake_job_client, caplog
) -> None:
    fake_job_client.create_job("j2", status="pending")

    class _BoomOrch:
        def run(self, repo_path):
            raise RuntimeError("audit boom")

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _BoomOrch)

    with caplog.at_level("ERROR"):
        api_main._run_audit_job("j2", "/some/repo")

    job = fake_job_client.get_job("j2")
    assert job["status"] == "failed"
    assert job["current_stage"] == "Failed"
    assert "audit boom" in job["error"]


def test_run_audit_job_marks_failed_when_orchestrator_returns_failed_result(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """``SOC2AuditOrchestrator.run`` catches its own internal failures and
    *returns* a ``status="failed"`` result rather than raising — the job's
    terminal status must follow ``result.status``, not be hardcoded to
    "completed" just because no exception propagated."""
    from soc2_compliance_team.models import SOC2AuditResult

    fake_job_client.create_job("j3", status="pending")

    class _FailedOrch:
        def run(self, repo_path):
            return SOC2AuditResult(
                status="failed", repo_path=str(repo_path), error="criteria audit timed out"
            )

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _FailedOrch)

    api_main._run_audit_job("j3", "/some/repo")

    job = fake_job_client.get_job("j3")
    assert job["status"] == "failed"
    assert job["current_stage"] == "Failed"
    assert job["error"] == "criteria audit timed out"
    assert job["result"]["status"] == "failed"


# ---------------------------------------------------------------------------
# _start_temporal_worker_backstop
# ---------------------------------------------------------------------------


def test_temporal_worker_backstop_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Temporal disabled the backstop delegates and does not raise."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    # start_soc2_temporal_worker_thread() returns False when disabled; the
    # backstop must complete cleanly.
    api_main._start_temporal_worker_backstop()


def test_temporal_worker_backstop_swallows_errors(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """A worker-boot failure is logged, never propagated (must not abort boot)."""

    def _boom() -> bool:
        raise RuntimeError("worker boom")

    monkeypatch.setattr(
        "soc2_compliance_team.temporal.worker.start_soc2_temporal_worker_thread", _boom
    )
    with caplog.at_level("WARNING"):
        api_main._start_temporal_worker_backstop()
    assert any("backstop failed to start" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _job_is_terminal / _update_job_terminal / _update_job_unless_terminal
# ---------------------------------------------------------------------------


def test_job_is_terminal_true_for_completed_and_failed(fake_job_client) -> None:
    fake_job_client.create_job("t1", status="completed")
    fake_job_client.create_job("t2", status="failed")
    assert api_main._job_is_terminal("t1") is True
    assert api_main._job_is_terminal("t2") is True


def test_job_is_terminal_false_for_pending_running_or_missing(fake_job_client) -> None:
    fake_job_client.create_job("t3", status="pending")
    fake_job_client.create_job("t4", status="running")
    assert api_main._job_is_terminal("t3") is False
    assert api_main._job_is_terminal("t4") is False
    assert api_main._job_is_terminal("does-not-exist") is False


def test_update_job_terminal_applies_when_not_terminal(fake_job_client) -> None:
    fake_job_client.create_job("g1", status="running")
    api_main._update_job_terminal("g1", status="completed", result={"status": "completed"})
    assert fake_job_client.get_job("g1")["status"] == "completed"


def test_update_job_terminal_skips_when_already_terminal(fake_job_client, caplog) -> None:
    """Whichever terminal write lands first must stick — a later one is a no-op."""
    fake_job_client.create_job("g2", status="completed", result={"status": "completed"})
    with caplog.at_level("WARNING"):
        api_main._update_job_terminal("g2", status="failed", error="too late")
    job = fake_job_client.get_job("g2")
    assert job["status"] == "completed"
    assert "error" not in job
    assert any("already terminal" in r.message for r in caplog.records)


def test_job_is_terminal_defaults_to_false_on_read_error(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    class _BoomJM:
        def get_job(self, job_id):
            raise RuntimeError("job store down")

    monkeypatch.setattr(api_main, "_job_manager", _BoomJM())
    with caplog.at_level("WARNING"):
        assert api_main._job_is_terminal("t5") is False
    assert any("Could not read job" in r.message for r in caplog.records)


def test_update_job_unless_terminal_applies_when_not_terminal(fake_job_client) -> None:
    fake_job_client.create_job("u1", status="running")
    api_main._update_job_unless_terminal("u1", current_stage="Loading repository")
    assert fake_job_client.get_job("u1")["current_stage"] == "Loading repository"


def test_update_job_unless_terminal_skips_when_already_terminal(fake_job_client, caplog) -> None:
    """A workflow that keeps running server-side after run_audit already wrote
    a terminal ``failed`` status (a lost dispatch ack) must not have its
    non-terminal ``status="running"`` write resurrect the job."""
    fake_job_client.create_job("u2", status="failed", error="dispatch timed out")
    with caplog.at_level("WARNING"):
        api_main._update_job_unless_terminal(
            "u2", status="running", current_stage="Loading repository"
        )
    job = fake_job_client.get_job("u2")
    assert job["status"] == "failed"
    assert "current_stage" not in job
    assert any("already terminal" in r.message for r in caplog.records)


def test_stale_job_threshold_covers_longest_temporal_activity_ceiling() -> None:
    """The stale-job monitor's threshold must stay comfortably above the
    decomposed Temporal pipeline's longest schedule-to-close ceiling (the
    criterion fan-out / report-writing activities can each go up to an hour
    with no job-row touch) — otherwise it can mark a legitimate long-running
    audit "failed (stale)" before it finishes, and the terminal-write guard
    would then treat that false failure as authoritative."""
    from soc2_compliance_team.temporal import workflows as wmod

    assert (
        api_main._STALE_JOB_THRESHOLD_SECONDS > wmod.AUDIT_SCHEDULE_TO_CLOSE_TIMEOUT.total_seconds()
    )
    assert (
        api_main._STALE_JOB_THRESHOLD_SECONDS
        > wmod.REPORT_SCHEDULE_TO_CLOSE_TIMEOUT.total_seconds()
    )
