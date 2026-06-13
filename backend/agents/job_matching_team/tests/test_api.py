"""API tests using FastAPI TestClient with an in-memory job store + fake orchestrator."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from job_matching_team.api import main as api_main
from job_matching_team.models import JobMatchResponse, JobPosting, RankedJob
from job_matching_team.profile.model import JobSeekerProfile


class InMemoryJobs:
    """Stand-in for the central job service used by the API layer."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def _now(self):
        return datetime.now(tz=timezone.utc).isoformat()

    def create_job(self, job_id, **fields):
        self.jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "created_at": self._now(),
            **fields,
        }

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def update_job(self, job_id, **fields):
        if job_id in self.jobs:
            self.jobs[job_id].update(fields)
            self.jobs[job_id]["updated_at"] = self._now()

    def list_jobs(self, statuses=None):
        vals = list(self.jobs.values())
        if statuses:
            vals = [j for j in vals if j.get("status") in statuses]
        return vals

    def is_job_cancelled(self, job_id):
        j = self.jobs.get(job_id)
        return j is not None and j.get("status") == "cancelled"

    def cancel_job(self, job_id):
        j = self.jobs.get(job_id)
        if j is None or j.get("status") not in {"pending", "running"}:
            return False
        j["status"] = "cancelled"
        return True

    def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None


class FakeOrchestrator:
    def run(self, request, *, job_id: str | None = None, **kwargs):
        posting = JobPosting(company="Acme", title="Engineer").ensure_fingerprint()
        return JobMatchResponse(
            run_id="run-1",
            ranked_jobs=[RankedJob(posting=posting, score=0.9, recommendation="apply")],
            total_found=1,
            total_ranked=1,
            profile_snapshot=JobSeekerProfile(),
        )


@pytest.fixture
def client(monkeypatch):
    store = InMemoryJobs()
    monkeypatch.setattr(api_main, "create_job", store.create_job)
    monkeypatch.setattr(api_main, "get_job", store.get_job)
    monkeypatch.setattr(api_main, "update_job", store.update_job)
    monkeypatch.setattr(api_main, "list_jobs", store.list_jobs)
    monkeypatch.setattr(api_main, "is_job_cancelled", store.is_job_cancelled)
    monkeypatch.setattr(api_main, "cancel_job", store.cancel_job)
    monkeypatch.setattr(api_main, "delete_job", store.delete_job)
    monkeypatch.setattr(api_main, "_get_orchestrator", lambda: FakeOrchestrator())
    return TestClient(api_main.app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_lifespan_runs_without_postgres(monkeypatch):
    import asyncio

    # No POSTGRES_HOST -> register_team_schemas is a no-op; lifespan must not raise.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    async def go():
        async with api_main._lifespan(api_main.app):
            pass

    asyncio.run(go())


def test_get_orchestrator_builds_real_instance():
    orch = api_main._get_orchestrator()
    assert hasattr(orch, "run")


def test_profile_returns_bundled_example(client):
    resp = client.get("/profile")
    assert resp.status_code == 200
    assert resp.json()["remote_preference"] in ("remote", "hybrid", "onsite", "any")


def _wait_for_completion(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/scan/status/{job_id}").json()
        if data["status"] in ("completed", "failed"):
            return data
        time.sleep(0.02)
    raise AssertionError("scan did not complete in time")


def test_scan_lifecycle(client):
    resp = client.post("/scan", json={"top_n": 5})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    data = _wait_for_completion(client, job_id)
    assert data["status"] == "completed"
    assert data["result"]["ranked_jobs"][0]["posting"]["company"] == "Acme"


def test_scan_status_not_found(client):
    assert client.get("/scan/status/missing").status_code == 404


def test_list_jobs(client):
    client.post("/scan", json={})
    jobs = client.get("/scan/jobs").json()["jobs"]
    assert len(jobs) >= 1


def test_cancel_then_delete(client):
    job_id = client.post("/scan", json={}).json()["job_id"]
    # Cancel may race with the fast fake orchestrator; either result is valid.
    client.post(f"/scan/jobs/{job_id}/cancel")
    deleted = client.delete(f"/scan/jobs/{job_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.delete(f"/scan/jobs/{job_id}").status_code == 404


def test_cancel_missing_job(client):
    assert client.post("/scan/jobs/missing/cancel").status_code == 404


def test_runs_endpoints_use_store(client, monkeypatch):
    from job_matching_team import store as store_mod
    from job_matching_team.models import RunDetail, RunSummary

    class FakeStore:
        def list_runs(self, *, limit=50):
            return [RunSummary(run_id="r1", status="completed", total_found=2, total_ranked=2)]

        def get_run(self, run_id):
            if run_id != "r1":
                return None
            return RunDetail(run_id="r1", status="completed", total_found=2, total_ranked=2)

    monkeypatch.setattr(store_mod, "get_store", lambda: FakeStore())
    assert client.get("/runs").json()[0]["run_id"] == "r1"
    assert client.get("/runs/r1").json()["status"] == "completed"
    assert client.get("/runs/missing").status_code == 404
