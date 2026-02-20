"""Tests for the run-team API endpoint."""

import importlib.util
import os
import subprocess
import tempfile
import time
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
