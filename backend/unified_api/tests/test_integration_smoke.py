"""Integration smoke test for the central job service.

Verifies that the test-integration CI infrastructure is wired correctly:

* The job service can be reached over HTTP at ``JOB_SERVICE_URL``.
* The Postgres ``jobs`` table accepts inserts via the public API.
* The full CRUD round-trip + atomic patch helpers work end-to-end.

This is the canonical "does the integration plumbing work?" test.  Per-team
integration suites are added incrementally via #361-#367; this file stays
small.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration]


def test_job_service_health(integration_job_service: str) -> None:
    """``GET /health`` on the job service returns 200."""
    import httpx

    resp = httpx.get(f"{integration_job_service}/health", timeout=5.0)
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_job_crud_roundtrip(integration_job_service: str, truncate_jobs_table: None) -> None:
    """Create / get / update / list / delete a job through ``JobServiceClient``."""
    from job_service_client import JobServiceClient

    client = JobServiceClient(team="integration_smoke", base_url=integration_job_service)
    job_id = f"smoke-{uuid.uuid4()}"

    client.create_job(job_id, status="pending", repo_path="/tmp/x", progress=0)
    job = client.get_job(job_id)
    assert job is not None
    assert job["status"] == "pending"
    assert job["repo_path"] == "/tmp/x"

    client.update_job(job_id, status="running", progress=50)
    job = client.get_job(job_id)
    assert job["status"] == "running"
    assert job["progress"] == 50

    jobs = client.list_jobs(statuses=["running"])
    assert any(j["job_id"] == job_id for j in jobs)

    assert client.delete_job(job_id) is True
    assert client.get_job(job_id) is None


def test_atomic_patch_helpers(integration_job_service: str, truncate_jobs_table: None) -> None:
    """``merge_nested`` / ``append_to_list`` / ``increment_field`` round-trip via Postgres."""
    from job_service_client import JobServiceClient

    client = JobServiceClient(team="integration_smoke", base_url=integration_job_service)
    job_id = f"atomic-{uuid.uuid4()}"

    client.create_job(job_id, status="running", counter=0, log=[], nested={})
    client.increment_field(job_id, "counter", delta=3)
    client.append_to_list(job_id, "log", ["step-1", "step-2"])
    client.merge_nested(job_id, "nested.subkey", {"value": 42})

    job = client.get_job(job_id)
    assert job["counter"] == 3
    assert job["log"] == ["step-1", "step-2"]
    assert job["nested"] == {"subkey": {"value": 42}}


def test_mark_all_active_jobs_failed(integration_job_service: str, truncate_jobs_table: None) -> None:
    """``mark_all_active_jobs_failed`` flips pending/running jobs to failed."""
    from job_service_client import JobServiceClient

    client = JobServiceClient(team="integration_smoke", base_url=integration_job_service)
    pending_id = f"pending-{uuid.uuid4()}"
    running_id = f"running-{uuid.uuid4()}"
    completed_id = f"completed-{uuid.uuid4()}"

    client.create_job(pending_id, status="pending")
    client.create_job(running_id, status="running")
    client.create_job(completed_id, status="completed")

    failed = client.mark_all_active_jobs_failed("smoke-shutdown")
    assert pending_id in failed
    assert running_id in failed
    assert completed_id not in failed

    assert client.get_job(pending_id)["status"] == "failed"
    assert client.get_job(running_id)["status"] == "failed"
    assert client.get_job(completed_id)["status"] == "completed"
