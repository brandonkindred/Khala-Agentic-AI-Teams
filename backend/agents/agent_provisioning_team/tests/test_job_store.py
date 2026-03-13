"""Tests for agent_provisioning_team job store."""

import pytest

from agent_provisioning_team.shared.job_store import (
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


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "cache"


def test_create_and_get_job(cache_dir):
    create_job("j1", agent_id="agent-001", manifest_path="default.yaml", cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["job_id"] == "j1"
    assert data["agent_id"] == "agent-001"
    assert data["status"] == JOB_STATUS_PENDING


def test_list_jobs_empty(cache_dir):
    jobs = list_jobs(cache_dir=cache_dir)
    assert jobs == []


def test_list_jobs_all(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    create_job("j2", "agent-2", "custom.yaml", cache_dir=cache_dir)
    jobs = list_jobs(cache_dir=cache_dir)
    assert len(jobs) == 2


def test_list_jobs_running_only(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    create_job("j2", "agent-2", "default.yaml", cache_dir=cache_dir)
    mark_job_running("j1", cache_dir=cache_dir)
    mark_job_failed("j2", error="failed", cache_dir=cache_dir)

    running = list_jobs(running_only=True, cache_dir=cache_dir)
    assert len(running) == 1
    assert running[0]["job_id"] == "j1"


def test_mark_job_running(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    mark_job_running("j1", cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_RUNNING


def test_mark_job_completed(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    mark_job_completed("j1", result={"agent_id": "agent-1", "success": True}, cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_COMPLETED
    assert data["progress"] == 100
    assert data["result"]["success"] is True


def test_mark_job_failed(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    mark_job_failed("j1", error="Docker not available", cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_FAILED
    assert data["error"] == "Docker not available"


def test_cancel_job(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    result = cancel_job("j1", cache_dir=cache_dir)
    assert result is True
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_CANCELLED


def test_cancel_nonexistent_job(cache_dir):
    result = cancel_job("no-such-job", cache_dir=cache_dir)
    assert result is False


def test_delete_job(cache_dir):
    create_job("j1", "agent-1", "default.yaml", cache_dir=cache_dir)
    deleted = delete_job("j1", cache_dir=cache_dir)
    assert deleted is True
    data = get_job("j1", cache_dir=cache_dir)
    assert data == {}


def test_get_missing_job(cache_dir):
    data = get_job("nonexistent", cache_dir=cache_dir)
    assert data == {}
