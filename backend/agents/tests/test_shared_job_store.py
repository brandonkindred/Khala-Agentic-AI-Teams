"""Unit tests for the shared BaseJobStore (no network — JobServiceClient is faked)."""

import shared_job_store
from shared_job_store import (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    BaseJobStore,
)


class _FakeClient:
    """Minimal in-memory stand-in for JobServiceClient."""

    def __init__(self, team: str) -> None:
        self.team = team
        self.jobs: dict = {}
        self.failed_reason: str | None = None

    def create_job(self, job_id, **fields):
        self.jobs[job_id] = dict(fields)

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def update_job(self, job_id, **fields):
        self.jobs.setdefault(job_id, {}).update(fields)

    def list_jobs(self, statuses=None):
        return [
            {"id": k, **v}
            for k, v in self.jobs.items()
            if not statuses or v.get("status") in statuses
        ]

    def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None

    def mark_all_active_jobs_failed(self, reason):
        self.failed_reason = reason


def _store(monkeypatch, team="test_team") -> BaseJobStore:
    monkeypatch.setattr(shared_job_store, "JobServiceClient", _FakeClient)
    return BaseJobStore(team=team)


def test_create_defaults_to_pending_but_caller_status_wins(monkeypatch):
    s = _store(monkeypatch)
    s.create_job("j1")
    assert s.get_job("j1")["status"] == JOB_STATUS_PENDING
    # A caller-supplied status must not raise (the old `status=…, **fields`
    # form would have been a duplicate-kwarg TypeError) and must win.
    s.create_job("j2", status=JOB_STATUS_RUNNING)
    assert s.get_job("j2")["status"] == JOB_STATUS_RUNNING


def test_cancel_only_when_cancellable(monkeypatch):
    s = _store(monkeypatch)
    s.create_job("j1")
    assert s.cancel_job("j1") is True
    assert s.is_job_cancelled("j1") is True
    # Already cancelled → no longer cancellable.
    assert s.cancel_job("j1") is False
    # Unknown job → False, not a crash.
    assert s.cancel_job("missing") is False


def test_list_filter_and_delete(monkeypatch):
    s = _store(monkeypatch)
    s.create_job("j1")
    s.create_job("j2", status=JOB_STATUS_RUNNING)
    assert len(s.list_jobs()) == 2
    assert [j["id"] for j in s.list_jobs(statuses=[JOB_STATUS_RUNNING])] == ["j2"]
    assert s.delete_job("j2") is True
    assert s.get_job("j2") is None
    assert s.delete_job("j2") is False


def test_lazy_singleton_client(monkeypatch):
    s = _store(monkeypatch)
    assert s._client_instance is None
    first = s._client()
    assert first is s._client()  # same instance reused
    assert first.team == "test_team"


def test_mark_all_running_jobs_failed_is_best_effort(monkeypatch):
    s = _store(monkeypatch)
    s.mark_all_running_jobs_failed("shutdown")
    assert s._client().failed_reason == "shutdown"
