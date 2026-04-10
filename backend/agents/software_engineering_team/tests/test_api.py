"""Tests for the run-team API endpoint."""

import importlib.util
import os
import subprocess

# Load api.main from this team's api/ (avoids conflict with agents/api/main.py)
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial spec"],
        cwd=repo,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
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
    from unittest.mock import MagicMock, patch

    from software_engineering_team.shared.models import ProductRequirements

    spec = "# Task Manager API\n\nREST API for managing tasks with CRUD operations."

    mock_arch = MagicMock()
    mock_arch.overview = "Task Manager architecture overview"
    mock_arch.architecture_document = "# Architecture\n\nDocument content."
    mock_arch.components = []
    mock_arch.diagrams = {"component": "graph TD;A-->B"}
    mock_arch.decisions = []
    mock_arch.tenancy_model = ""
    mock_arch.reliability_model = ""

    mock_output = MagicMock()
    mock_output.architecture = mock_arch
    mock_output.summary = "Architecture summary"

    mock_agent = MagicMock()
    mock_agent.run.return_value = mock_output

    fake_reqs = ProductRequirements(title="Task Manager", description="Task manager API")

    with (
        patch("spec_parser.parse_spec_with_llm", return_value=fake_reqs),
        patch("architecture_expert.ArchitectureExpertAgent", return_value=mock_agent),
        patch("llm_service.get_client"),
    ):
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


def test_delete_job_success(client: TestClient, temp_work_path: Path) -> None:
    """DELETE /run-team/{job_id} removes the job and returns 200."""
    from software_engineering_team.shared.job_store import create_job, get_job, list_jobs

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    assert get_job(job_id) is not None

    r = client.delete(f"/run-team/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data.get("job_id") == job_id
    assert "message" in data

    assert get_job(job_id) is None
    job_ids = [j["job_id"] for j in list_jobs(running_only=False)]
    assert job_id not in job_ids


def test_delete_job_404(client: TestClient) -> None:
    """DELETE /run-team/{job_id} returns 404 for non-existent job."""
    r = client.delete("/run-team/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_get_running_jobs(client: TestClient) -> None:
    """GET /run-team/jobs returns list of running/pending jobs (default running_only=True)."""
    r = client.get("/run-team/jobs")
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data
    assert isinstance(data["jobs"], list)


def test_get_running_jobs_all(client: TestClient) -> None:
    """GET /run-team/jobs?running_only=false returns all jobs (including completed/failed)."""
    r = client.get("/run-team/jobs", params={"running_only": "false"})
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
    # When LLM_PROVIDER=dummy (CI without a real LLM) the job may still be
    # running after the timeout – the polling mechanism has already been verified.
    if os.getenv("LLM_PROVIDER", "") not in ("dummy", ""):
        assert data["status"] in ("completed", "failed")
    if data["status"] == "completed":
        assert "requirements_title" in data or data.get("architecture_overview") is not None
        assert "task_results" in data

    # Verify agents wrote files only if job completed successfully
    if data and data.get("status") == "completed":
        work_path = temp_work_path
        backend_dir = work_path / "backend"
        devops_dir = work_path / "devops"
        assert backend_dir.exists() or devops_dir.exists(), (
            "Agent output should create backend or devops dirs"
        )
        if backend_dir.exists():
            assert any(backend_dir.rglob("*.py")), "Backend should have added Python files"


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
    assert (
        "cannot be resumed" in r.json().get("detail", "").lower()
        or "status" in r.json().get("detail", "").lower()
    )


def test_resume_200_when_status_failed(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 200 when job status is failed (resume is allowed for failed jobs)."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status="failed")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 200


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
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="planning_v2")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 400
    assert (
        "run_team" in r.json().get("detail", "").lower()
        or "job_type" in r.json().get("detail", "").lower()
    )


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

    # Job store should show running or failed (fails fast in CI without LLM)
    time.sleep(0.15)
    job_data = get_job(job_id)
    assert job_data is not None
    assert job_data.get("status") in ("running", "failed")


def test_resume_200_when_agent_crash(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/resume returns 200 when status is agent_crash."""
    from software_engineering_team.shared.job_store import (
        JOB_STATUS_AGENT_CRASH,
        create_job,
        get_job,
        update_job,
    )

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status=JOB_STATUS_AGENT_CRASH, error="Simulated crash")

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id
    assert r.json()["status"] == "running"

    # Job store should show running or failed (fails fast in CI without LLM)
    time.sleep(0.15)
    job_data = get_job(job_id)
    assert job_data is not None
    assert job_data.get("status") in ("running", "failed")


def test_mark_all_running_jobs_failed(tmp_path: Path) -> None:
    """mark_all_running_jobs_failed sets all running/pending jobs to failed with reason."""
    from software_engineering_team.shared.job_store import (
        JOB_STATUS_RUNNING,
        create_job,
        get_job,
        mark_all_running_jobs_failed,
        update_job,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/some/repo", cache_dir=cache_dir)
    update_job(job_id, status=JOB_STATUS_RUNNING, cache_dir=cache_dir)

    mark_all_running_jobs_failed("test", cache_dir=cache_dir)

    job_data = get_job(job_id, cache_dir=cache_dir)
    assert job_data is not None
    assert job_data.get("status") == "interrupted"
    assert job_data.get("error") == "test"


def test_job_store_single_path_composite_update_visible(tmp_path: Path) -> None:
    """create_job, update_task_state, then get_job/list_jobs see same data (single path via manager)."""
    from software_engineering_team.shared.job_store import (
        create_job,
        get_job,
        list_jobs,
        update_task_state,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)
    update_task_state(job_id, "task_1", cache_dir=cache_dir, status="done", assignee="backend")

    job_data = get_job(job_id, cache_dir=cache_dir)
    assert job_data is not None
    assert job_data.get("task_states", {}).get("task_1") == {
        "status": "done",
        "assignee": "backend",
    }

    jobs = list_jobs(cache_dir=cache_dir, running_only=False)
    assert any(j.get("job_id") == job_id for j in jobs)


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
    assert (
        "cannot be restarted" in r.json().get("detail", "").lower()
        or "status" in r.json().get("detail", "").lower()
    )


def test_restart_400_when_job_type_not_run_team(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/restart returns 400 for non-run_team jobs."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="planning_v2")
    update_job(job_id, status="failed")

    r = client.post(f"/run-team/{job_id}/restart")
    assert r.status_code == 400


def test_restart_200_when_failed_reuses_same_job(client: TestClient, temp_work_path: Path) -> None:
    """POST /run-team/{job_id}/restart returns 200 and reuses the same job (same job_id), reset and running."""
    from software_engineering_team.shared.job_store import create_job, get_job, update_job

    old_job_id = str(uuid.uuid4())
    create_job(old_job_id, str(temp_work_path), job_type="run_team")
    update_job(old_job_id, status="failed")

    r = client.post(f"/run-team/{old_job_id}/restart")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["job_id"] == old_job_id

    job = get_job(old_job_id)
    assert job is not None
    assert job.get("status") == "running"
    assert job.get("repo_path") == str(temp_work_path)
