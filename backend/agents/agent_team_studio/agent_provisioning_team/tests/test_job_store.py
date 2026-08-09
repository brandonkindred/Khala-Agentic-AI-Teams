"""Tests for agent_provisioning_team job store.

Exercises the team's job_store helpers directly via the in-memory
FakeJobServiceClient (autouse fixture in conftest.py).
"""

from agent_team_studio.agent_provisioning_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    list_jobs,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
)


def test_create_and_get_job():
    create_job("j1", agent_id="agent-001", manifest_path="default.yaml")
    data = get_job("j1")
    assert data["job_id"] == "j1"
    assert data["agent_id"] == "agent-001"
    assert data["status"] == JOB_STATUS_PENDING


def test_list_jobs_empty():
    jobs = list_jobs()
    assert jobs == []


def test_list_jobs_all():
    create_job("j1", "agent-1", "default.yaml")
    create_job("j2", "agent-2", "custom.yaml")
    jobs = list_jobs()
    assert len(jobs) == 2


def test_list_jobs_running_only():
    create_job("j1", "agent-1", "default.yaml")
    create_job("j2", "agent-2", "default.yaml")
    mark_job_running("j1")
    mark_job_failed("j2", error="failed")

    running = list_jobs(running_only=True)
    assert len(running) == 1
    assert running[0]["job_id"] == "j1"


def test_mark_job_running():
    create_job("j1", "agent-1", "default.yaml")
    mark_job_running("j1")
    data = get_job("j1")
    assert data["status"] == JOB_STATUS_RUNNING


def test_mark_job_completed():
    create_job("j1", "agent-1", "default.yaml")
    mark_job_completed("j1", result={"agent_id": "agent-1", "success": True})
    data = get_job("j1")
    assert data["status"] == JOB_STATUS_COMPLETED
    assert data["progress"] == 100
    assert data["result"]["success"] is True


def test_mark_job_failed():
    create_job("j1", "agent-1", "default.yaml")
    mark_job_failed("j1", error="Docker not available")
    data = get_job("j1")
    assert data["status"] == JOB_STATUS_FAILED
    assert data["error"] == "Docker not available"


def test_cancel_job():
    create_job("j1", "agent-1", "default.yaml")
    result = cancel_job("j1")
    assert result is True
    data = get_job("j1")
    assert data["status"] == JOB_STATUS_CANCELLED


def test_cancel_nonexistent_job():
    result = cancel_job("no-such-job")
    assert result is False


def test_delete_job():
    create_job("j1", "agent-1", "default.yaml")
    deleted = delete_job("j1")
    assert deleted is True
    data = get_job("j1")
    assert data == {}


def test_get_missing_job():
    data = get_job("nonexistent")
    assert data == {}
