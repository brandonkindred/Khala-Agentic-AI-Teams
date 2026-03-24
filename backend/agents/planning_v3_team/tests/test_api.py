"""API tests for Planning V3: run returns job_id, status/result shapes."""

import sys
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from planning_v3_team.api.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def temp_repo(tmp_path):
    (tmp_path / "plan").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_run_returns_job_id(client, temp_repo):
    r = client.post(
        "/run",
        json={
            "repo_path": temp_repo,
            "client_name": "Test",
            "initial_brief": "Small app",
            "use_product_analysis": False,
            "use_planning_v2": False,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data.get("status") == "running"


def test_run_400_if_repo_not_dir(client):
    r = client.post("/run", json={"repo_path": "/nonexistent/path/12345"})
    assert r.status_code == 400


def test_status_404(client):
    r = client.get("/status/nonexistent-job-id")
    assert r.status_code == 404


def test_status_after_run(client, temp_repo):
    run_r = client.post(
        "/run",
        json={"repo_path": temp_repo, "use_product_analysis": False, "use_planning_v2": False},
    )
    job_id = run_r.json()["job_id"]
    r = client.get(f"/status/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == job_id
    assert data["status"] in ("pending", "running", "completed", "failed")
    assert "progress" in data


def test_result_404(client):
    r = client.get("/result/nonexistent-job-id")
    assert r.status_code == 404


def test_jobs_list(client):
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "jobs" in r.json()
