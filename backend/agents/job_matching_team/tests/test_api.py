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

    # No POSTGRES_HOST -> register_team_schemas is a no-op; the factory-provided
    # lifespan must still enter and exit cleanly.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    async def go():
        async with api_main.app.router.lifespan_context(api_main.app):
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


def test_scan_dispatches_via_temporal_when_enabled(client, monkeypatch):
    """With Temporal enabled the scan is handed to the workflow, not a thread."""
    import shared_temporal
    from job_matching_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    dispatched: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sw,
        "start_job_matching_workflow",
        lambda job_id, request: dispatched.append((job_id, request)),
    )

    resp = client.post("/scan", json={"top_n": 3})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    # Temporal path is fire-and-forget: the worker (not the API thread) advances
    # the job, so with the stub in place the job stays PENDING.
    assert client.get(f"/scan/status/{job_id}").json()["status"] == "pending"
    assert len(dispatched) == 1
    assert dispatched[0][0] == job_id
    assert dispatched[0][1]["top_n"] == 3


def test_scan_returns_503_and_marks_failed_when_temporal_dispatch_fails(client, monkeypatch):
    """A Temporal dispatch failure must not orphan a PENDING job: the job is
    marked FAILED and the caller gets a 503, not a bare 500 with a stuck row."""
    import shared_temporal
    from job_matching_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(sw, "start_job_matching_workflow", _boom)

    resp = client.post("/scan", json={"top_n": 1})
    assert resp.status_code == 503
    # No orphaned PENDING row: the one job recorded is FAILED.
    jobs = client.get("/scan/jobs").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"


def test_scan_falls_back_to_thread_when_temporal_disabled(client, monkeypatch):
    """When Temporal is disabled the dispatch helper reports no-dispatch and the
    thread path runs the scan to completion."""
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    job_id = client.post("/scan", json={}).json()["job_id"]
    data = _wait_for_completion(client, job_id)
    assert data["status"] == "completed"


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


def test_put_profile_saves_career_section(client, monkeypatch):
    saved = []

    def fake_save(profile):
        saved.append(profile)
        return profile

    monkeypatch.setattr(api_main, "save_career_profile", fake_save)
    resp = client.put("/profile", json={"target_titles": ["Staff Eng"], "salary_min": 200000})
    assert resp.status_code == 200
    assert resp.json()["target_titles"] == ["Staff Eng"]
    assert saved[0].salary_min == 200000


def test_put_profile_503_when_store_unavailable(client, monkeypatch):
    from job_matching_team.profile.career_store import CareerProfileUnavailableError

    def fake_save(profile):
        raise CareerProfileUnavailableError("Postgres unavailable")

    monkeypatch.setattr(api_main, "save_career_profile", fake_save)
    resp = client.put("/profile", json={})
    assert resp.status_code == 503
    assert "Postgres" in resp.json()["detail"]


def test_put_profile_rejects_invalid_payload(client):
    assert client.put("/profile", json={"remote_preference": "bogus"}).status_code == 422


def _fake_listing(status="new"):
    from job_matching_team.models import Listing

    posting = JobPosting(company="Acme", title="Engineer", location="NYC").ensure_fingerprint()
    return Listing(
        fingerprint=posting.fingerprint,
        posting=posting,
        score=0.9,
        recommendation="apply",
        rationale="fit",
        run_id="r1",
        status=status,
    )


class FakeListingStore:
    def __init__(self):
        self.calls = []

    def list_listings(self, *, status="active", limit=200):
        from job_matching_team.models import ListingsResponse

        self.calls.append(("list", status, limit))
        return ListingsResponse(listings=[_fake_listing()], total=1, counts={"new": 1})

    def update_listing_state(self, fingerprint, update):
        self.calls.append(("update", fingerprint, update))
        if fingerprint == "missing":
            return None
        return _fake_listing(status=update.status)


def test_list_listings_delegates_to_store(client, monkeypatch):
    from job_matching_team import store as store_mod

    fake = FakeListingStore()
    monkeypatch.setattr(store_mod, "get_store", lambda: fake)
    resp = client.get("/listings", params={"status": "favorite", "limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["counts"] == {"new": 1}
    assert body["listings"][0]["posting"]["company"] == "Acme"
    assert fake.calls == [("list", "favorite", 10)]


def test_list_listings_rejects_invalid_filter(client):
    assert client.get("/listings", params={"status": "bogus"}).status_code == 422
    assert client.get("/listings", params={"limit": 0}).status_code == 422


def test_patch_listing_updates_status(client, monkeypatch):
    from job_matching_team import store as store_mod

    fake = FakeListingStore()
    monkeypatch.setattr(store_mod, "get_store", lambda: fake)
    resp = client.patch("/listings/abc123", json={"status": "archived"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    kind, fingerprint, update = fake.calls[0]
    assert (kind, fingerprint, update.status) == ("update", "abc123", "archived")


def test_patch_listing_404_when_unknown(client, monkeypatch):
    from job_matching_team import store as store_mod

    monkeypatch.setattr(store_mod, "get_store", lambda: FakeListingStore())
    assert client.patch("/listings/missing", json={"status": "favorite"}).status_code == 404


def test_patch_listing_rejects_invalid_status(client):
    assert client.patch("/listings/abc", json={"status": "starred"}).status_code == 422


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
