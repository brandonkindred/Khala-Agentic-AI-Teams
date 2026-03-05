"""Tests for the run-team API endpoint."""

import importlib.util
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Load api.main from this team's api/ (avoids conflict with agents/api/main.py)
import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_spec = importlib.util.spec_from_file_location(
    "software_engineering_api_main",
    _team_dir / "api" / "main.py",
)
_api_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_main)
app = _api_main.app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def temp_work_path(tmp_path: Path) -> Path:
    """Create a work folder with initial_spec.md only (no git required)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "initial_spec.md").write_text("# Task Manager API\n\nREST API for tasks.")
    return work


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    """Create a minimal valid git repo with initial_spec.md and initial commit (for backward compat)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "initial_spec.md").write_text("# Task Manager API\n\nREST API for tasks.")
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "initial_spec.md"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial spec"],
        cwd=repo,
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    return repo


def test_health(client: TestClient) -> None:
    """Health endpoint returns ok."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_architect_design_empty_spec(client: TestClient) -> None:
    """architect/design returns 400 when spec is empty."""
    r = client.post("/architect/design", json={"spec": ""})
    assert r.status_code == 400


def test_architect_design_success(client: TestClient) -> None:
    """architect/design returns architecture documents and diagrams."""
    spec = "# Task Manager API\n\nREST API for managing tasks with CRUD operations."
    r = client.post("/architect/design", json={"spec": spec})
    assert r.status_code == 200
    data = r.json()
    assert "overview" in data
    assert "architecture_document" in data
    assert "components" in data
    assert "diagrams" in data
    assert "decisions" in data
    assert isinstance(data["diagrams"], dict)


def test_run_team_requires_repo_path(client: TestClient) -> None:
    """run-team returns 422 when repo_path missing."""
    r = client.post("/run-team", json={})
    assert r.status_code == 422


def test_run_team_invalid_path(client: TestClient) -> None:
    """run-team returns 400 for non-existent path."""
    r = client.post("/run-team", json={"repo_path": "/nonexistent/path"})
    assert r.status_code == 400


def test_get_job_status_404(client: TestClient) -> None:
    """GET /run-team/{job_id} returns 404 for unknown job."""
    r = client.get("/run-team/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_get_running_jobs(client: TestClient) -> None:
    """GET /run-team/jobs returns list of running/pending jobs."""
    r = client.get("/run-team/jobs")
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data
    assert isinstance(data["jobs"], list)


def test_run_team_returns_job_id(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team returns job_id and status immediately (work path need not be a git repo)."""
    r = client.post("/run-team", json={"repo_path": str(temp_work_path)})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data["status"] in ("running", "pending")
    assert "message" in data


def test_run_team_poll_status(client: TestClient, temp_work_path: Path) -> None:
    """POST starts job; GET /run-team/{job_id} returns status until completed."""
    r = client.post("/run-team", json={"repo_path": str(temp_work_path)})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Brief delay so job file is fully written before first poll
    time.sleep(0.2)

    # Poll until completed or failed (max 60s)
    data = None
    for _ in range(60):
        r = client.get(f"/run-team/{job_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("pending", "running", "completed", "failed")
        if data["status"] in ("completed", "failed"):
            break
        time.sleep(1)

    assert data is not None
    assert data["status"] == "completed"
    assert "requirements_title" in data or data.get("architecture_overview") is not None
    assert "task_results" in data

    # Verify agents wrote files (backend/frontend in their own repos under work path)
    work_path = temp_work_path
    backend_dir = work_path / "backend"
    devops_dir = work_path / "devops"
    assert backend_dir.exists() or devops_dir.exists(), "Agent output should create backend or devops dirs"
    if backend_dir.exists():
        assert any(backend_dir.rglob("*.py")), "Backend should have added Python files"
    if (work_path / "devops" / ".github" / "workflows" / "ci.yml").exists() or (work_path / ".github" / "workflows" / "ci.yml").exists():
        pass  # DevOps may add CI config


# --- Resume endpoint tests ---


def test_resume_404_when_job_missing(client: TestClient) -> None:
    """POST /run-team/{job_id}/resume returns 404 for unknown job."""
    job_id = str(uuid.uuid4())
    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 404
    assert "Job not found" in r.json().get("detail", "")


def test_resume_400_when_no_repo_path(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 400 when job has no repo_path."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, repo_path=None)

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400
    assert "repo_path" in r.json().get("detail", "").lower()


def test_resume_400_when_status_completed(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 400 when job status is completed."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status="completed")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400
    assert "cannot be resumed" in r.json().get("detail", "").lower() or "status" in r.json().get("detail", "").lower()


def test_resume_400_when_status_failed(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 400 when job status is failed."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status="failed")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400


def test_resume_400_when_status_cancelled(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 400 when job status is cancelled."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status="cancelled")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400


def test_resume_400_when_invalid_repo_path(client: TestClient) -> None:
    """POST /run-team/{job_id}/resume returns 400 when repo_path does not exist or is invalid."""
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, "/nonexistent/path/for/resume/test", job_type="run_team")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400
    assert "detail" in r.json()


def test_resume_400_when_job_type_not_run_team(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 400 when job_type is not run_team."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="planning_v2")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400
    assert "run_team" in r.json().get("detail", "").lower() or "job_type" in r.json().get("detail", "").lower()


def test_resume_200_when_pending(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 200 and starts thread when status is pending."""
    from software_engineering_team.shared.job_store import create_job, get_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == job_id
    assert data["status"] == "running"
    assert "message" in data

    # Job store should show running
    time.sleep(0.15)
    job_data = get_job(job_id)
    assert job_data is not None
    assert job_data.get("status") == "running"


def test_resume_200_when_agent_crash(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 200 when status is agent_crash."""
    from software_engineering_team.shared.job_store import create_job, get_job, update_job
    from software_engineering_team.shared.job_store import JOB_STATUS_AGENT_CRASH

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status=JOB_STATUS_AGENT_CRASH, error="Simulated crash")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id
    assert r.json()["status"] == "running"

    time.sleep(0.15)
    job_data = get_job(job_id)
    assert job_data is not None
    assert job_data.get("status") == "running"


def test_mark_all_running_jobs_failed(tmp_path: Path) -> None:
    """mark_all_running_jobs_failed sets all running/pending jobs to failed with reason."""
    from software_engineering_team.shared.job_store import (
        create_job,
        get_job,
        mark_all_running_jobs_failed,
        update_job,
        JOB_STATUS_RUNNING,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/some/repo", cache_dir=cache_dir)
    update_job(job_id, status=JOB_STATUS_RUNNING, cache_dir=cache_dir)

    mark_all_running_jobs_failed("test", cache_dir=cache_dir)

    job_data = get_job(job_id, cache_dir=cache_dir)
    assert job_data is not None
    assert job_data.get("status") == "failed"
    assert job_data.get("error") == "test"


# --- Restart endpoint tests ---


def test_restart_404_when_job_missing(client: TestClient) -> None:
    """POST /run-team/{job_id}/restart returns 404 for unknown job."""
    job_id = str(uuid.uuid4())
    r = client.post(f"/run-team/{job_id}/restart")
    assert r.status_code == 404


def test_restart_400_when_status_running(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/restart returns 400 when job is still active."""
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")

    r = client.post(f"/run-team/{job_id}/restart")
    assert r.status_code == 400
    assert "cannot be restarted" in r.json().get("detail", "").lower() or "status" in r.json().get("detail", "").lower()


def test_restart_400_when_job_type_not_run_team(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/restart returns 400 for non-run_team jobs."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="planning_v2")
    update_job(job_id, status="failed")

    r = client.post(f"/run-team/{job_id}/restart")
    assert r.status_code == 400


def test_restart_200_when_failed_creates_new_job(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/restart returns 200 and creates a new running job."""
    from software_engineering_team.shared.job_store import create_job, get_job, update_job

    old_job_id = str(uuid.uuid4())
    create_job(old_job_id, str(temp_work_path), job_type="run_team")
    update_job(old_job_id, status="failed")

    r = client.post(f"/run-team/{old_job_id}/restart")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["job_id"] != old_job_id

    new_job = get_job(data["job_id"])
    assert new_job is not None
    assert new_job.get("status") == "running"
    assert new_job.get("repo_path") == str(temp_work_path)
