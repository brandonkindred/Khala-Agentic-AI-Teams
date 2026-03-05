"""
API tests for the frontend-code-v2 endpoints:
  POST /frontend-code-v2/run
  GET /frontend-code-v2/status/{job_id}
"""

from __future__ import annotations

import importlib.util
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
def temp_repo(tmp_path: Path) -> Path:
    """Create a minimal directory that can serve as a repo path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "test-app"}')
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


class TestFrontendCodeV2RunEndpoint:
    def test_run_returns_job_id(self, client: TestClient, temp_repo: Path):
        response = client.post("/frontend-code-v2/run", json={
            "task": {
                "title": "Test task",
                "description": "Implement login component",
            },
            "repo_path": str(temp_repo),
        })
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "running"
        assert data["message"]

    def test_run_rejects_invalid_repo_path(self, client: TestClient):
        response = client.post("/frontend-code-v2/run", json={
            "task": {"title": "Test", "description": "test"},
            "repo_path": "/nonexistent/path/does/not/exist",
        })
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    def test_run_accepts_optional_fields(self, client: TestClient, temp_repo: Path):
        response = client.post("/frontend-code-v2/run", json={
            "task": {
                "title": "Full task",
                "description": "Add dashboard",
                "requirements": "Angular, Material",
                "acceptance_criteria": ["Responsive layout", "Dark mode"],
            },
            "repo_path": str(temp_repo),
            "spec_content": "Dashboard spec",
            "architecture": "SPA with Angular",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"]

    def test_run_requires_task_and_repo(self, client: TestClient):
        response = client.post("/frontend-code-v2/run", json={})
        assert response.status_code == 422


class TestFrontendCodeV2StatusEndpoint:
    def test_status_returns_404_for_unknown_job(self, client: TestClient):
        response = client.get("/frontend-code-v2/status/nonexistent-job-id")
        assert response.status_code == 404

    def test_status_returns_pending_after_create(self, client: TestClient, temp_repo: Path):
        run_resp = client.post("/frontend-code-v2/run", json={
            "task": {"title": "Test", "description": "test"},
            "repo_path": str(temp_repo),
        })
        job_id = run_resp.json()["job_id"]

        time.sleep(0.2)

        status_resp = client.get(f"/frontend-code-v2/status/{job_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("pending", "running", "completed", "failed")
        assert "progress" in data
        assert isinstance(data["completed_phases"], list)
        assert isinstance(data["microtasks_completed"], int)
        assert isinstance(data["microtasks_total"], int)

    def test_status_response_shape(self, client: TestClient, temp_repo: Path):
        run_resp = client.post("/frontend-code-v2/run", json={
            "task": {"title": "Shape test", "description": "test"},
            "repo_path": str(temp_repo),
        })
        job_id = run_resp.json()["job_id"]

        time.sleep(0.1)

        status_resp = client.get(f"/frontend-code-v2/status/{job_id}")
        data = status_resp.json()
        expected_keys = {"job_id", "status", "repo_path", "current_phase", "current_microtask",
                         "progress", "microtasks_completed", "microtasks_total",
                         "completed_phases", "error", "summary"}
        assert expected_keys.issubset(set(data.keys()))
