"""Tests for ai_systems_team job store.

Exercises the team's job_store helpers directly; relied on the file
fallback that is now removed.  Marked integration until follow-up
conversion to the in-memory fake.
"""

import pytest

from ai_systems_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    add_completed_phase,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    list_jobs,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "cache"


def test_create_and_get_job(cache_dir):
    create_job("j1", "my_proj", "/spec.md", cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["job_id"] == "j1"
    assert data["project_name"] == "my_proj"
    assert data["status"] == JOB_STATUS_PENDING


def test_list_jobs_empty(cache_dir):
    jobs = list_jobs(cache_dir=cache_dir)
    assert jobs == []


def test_list_jobs_running_only(cache_dir):
    create_job("j1", "proj1", "/spec1.md", cache_dir=cache_dir)
    create_job("j2", "proj2", "/spec2.md", cache_dir=cache_dir)
    mark_job_running("j1", cache_dir=cache_dir)
    mark_job_failed("j2", error="failed", cache_dir=cache_dir)

    running = list_jobs(running_only=True, cache_dir=cache_dir)
    all_jobs = list_jobs(running_only=False, cache_dir=cache_dir)

    # running_only returns pending+running, j1=running, j2=failed -> only j1
    assert len(running) == 1
    assert running[0]["job_id"] == "j1"
    assert len(all_jobs) == 2


def test_mark_job_running(cache_dir):
    create_job("j1", "proj", "/spec.md", cache_dir=cache_dir)
    mark_job_running("j1", cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_RUNNING


def test_mark_job_completed(cache_dir):
    create_job("j1", "proj", "/spec.md", cache_dir=cache_dir)
    mark_job_completed("j1", blueprint={"project_name": "proj"}, cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_COMPLETED
    assert data["progress"] == 100
    assert data["blueprint"]["project_name"] == "proj"


def test_mark_job_failed(cache_dir):
    create_job("j1", "proj", "/spec.md", cache_dir=cache_dir)
    mark_job_failed("j1", error="something broke", cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_FAILED
    assert data["error"] == "something broke"


def test_cancel_job(cache_dir):
    create_job("j1", "proj", "/spec.md", cache_dir=cache_dir)
    result = cancel_job("j1", cache_dir=cache_dir)
    assert result is True
    data = get_job("j1", cache_dir=cache_dir)
    assert data["status"] == JOB_STATUS_CANCELLED


def test_delete_job(cache_dir):
    create_job("j1", "proj", "/spec.md", cache_dir=cache_dir)
    result = delete_job("j1", cache_dir=cache_dir)
    assert result is True
    data = get_job("j1", cache_dir=cache_dir)
    assert data == {}


def test_add_completed_phase(cache_dir):
    create_job("j1", "proj", "/spec.md", cache_dir=cache_dir)
    add_completed_phase("j1", "spec_intake", phase_result={"success": True}, cache_dir=cache_dir)
    data = get_job("j1", cache_dir=cache_dir)
    assert "spec_intake" in data["completed_phases"]
    assert data["phase_results"]["spec_intake"]["success"] is True


def test_get_missing_job_returns_empty(cache_dir):
    data = get_job("nonexistent", cache_dir=cache_dir)
    assert data == {}


def test_cancel_nonexistent_job_returns_false(cache_dir):
    result = cancel_job("nonexistent", cache_dir=cache_dir)
    assert result is False
