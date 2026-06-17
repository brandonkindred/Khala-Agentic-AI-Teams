"""API tests for Planning V3: run returns job_id, status/result shapes.

Hits the team API which calls the real job service.  Marked integration
pending follow-up.
"""

import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from planning_v3_team.api.main import app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Keep auto-created workspaces inside the test temp dir, never the repo or /.
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "cache"))
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


def test_run_without_repo_path_creates_workspace(client, tmp_path):
    r = client.post(
        "/run",
        json={"initial_brief": "Greenfield app", "use_product_analysis": False},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    # The workspace is created synchronously before the response, under AGENT_CACHE.
    status = client.get(f"/status/{job_id}").json()
    workspace = Path(status["repo_path"]).resolve()
    root = (tmp_path / "cache" / "planning_v3").resolve()
    assert workspace.is_dir()
    assert workspace.is_relative_to(root)


def test_run_with_git_url_repo_path(client, tmp_path):
    r = client.post(
        "/run",
        json={
            "repo_path": "git@github.com:owner/repo.git",
            "initial_brief": "Home maintenance tracker",
            "use_product_analysis": False,
        },
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    # The git URL resolves to a server-side workspace named after the repo,
    # confined under the cache root (no clone happens).
    workspace = Path(client.get(f"/status/{job_id}").json()["repo_path"]).resolve()
    root = (tmp_path / "cache" / "planning_v3").resolve()
    assert workspace.is_dir()
    assert workspace.is_relative_to(root)


def test_run_confines_traversal_path(client, tmp_path):
    # An explicit traversal path is confined under AGENT_CACHE, not used verbatim.
    r = client.post(
        "/run",
        json={"repo_path": "../../outside", "initial_brief": "x", "use_product_analysis": False},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    workspace = Path(client.get(f"/status/{job_id}").json()["repo_path"]).resolve()
    root = (tmp_path / "cache" / "planning_v3").resolve()
    assert workspace.is_relative_to(root)


def test_run_400_when_workspace_segment_is_file(client, tmp_path):
    # Acceptance criterion: an existing file where the workspace would be created
    # returns 400 from /run. Pre-create a regular file at the user-derived
    # segment under AGENT_CACHE so the handler's synchronous workspace mkdir
    # collides with a non-directory and surfaces a clean 400.
    root = tmp_path / "cache" / "planning_v3"
    root.mkdir(parents=True, exist_ok=True)
    (root / "collide").write_text("x", encoding="utf-8")
    r = client.post(
        "/run",
        json={
            "repo_path": "/client/path/collide",
            "initial_brief": "x",
            "use_product_analysis": False,
        },
    )
    assert r.status_code == 400


def test_run_with_spec_only_no_brief(client):
    r = client.post(
        "/run",
        json={"spec_content": "# Full spec", "use_product_analysis": False},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_run_422_without_brief_or_spec(client):
    r = client.post("/run", json={"use_product_analysis": False})
    assert r.status_code == 422


def test_status_404(client):
    r = client.get("/status/nonexistent-job-id")
    assert r.status_code == 404


def test_status_after_run(client, temp_repo):
    run_r = client.post(
        "/run",
        json={
            "repo_path": temp_repo,
            "initial_brief": "x",
            "use_product_analysis": False,
            "use_planning_v2": False,
        },
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
