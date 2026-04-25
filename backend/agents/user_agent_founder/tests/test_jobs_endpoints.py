"""Tests for the centralized-jobs endpoints on the user_agent_founder API.

These drive ``start_founder_workflow``, ``resume_job``, ``restart_job``,
``cancel_job``, and ``delete_job`` directly (not through FastAPI TestClient)
so Postgres, Temporal, and the orchestrator can all be stubbed out.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeJobStore:
    """In-memory stand-in for ``user_agent_founder.shared.job_store``."""

    RESUMABLE_STATUSES = frozenset({"pending", "running", "failed", "interrupted", "agent_crash"})
    RESTARTABLE_STATUSES = frozenset(
        {"completed", "failed", "cancelled", "interrupted", "agent_crash"}
    )

    def __init__(self) -> None:
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.create_calls: list[tuple[str, dict]] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.reset_calls: list[str] = []

    # API mirrors ``shared.job_store``
    def create_job(self, job_id: str, *, status: str = "pending", **fields: Any) -> None:
        self.create_calls.append((job_id, {"status": status, **fields}))
        self.jobs[job_id] = {"job_id": job_id, "status": status, **fields}

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        self.update_calls.append((job_id, dict(fields)))
        if job_id in self.jobs:
            self.jobs[job_id].update(fields)

    def delete_job(self, job_id: str) -> bool:
        self.delete_calls.append(job_id)
        return self.jobs.pop(job_id, None) is not None

    def reset_job(self, job_id: str) -> None:
        self.reset_calls.append(job_id)
        if job_id in self.jobs:
            self.jobs[job_id].update(
                {"status": "pending", "error": None, "current_phase": "starting"}
            )

    def list_jobs(self, statuses: Optional[list[str]] = None) -> list[Dict[str, Any]]:
        if statuses is None:
            return list(self.jobs.values())
        return [j for j in self.jobs.values() if j.get("status") in set(statuses)]

    @staticmethod
    def validate_job_for_action(
        job_data: Optional[Dict[str, Any]],
        job_id: str,
        allowed_statuses: frozenset,
        action_label: str = "action",
    ) -> Dict[str, Any]:
        if not job_data:
            raise ValueError(f"Job {job_id} not found")
        status = job_data.get("status", "pending")
        if status not in allowed_statuses:
            raise ValueError(f"Job cannot be {action_label} (status={status}).")
        return job_data


@pytest.fixture
def fake_job_store(monkeypatch):
    """Patch ``user_agent_founder.shared.job_store`` with an in-memory fake."""
    import user_agent_founder.shared.job_store as real_job_store

    fake = FakeJobStore()
    for attr in (
        "create_job",
        "get_job",
        "update_job",
        "delete_job",
        "reset_job",
        "list_jobs",
        "validate_job_for_action",
        "RESUMABLE_STATUSES",
        "RESTARTABLE_STATUSES",
    ):
        monkeypatch.setattr(
            real_job_store,
            attr,
            getattr(fake, attr) if not attr.endswith("STATUSES") else getattr(fake, attr),
        )
    return fake


@pytest.fixture
def fake_store(monkeypatch):
    """Patch the founder Postgres store with a MagicMock."""
    from user_agent_founder.api import main as api_main

    store = MagicMock()
    store.create_run.return_value = "run-123"
    store.delete_run.return_value = True
    monkeypatch.setattr(api_main, "get_founder_store", lambda: store)
    return store


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Patch the dispatcher so we never touch Temporal or spawn a real thread."""
    from user_agent_founder.api import main as api_main

    dispatched: list[str] = []

    def _dispatch(run_id: str) -> str:
        dispatched.append(run_id)
        return "thread"

    monkeypatch.setattr(api_main, "_dispatch_founder_run", _dispatch)
    return dispatched


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def test_start_creates_job_and_dispatches(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import start_founder_workflow

    resp = start_founder_workflow()

    assert resp.job_id == "run-123"
    assert resp.status == "running"
    assert fake_dispatch == ["run-123"]
    assert fake_job_store.create_calls == [
        (
            "run-123",
            {
                "status": "running",
                "label": "Testing Personas workflow",
                "current_phase": "starting",
            },
        )
    ]
    assert fake_job_store.jobs["run-123"]["status"] == "running"
    # Default target_team_key is recorded on create_run.
    fake_store.create_run.assert_called_once_with(target_team_key="software_engineering")


def test_start_passes_explicit_target_team_key_through(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import StartRunRequest, start_founder_workflow

    resp = start_founder_workflow(StartRunRequest(target_team_key="software_engineering"))

    assert resp.job_id == "run-123"
    fake_store.create_run.assert_called_once_with(target_team_key="software_engineering")


def test_start_rejects_unknown_target_team_key(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import StartRunRequest, start_founder_workflow

    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(StartRunRequest(target_team_key="not_a_real_team"))
    assert excinfo.value.status_code == 400
    # No dispatch when validation rejects the request.
    assert fake_dispatch == []


def test_start_marks_job_failed_when_dispatch_raises(fake_job_store, fake_store, monkeypatch):
    from user_agent_founder.api import main as api_main

    def _boom(run_id: str) -> str:
        raise RuntimeError("no worker")

    monkeypatch.setattr(api_main, "_dispatch_founder_run", _boom)

    with pytest.raises(HTTPException) as excinfo:
        api_main.start_founder_workflow()

    assert excinfo.value.status_code == 500
    assert fake_job_store.jobs["run-123"]["status"] == "failed"
    assert "no worker" in fake_job_store.jobs["run-123"]["error"]


# ---------------------------------------------------------------------------
# /job/{id}/resume
# ---------------------------------------------------------------------------


def test_resume_rejects_missing_job(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import resume_job

    with pytest.raises(HTTPException) as excinfo:
        resume_job("missing")
    assert excinfo.value.status_code == 404
    assert fake_dispatch == []


def test_resume_rejects_completed_job(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import resume_job

    fake_job_store.create_job("run-done", status="completed")

    with pytest.raises(HTTPException) as excinfo:
        resume_job("run-done")
    assert excinfo.value.status_code == 400
    assert fake_dispatch == []


def test_resume_redispatches_failed_job(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import resume_job

    fake_job_store.create_job("run-bad", status="failed", error="boom")

    resp = resume_job("run-bad")

    assert resp.job_id == "run-bad"
    assert fake_dispatch == ["run-bad"]
    assert fake_job_store.jobs["run-bad"]["status"] == "running"
    assert fake_job_store.jobs["run-bad"]["error"] is None


def test_resume_mirrors_dispatch_failure_into_founder_store(
    fake_job_store, fake_store, monkeypatch
):
    """Codex P1: if redispatch raises, both the central job and the founder
    store row must be marked failed — otherwise the Testing Personas dashboard
    leaves the run at ``pending`` in its Running section."""
    from user_agent_founder.api import main as api_main

    fake_job_store.create_job("run-bad", status="failed", error="boom")
    monkeypatch.setattr(
        api_main,
        "_dispatch_founder_run",
        lambda _run_id: (_ for _ in ()).throw(RuntimeError("no worker")),
    )

    with pytest.raises(HTTPException) as excinfo:
        api_main.resume_job("run-bad")

    assert excinfo.value.status_code == 500
    assert fake_job_store.jobs["run-bad"]["status"] == "failed"
    assert "no worker" in fake_job_store.jobs["run-bad"]["error"]
    fake_store.update_run.assert_any_call(
        "run-bad", status="failed", error="Resume dispatch failed: no worker"
    )


# ---------------------------------------------------------------------------
# /job/{id}/restart
# ---------------------------------------------------------------------------


def test_restart_rejects_running_job(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import restart_job

    fake_job_store.create_job("run-live", status="running")

    with pytest.raises(HTTPException) as excinfo:
        restart_job("run-live")
    assert excinfo.value.status_code == 400
    assert fake_dispatch == []


def test_restart_resets_and_redispatches_completed_job(fake_job_store, fake_store, fake_dispatch):
    from user_agent_founder.api.main import restart_job

    fake_job_store.create_job("run-done", status="completed", error=None)

    resp = restart_job("run-done")

    assert resp.job_id == "run-done"
    assert fake_dispatch == ["run-done"]
    assert "run-done" in fake_job_store.reset_calls
    assert fake_job_store.jobs["run-done"]["status"] == "running"


def test_restart_clears_founder_store_checkpoint_columns(fake_job_store, fake_store, fake_dispatch):
    """Restart must NULL every column the resume short-circuit reads, otherwise
    a restarted run skips spec/analysis or polls a stale SE job id (#347)."""
    from user_agent_founder.api.main import restart_job

    fake_job_store.create_job("run-done", status="completed", error=None)

    restart_job("run-done")

    fake_store.update_run.assert_any_call(
        "run-done",
        status="pending",
        error=None,
        spec_content=None,
        analysis_job_id=None,
        repo_path=None,
        se_job_id=None,
    )


def test_restart_mirrors_dispatch_failure_into_founder_store(
    fake_job_store, fake_store, monkeypatch
):
    """Codex P1: same invariant as test_resume_mirrors_dispatch_failure_into_founder_store
    but for /job/{id}/restart."""
    from user_agent_founder.api import main as api_main

    fake_job_store.create_job("run-done", status="completed", error=None)
    monkeypatch.setattr(
        api_main,
        "_dispatch_founder_run",
        lambda _run_id: (_ for _ in ()).throw(RuntimeError("no worker")),
    )

    with pytest.raises(HTTPException) as excinfo:
        api_main.restart_job("run-done")

    assert excinfo.value.status_code == 500
    assert fake_job_store.jobs["run-done"]["status"] == "failed"
    assert "no worker" in fake_job_store.jobs["run-done"]["error"]
    fake_store.update_run.assert_any_call(
        "run-done", status="failed", error="Restart dispatch failed: no worker"
    )


# ---------------------------------------------------------------------------
# /job/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_rejects_missing_job(fake_job_store, fake_store):
    from user_agent_founder.api.main import cancel_job

    with pytest.raises(HTTPException) as excinfo:
        cancel_job("ghost")
    assert excinfo.value.status_code == 404


def test_cancel_rejects_completed_job(fake_job_store, fake_store):
    from user_agent_founder.api.main import cancel_job

    fake_job_store.create_job("run-done", status="completed")
    with pytest.raises(HTTPException) as excinfo:
        cancel_job("run-done")
    assert excinfo.value.status_code == 400


def test_cancel_updates_job_and_store(fake_job_store, fake_store):
    from user_agent_founder.api.main import cancel_job

    fake_job_store.create_job("run-live", status="running")

    result = cancel_job("run-live")

    assert result == {"status": "cancelled", "job_id": "run-live"}
    assert fake_job_store.jobs["run-live"]["status"] == "cancelled"
    fake_store.update_run.assert_called_once_with(
        "run-live", status="failed", error="Cancelled by user"
    )


# ---------------------------------------------------------------------------
# DELETE /job/{id}
# ---------------------------------------------------------------------------


def test_delete_returns_404_when_missing(fake_job_store, fake_store):
    from user_agent_founder.api.main import delete_job

    with pytest.raises(HTTPException) as excinfo:
        delete_job("ghost")
    assert excinfo.value.status_code == 404


def test_delete_removes_from_both_stores(fake_job_store, fake_store):
    from user_agent_founder.api.main import delete_job

    fake_job_store.create_job("run-done", status="completed")

    result = delete_job("run-done")

    assert result == {"deleted": "true", "job_id": "run-done"}
    assert "run-done" in fake_job_store.delete_calls
    fake_store.delete_run.assert_called_once_with("run-done")
