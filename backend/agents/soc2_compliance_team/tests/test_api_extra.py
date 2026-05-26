"""Additional API tests for paths not covered by ``test_api.py``:

* ``_now`` timestamp helper
* ``mark_all_running_jobs_failed`` (both success and failure paths)
* ``_run_audit_job`` failure branch (audit run raises)
* ``run_audit`` Temporal branch (when Temporal is enabled)
"""

from __future__ import annotations

import time
from pathlib import Path
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


# ---------------------------------------------------------------------------
# run_audit — Temporal-enabled branch
# ---------------------------------------------------------------------------


def test_run_audit_uses_temporal_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``is_temporal_enabled`` returns True and ``start_audit_workflow``
    succeeds, the route should return immediately with the Temporal-specific
    message without spawning a thread."""
    (tmp_path / "x.py").write_text("a=1")

    import soc2_compliance_team.temporal.client as cmod
    import soc2_compliance_team.temporal.start_workflow as swmod

    captured: dict[str, Any] = {}

    def _fake_enabled():
        return True

    def _fake_start(job_id, repo_path):
        captured["job_id"] = job_id
        captured["repo_path"] = repo_path

    monkeypatch.setattr(cmod, "is_temporal_enabled", _fake_enabled)
    monkeypatch.setattr(swmod, "start_audit_workflow", _fake_start)
    # Also stub the threading branch so a test failure on the wrong branch
    # is loud rather than the thread silently doing real work.

    def _no_thread(*a, **k):
        raise AssertionError("Should not spawn a thread when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert "(Temporal)" in body["message"]
    assert captured["job_id"] == body["job_id"]
    assert captured["repo_path"] == str(tmp_path.resolve())


def test_run_audit_falls_back_to_thread_on_temporal_import_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _patched
) -> None:
    """If the temporal modules can't be imported, the route should fall
    back to the threaded branch."""
    (tmp_path / "x.py").write_text("a=1")

    real_import = (
        __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    )

    def _fake_import(name, *args, **kwargs):
        if name.startswith("soc2_compliance_team.temporal"):
            raise ImportError("temporal not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    # Stub the orchestrator so the background thread does no real work.
    from soc2_compliance_team.models import SOC2AuditResult

    class _Orch:
        def run(self, repo_path):
            return SOC2AuditResult(status="completed", repo_path=str(repo_path))

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _Orch)

    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    # Threaded message (no "Temporal" suffix)
    assert "(Temporal)" not in body["message"]

    # Wait for the background thread to finish *while the stubs are still
    # active*, so the real orchestrator / job-manager paths are never
    # reached after fixture teardown.
    job_id = body["job_id"]
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        job = _patched.get_job(job_id)
        if job and job.get("status") in ("completed", "failed"):
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"Audit job {job_id} did not reach a terminal state in 2s")
