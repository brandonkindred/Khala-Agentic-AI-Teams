"""Endpoint-level tests for Product Analysis routes.

Validates request-side branches (400/404, payload coercion, error mapping)
without spinning up the actual LLM-driven workflow. The background-task
launch is pragma'd out separately as integration-only.
"""

from __future__ import annotations

import sys
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
    """Stub the Temporal dispatch call the run/start-from-spec routes make.

    Both routes call start_standalone_workflow unconditionally — no thread
    fallback. Without a real Temporal client that raises, which the route's
    try/except turns into a 503. Stubbing it to a no-op keeps these unit
    tests' endpoint-contract assertions meaningful without a live Temporal
    deployment.
    """
    import software_engineering_team.temporal.start_workflow as _start_workflow

    monkeypatch.setattr(_start_workflow, "start_standalone_workflow", lambda *a, **k: None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_run_product_analysis_400_when_repo_path_missing(client, tmp_path: Path):
    """run_product_analysis rejects a non-existent repo path."""
    missing = tmp_path / "does-not-exist"
    resp = client.post(
        "/product-analysis/run",
        json={"repo_path": str(missing), "spec_content": "# Spec"},
    )
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]


def test_run_product_analysis_400_when_no_spec_and_no_spec_file(client, tmp_path: Path):
    """When neither spec_content nor a spec file is present, return 400."""
    repo = tmp_path / "repo"
    repo.mkdir()
    resp = client.post(
        "/product-analysis/run",
        json={"repo_path": str(repo)},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "No spec file found" in detail or "spec" in detail.lower()


def test_run_product_analysis_accepts_provided_spec_content(client, tmp_path: Path):
    """When spec_content is provided, the endpoint reaches the launch try-block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    resp = client.post(
        "/product-analysis/run",
        json={"repo_path": str(repo), "spec_content": "# Spec"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["job_id"]


def test_start_from_spec_400_for_invalid_project_name(client):
    """Project names with spaces or special chars are rejected."""
    resp = client.post(
        "/product-analysis/start-from-spec",
        json={"project_name": "bad name", "spec_content": "# Spec"},
    )
    assert resp.status_code == 400
    assert "project_name" in resp.json()["detail"]


def test_start_from_spec_creates_project_and_starts(monkeypatch, tmp_path: Path, client):
    """start_from_spec writes the spec file and reaches the launch try-block."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    resp = client.post(
        "/product-analysis/start-from-spec",
        json={"project_name": "myproj", "spec_content": "# Spec\nFeature"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"]
    # Workspace was created
    proj_dir = tmp_path / "projects" / "myproj"
    assert proj_dir.exists()
    assert (proj_dir / _api_main.SPEC_FILENAME).read_text(encoding="utf-8").startswith("# Spec")


def test_start_from_spec_keeps_project_dir_on_dispatch_failure(
    monkeypatch, tmp_path: Path, client
):
    """A dispatch exception (timeout, RPC error, no client, ...) never proves the
    workflow wasn't scheduled, so the project dir is deliberately left in place —
    deleting it could race a workflow still running against a path a retry just
    recreated. A retry under the same name correctly 400s as "already exists"."""
    import software_engineering_team.temporal.start_workflow as start_workflow

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def _raise(*a, **k):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(start_workflow, "start_standalone_workflow", _raise)

    resp = client.post(
        "/product-analysis/start-from-spec",
        json={"project_name": "myproj3", "spec_content": "# Spec\nFeature"},
    )
    assert resp.status_code == 503
    proj_dir = tmp_path / "projects" / "myproj3"
    assert proj_dir.exists()

    retry_resp = client.post(
        "/product-analysis/start-from-spec",
        json={"project_name": "myproj3", "spec_content": "# Spec\nFeature"},
    )
    assert retry_resp.status_code == 400


def test_start_from_spec_400_when_project_already_exists(monkeypatch, tmp_path: Path, client):
    """If the project directory already exists, return 400."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True)
    (projects_root / "myproj").mkdir()
    resp = client.post(
        "/product-analysis/start-from-spec",
        json={"project_name": "myproj", "spec_content": "# Spec"},
    )
    assert resp.status_code == 400


def test_get_product_analysis_status_404_when_job_missing(client):
    """Unknown job_id → 404."""
    resp = client.get("/product-analysis/status/this-job-does-not-exist")
    assert resp.status_code == 404


def test_get_product_analysis_status_returns_data_for_existing_job(client, fake_job_client):
    """Existing product_analysis job → 200 with expected fields."""
    job_id = "job-pa-1"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    fake_job_client.update_job(
        job_id,
        status="completed",
        progress=100,
        current_phase="spec_cleanup",
        iterations=2,
    )
    resp = client.get(f"/product-analysis/status/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "completed"


def test_submit_product_analysis_answers_404_when_job_missing(client):
    resp = client.post(
        "/product-analysis/job-missing/answers",
        json={"answers": []},
    )
    assert resp.status_code == 404


def test_submit_product_analysis_answers_rejects_wrong_job_type(client, fake_job_client):
    """Endpoint is product_analysis-only; reject run_team jobs."""
    job_id = "job-rt-1"
    fake_job_client.create_job(job_id, job_type="run_team", repo_path="/tmp/repo")
    fake_job_client.update_job(job_id, waiting_for_answers=True)
    resp = client.post(f"/product-analysis/{job_id}/answers", json={"answers": []})
    assert resp.status_code == 400


def test_submit_product_analysis_answers_rejects_when_not_waiting(client, fake_job_client):
    job_id = "job-pa-2"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    # Do NOT set waiting_for_answers → 400
    resp = client.post(f"/product-analysis/{job_id}/answers", json={"answers": []})
    assert resp.status_code == 400


def test_submit_product_analysis_answers_rejects_missing_required_question(client, fake_job_client):
    job_id = "job-pa-3"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True}],
    )
    # No answer provided → 400 missing required
    resp = client.post(f"/product-analysis/{job_id}/answers", json={"answers": []})
    assert resp.status_code == 400
    assert "Missing answers" in resp.json()["detail"]


def test_submit_product_analysis_answers_rejects_unknown_question_id(client, fake_job_client):
    job_id = "job-pa-4"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": False}],
    )
    resp = client.post(
        f"/product-analysis/{job_id}/answers",
        json={"answers": [{"question_id": "unknown-id", "selected_option_id": "yes"}]},
    )
    assert resp.status_code == 400
    assert "Unknown question" in resp.json()["detail"]


def test_submit_product_analysis_answers_accepts_valid_answers(client, fake_job_client):
    job_id = "job-pa-5"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[
            {"id": "q1", "required": True},
            {"id": "q2", "required": False},
        ],
    )
    resp = client.post(
        f"/product-analysis/{job_id}/answers",
        json={
            "answers": [
                {"question_id": "q1", "selected_option_id": "yes"},
                {"question_id": "q2", "selected_option_id": "no"},
            ]
        },
    )
    # Returns 200 with product-analysis status
    assert resp.status_code == 200


def test_auto_answer_product_analysis_404_when_job_missing(client):
    resp = client.post(
        "/product-analysis/job-missing/auto-answer/q1",
    )
    assert resp.status_code == 404


def test_auto_answer_product_analysis_400_when_wrong_job_type(client, fake_job_client):
    job_id = "job-rt-2"
    fake_job_client.create_job(job_id, job_type="run_team", repo_path="/tmp/repo")
    resp = client.post(
        f"/product-analysis/{job_id}/auto-answer/q1",
    )
    assert resp.status_code == 400


def test_auto_answer_product_analysis_404_when_question_unknown(client, fake_job_client):
    job_id = "job-pa-6"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    fake_job_client.update_job(job_id, pending_questions=[{"id": "q1"}])
    resp = client.post(
        f"/product-analysis/{job_id}/auto-answer/q-unknown",
    )
    assert resp.status_code == 404


def test_auto_answer_product_analysis_422_when_no_options(client, fake_job_client):
    job_id = "job-pa-7"
    fake_job_client.create_job(job_id, job_type="product_analysis", repo_path="/tmp/repo")
    fake_job_client.update_job(
        job_id,
        pending_questions=[{"id": "q1", "question_text": "What fields?", "options": []}],
    )
    resp = client.post(f"/product-analysis/{job_id}/auto-answer/q1")
    assert resp.status_code == 422
