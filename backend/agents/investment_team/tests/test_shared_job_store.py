"""Coverage for ``investment_team.shared.job_store``.

The module is a thin facade over ``JobServiceClient`` constants and
methods. Tests patch the underlying client to verify the facade forwards
arguments and applies the small amount of policy (cancel-eligibility,
cancellation check).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


class _FakeClient:
    """Minimal in-memory job service for the facade tests."""

    def __init__(self, team: str = "x") -> None:
        self.team = team
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def create_job(self, job_id: str, *, status: str = "pending", **fields: Any) -> None:
        self.jobs[job_id] = {"job_id": job_id, "status": status, **fields}

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return dict(self.jobs[job_id]) if job_id in self.jobs else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        if job_id in self.jobs:
            self.jobs[job_id].update(fields)

    def update_job_if_not_cancelled(self, job_id: str, **fields: Any) -> Optional[bool]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.get("status") == "cancelled":
            return False
        job.update(fields)
        return True

    def cancel_active_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.get("status") not in ("pending", "running"):
            return False
        job["status"] = "cancelled"
        return True

    def list_jobs(self, *, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        jobs = list(self.jobs.values())
        if statuses:
            jobs = [j for j in jobs if j.get("status") in statuses]
        return jobs

    def delete_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    def mark_all_active_jobs_failed(self, reason: str) -> List[str]:
        marked = []
        for job_id, job in self.jobs.items():
            if job.get("status") in ("pending", "running"):
                job["status"] = "failed"
                job["error"] = reason
                marked.append(job_id)
        return marked


@pytest.fixture(autouse=True)
def _reset_module_client(monkeypatch: pytest.MonkeyPatch):
    """Force ``_client_instance`` to refresh from our fake every test."""
    from investment_team.shared import job_store

    fake = _FakeClient(team="investment_backtests")
    monkeypatch.setattr(job_store, "_client_instance", fake, raising=False)
    yield fake


def test_create_get_update_round_trip(_reset_module_client) -> None:
    from investment_team.shared.job_store import (
        JOB_STATUS_PENDING,
        JOB_STATUS_RUNNING,
        create_job,
        get_job,
        update_job,
    )

    create_job("j1", strategy_id="s1")
    job = get_job("j1")
    assert job is not None
    assert job["status"] == JOB_STATUS_PENDING

    update_job("j1", status=JOB_STATUS_RUNNING)
    assert get_job("j1")["status"] == JOB_STATUS_RUNNING


def test_get_job_returns_none_for_missing(_reset_module_client) -> None:
    from investment_team.shared.job_store import get_job

    assert get_job("missing") is None


def test_list_jobs_with_status_filter(_reset_module_client) -> None:
    from investment_team.shared.job_store import (
        JOB_STATUS_FAILED,
        JOB_STATUS_PENDING,
        create_job,
        list_jobs,
        update_job,
    )

    create_job("j1")
    create_job("j2")
    update_job("j2", status=JOB_STATUS_FAILED)

    assert len(list_jobs()) == 2
    assert [j["job_id"] for j in list_jobs(statuses=[JOB_STATUS_PENDING])] == ["j1"]
    assert [j["job_id"] for j in list_jobs(statuses=[JOB_STATUS_FAILED])] == ["j2"]


def test_cancel_job_only_cancels_active(_reset_module_client) -> None:
    from investment_team.shared.job_store import (
        JOB_STATUS_CANCELLED,
        JOB_STATUS_COMPLETED,
        JOB_STATUS_RUNNING,
        cancel_job,
        create_job,
        get_job,
        update_job,
    )

    create_job("j1")
    assert cancel_job("j1") is True
    assert get_job("j1")["status"] == JOB_STATUS_CANCELLED

    # Already cancelled — not re-cancellable.
    assert cancel_job("j1") is False

    create_job("j2")
    update_job("j2", status=JOB_STATUS_COMPLETED)
    assert cancel_job("j2") is False

    # Running jobs ARE cancellable.
    create_job("j3")
    update_job("j3", status=JOB_STATUS_RUNNING)
    assert cancel_job("j3") is True


def test_cancel_job_returns_false_for_missing_id(_reset_module_client) -> None:
    from investment_team.shared.job_store import cancel_job

    assert cancel_job("never-existed") is False


def test_is_job_cancelled_states(_reset_module_client) -> None:
    from investment_team.shared.job_store import (
        cancel_job,
        create_job,
        is_job_cancelled,
    )

    assert is_job_cancelled("missing") is False

    create_job("j1")
    assert is_job_cancelled("j1") is False

    cancel_job("j1")
    assert is_job_cancelled("j1") is True


def test_delete_job_returns_bool(_reset_module_client) -> None:
    from investment_team.shared.job_store import create_job, delete_job

    create_job("j1")
    assert delete_job("j1") is True
    assert delete_job("j1") is False


def test_mark_all_running_jobs_failed_marks_only_active(_reset_module_client) -> None:
    from investment_team.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_RUNNING,
        create_job,
        get_job,
        mark_all_running_jobs_failed,
        update_job,
    )

    create_job("j1")  # stays pending
    create_job("j2")
    update_job("j2", status=JOB_STATUS_RUNNING)
    create_job("j3")
    update_job("j3", status=JOB_STATUS_COMPLETED)

    mark_all_running_jobs_failed("server shutdown")

    assert get_job("j1")["status"] == "failed"
    assert get_job("j2")["status"] == "failed"
    assert get_job("j3")["status"] == JOB_STATUS_COMPLETED  # untouched


def test_mark_all_running_jobs_failed_never_raises_on_client_error(
    _reset_module_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from investment_team.shared import job_store

    def _boom(self, reason: str) -> None:
        raise RuntimeError("job service unreachable")

    monkeypatch.setattr(type(_reset_module_client), "mark_all_active_jobs_failed", _boom)
    job_store.mark_all_running_jobs_failed("server shutdown")  # must not raise


def test_client_factory_lazy_creates_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call constructs the JobServiceClient lazily, subsequent calls reuse it."""
    from investment_team.shared import job_store

    constructed: List[str] = []

    class _Stub:
        def __init__(self, team: str = "x") -> None:
            constructed.append(team)

    monkeypatch.setattr(job_store, "JobServiceClient", _Stub)
    monkeypatch.setattr(job_store, "_client_instance", None)
    job_store._client()
    job_store._client()
    assert constructed == ["investment_backtests"]
