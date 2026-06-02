"""Unit tests for the async job-store wrapper (mocked JobServiceClient)."""

from __future__ import annotations

import pytest

from job_matching_team.shared import job_store


class FakeClient:
    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def create_job(self, job_id, status=None, **fields):
        self.jobs[job_id] = {"job_id": job_id, "status": status, **fields}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def update_job(self, job_id, **fields):
        self.jobs.setdefault(job_id, {}).update(fields)

    def list_jobs(self, statuses=None):
        vals = list(self.jobs.values())
        return [j for j in vals if j.get("status") in statuses] if statuses else vals

    def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(job_store, "_client", lambda: client)
    return client


def test_create_and_get(fake):
    job_store.create_job("j1", message="hi")
    job = job_store.get_job("j1")
    assert job["status"] == job_store.JOB_STATUS_PENDING
    assert job["message"] == "hi"


def test_update_and_list(fake):
    job_store.create_job("j1")
    job_store.update_job("j1", status=job_store.JOB_STATUS_RUNNING)
    running = job_store.list_jobs(statuses=[job_store.JOB_STATUS_RUNNING])
    assert len(running) == 1
    assert len(job_store.list_jobs()) == 1


def test_cancel_pending(fake):
    job_store.create_job("j1")
    assert job_store.cancel_job("j1") is True
    assert job_store.is_job_cancelled("j1") is True


def test_cancel_completed_returns_false(fake):
    job_store.create_job("j1")
    job_store.update_job("j1", status=job_store.JOB_STATUS_COMPLETED)
    assert job_store.cancel_job("j1") is False


def test_cancel_missing_returns_false(fake):
    assert job_store.cancel_job("missing") is False


def test_is_cancelled_missing(fake):
    assert job_store.is_job_cancelled("missing") is False


def test_delete(fake):
    job_store.create_job("j1")
    assert job_store.delete_job("j1") is True
    assert job_store.delete_job("j1") is False


def test_default_client_is_cached(monkeypatch):
    # Exercise the real lazy accessor (no network: just construction).
    monkeypatch.setattr(job_store, "_client_instance", None)

    class Sentinel:
        pass

    monkeypatch.setattr(job_store, "JobServiceClient", lambda team: Sentinel())
    first = job_store._client()
    second = job_store._client()
    assert first is second
