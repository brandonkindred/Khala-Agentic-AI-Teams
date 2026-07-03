"""
API tests for the backend-code-v2 endpoints:
  POST /backend-code-v2/run
  GET /backend-code-v2/status/{job_id}

Routed through the in-memory ``FakeJobServiceClient`` via the autouse
``_autouse_patched_job_store`` fixture, so the team API's job-store calls
land in a per-test in-memory dict.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from software_engineering_team.api import main as _api_main  # noqa: E402

app = _api_main.app


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


@pytest.fixture(autouse=True)
def _stub_background_workflow(monkeypatch):
    """Replace the fire-and-forget workflow worker with a no-op.

    The ``POST /backend-code-v2/run`` endpoint spawns a daemon thread that runs
    the real (integration-only) backend workflow. These unit tests only assert
    the endpoint contract, so without this stub the thread keeps executing after
    the test returns and logs to closed pytest capture streams
    ("ValueError: I/O operation on closed file"). Mirrors the
    ``patch.object(_api_main, "_run_orchestrator_background")`` pattern used for
    the /run-team endpoint tests.
    """
    monkeypatch.setattr(_api_main, "_run_backend_code_v2_background", lambda *a, **k: None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    """Create a minimal directory that can serve as a repo path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("flask\n")
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


class TestBackendCodeV2RunEndpoint:
    def test_run_returns_job_id(self, client: TestClient, temp_repo: Path):
        response = client.post(
            "/backend-code-v2/run",
            json={
                "task": {
                    "title": "Test task",
                    "description": "Implement hello world",
                },
                "repo_path": str(temp_repo),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "running"
        assert data["message"]

    def test_run_rejects_invalid_repo_path(self, client: TestClient):
        response = client.post(
            "/backend-code-v2/run",
            json={
                "task": {"title": "Test", "description": "test"},
                "repo_path": "/nonexistent/path/does/not/exist",
            },
        )
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    def test_run_accepts_optional_fields(self, client: TestClient, temp_repo: Path):
        response = client.post(
            "/backend-code-v2/run",
            json={
                "task": {
                    "title": "Full task",
                    "description": "Implement user registration",
                    "requirements": "FastAPI, SQLAlchemy",
                    "acceptance_criteria": ["POST /users returns 201", "Email validation works"],
                },
                "repo_path": str(temp_repo),
                "spec_content": "User registration spec",
                "architecture": "Monolith with FastAPI",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"]

    def test_run_requires_task_and_repo(self, client: TestClient):
        response = client.post("/backend-code-v2/run", json={})
        assert response.status_code == 422


class TestBackendCodeV2StatusEndpoint:
    def test_status_returns_404_for_unknown_job(self, client: TestClient):
        response = client.get("/backend-code-v2/status/nonexistent-job-id")
        assert response.status_code == 404

    def test_status_returns_pending_after_create(self, client: TestClient, temp_repo: Path):
        run_resp = client.post(
            "/backend-code-v2/run",
            json={
                "task": {"title": "Test", "description": "test"},
                "repo_path": str(temp_repo),
            },
        )
        job_id = run_resp.json()["job_id"]

        time.sleep(0.2)

        status_resp = client.get(f"/backend-code-v2/status/{job_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("pending", "running", "completed", "failed")
        assert "progress" in data
        assert isinstance(data["completed_phases"], list)
        assert isinstance(data["microtasks_completed"], int)
        assert isinstance(data["microtasks_total"], int)

    def test_status_response_shape(self, client: TestClient, temp_repo: Path):
        run_resp = client.post(
            "/backend-code-v2/run",
            json={
                "task": {"title": "Shape test", "description": "test"},
                "repo_path": str(temp_repo),
            },
        )
        job_id = run_resp.json()["job_id"]

        time.sleep(0.1)

        status_resp = client.get(f"/backend-code-v2/status/{job_id}")
        data = status_resp.json()
        expected_keys = {
            "job_id",
            "status",
            "repo_path",
            "current_phase",
            "current_microtask",
            "progress",
            "microtasks_completed",
            "microtasks_total",
            "completed_phases",
            "error",
            "summary",
        }
        assert expected_keys.issubset(set(data.keys()))
