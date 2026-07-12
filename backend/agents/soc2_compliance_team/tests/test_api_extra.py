"""Additional API tests for paths not covered by ``test_api.py``:

* ``_now`` timestamp helper
* ``mark_all_running_jobs_failed`` (both success and failure paths)
* ``_run_audit_job`` failure branch (audit run raises)

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
