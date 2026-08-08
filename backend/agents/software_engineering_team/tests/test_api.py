"""Tests for the run-team API endpoint.

Routed through the in-memory ``FakeJobServiceClient`` via the autouse
``_autouse_patched_job_store`` fixture, so a job-store call made while a
test is executing lands in a per-test in-memory dict. That alone is not
enough for these endpoints: they dispatch unconditionally to Temporal's
``start_run_team_workflow``/``start_retry_failed_workflow``, which raise
immediately in tests (no real Temporal client is configured), turning every
would-be-200 response into a 503. The autouse ``_stub_background_workflow``
fixture below stubs those two dispatch calls to no-ops so the route handlers'
synchronous behavior (status codes, the immediate job-store write) can be
asserted without a live Temporal deployment.
"""

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
from software_engineering_team.api import main as _api_main  # noqa: E402

app = _api_main.app


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


@pytest.fixture(autouse=True)
def _stub_background_workflow(monkeypatch):
    """Stub the Temporal dispatch calls POST /run-team (and resume/restart/retry) make.

    These routes call start_run_team_workflow/start_retry_failed_workflow
    unconditionally now — no thread fallback. Without a real Temporal client
    those raise, which the route's try/except turns into a 503. Stubbing them
    to no-ops keeps every synchronous assertion in this file (status codes,
    the immediate job-store write the route handler makes before dispatching)
    meaningful without a live Temporal deployment.
    """
    import software_engineering_team.temporal.start_workflow as _start_workflow

    monkeypatch.setattr(_start_workflow, "start_run_team_workflow", lambda *a, **k: None)
    monkeypatch.setattr(_start_workflow, "start_retry_failed_workflow", lambda *a, **k: None)


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

    from shared.dev_models.models import ProductRequirements

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
        patch("software_engineering_team.spec_parser.parse_spec_with_llm", return_value=fake_reqs),
        patch(
            "software_engineering_team.architect_agents.architecture_expert.ArchitectureExpertAgent",
            return_value=mock_agent,
        ),
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

    # The background pipeline is stubbed to a no-op (see _stub_background_workflow),
    # so status never leaves "running" here — a handful of quick polls is enough
    # to prove the polling mechanism works without waiting out a real completion
    # that (per the LLM_PROVIDER=dummy branch below) isn't guaranteed anyway.
    data = None
    for _ in range(5):
        r = client.get(f"/run-team/{job_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("pending", "running", "completed", "failed")
        if data["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)

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
    create_job(job_id, str(temp_work_path), job_type="backend_code_v2")

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
    create_job(job_id, str(temp_work_path), job_type="backend_code_v2")
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


def test_get_job_status_exposes_activity_and_timestamps(
    client: TestClient, temp_work_path: Path
) -> None:
    """GET /run-team/{job_id} round-trips current_activity, last_activity_at, and the
    job-service timestamps so the UI can render sub-agent progress + a stall warning."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(
        job_id,
        current_activity={
            "agent": "code_review",
            "step": "reviewing",
            "detail": "chunk 2/5: src/app.py",
            "fraction": 0.4,
            "task_id": "t1",
            "task_title": "T1",
        },
        last_activity_at="2026-06-10T12:00:00+00:00",
    )

    r = client.get(f"/run-team/{job_id}")
    assert r.status_code == 200
    data = r.json()
    activity = data["current_activity"]
    assert activity["agent"] == "code_review"
    assert activity["step"] == "reviewing"
    assert activity["detail"] == "chunk 2/5: src/app.py"
    assert activity["fraction"] == 0.4
    assert data["last_activity_at"] == "2026-06-10T12:00:00+00:00"


def test_get_job_status_fresh_job_has_activity_baseline(
    client: TestClient, temp_work_path: Path
) -> None:
    """A fresh job has no current_activity but DOES carry a creation-time
    last_activity_at baseline (stamped centrally by the job service), so a job that
    hangs while still pending is detectable by the stall warning."""
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")

    r = client.get(f"/run-team/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["current_activity"] is None
    assert data["last_activity_at"] is not None


def test_get_job_status_includes_server_time(client: TestClient, temp_work_path: Path) -> None:
    """server_time is the backend clock the UI computes staleness against — present
    and ISO-parseable on every response (browser clock skew immunity)."""
    from datetime import datetime

    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")

    r = client.get(f"/run-team/{job_id}")
    assert r.status_code == 200
    server_time = r.json()["server_time"]
    assert server_time is not None
    datetime.fromisoformat(server_time)


def test_get_job_status_clamps_progress(client: TestClient, temp_work_path: Path) -> None:
    """Progress is clamped to [0, 100] via shared.hitl.progress.coerce_progress, so a corrupt
    stored value can no longer render an out-of-range bar. This is an intentional behavior
    change from SE's previous unclamped int() coercion."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")

    update_job(job_id, progress=250)
    assert client.get(f"/run-team/{job_id}").json()["progress"] == 100

    update_job(job_id, progress=-5)
    assert client.get(f"/run-team/{job_id}").json()["progress"] == 0


def test_get_job_status_preserves_recommendation_and_allow_multiple(
    client: TestClient, temp_work_path: Path
) -> None:
    """The status route materializes pending questions via shared.hitl.pending_questions_from_raw
    (model_validate), so recommendation/allow_multiple survive the round-trip. The previous
    hand-enumeration silently dropped both fields."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[
            {
                "id": "q1",
                "question_text": "Which auth?",
                "recommendation": "Use OAuth",
                "allow_multiple": True,
                "options": [{"id": "oauth", "label": "OAuth"}],
                "required": True,
                "source": "tech_lead",
            }
        ],
    )

    pq = client.get(f"/run-team/{job_id}").json()["pending_questions"][0]
    assert pq["recommendation"] == "Use OAuth"
    assert pq["allow_multiple"] is True
    assert pq["source"] == "tech_lead"


def test_get_job_status_malformed_activity_value_degrades_to_none(
    client: TestClient, temp_work_path: Path
) -> None:
    """A dict current_activity with a malformed field value (non-numeric fraction)
    must degrade the optional detail to None — never 500 the whole status endpoint."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, current_activity={"agent": "code_review", "fraction": "n/a"})

    r = client.get(f"/run-team/{job_id}")
    assert r.status_code == 200
    assert r.json()["current_activity"] is None


def test_get_job_status_non_dict_current_activity_coerced_to_none(
    client: TestClient, temp_work_path: Path
) -> None:
    """A malformed (non-dict) current_activity value must not break the status endpoint."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, current_activity="not-a-dict")

    r = client.get(f"/run-team/{job_id}")
    assert r.status_code == 200
    assert r.json()["current_activity"] is None


# ---------------------------------------------------------------------------
# Unit tests for the shared path-validation / temporal-guard helpers extracted
# from run_team / resume / restart (single-contract collapse).
# ---------------------------------------------------------------------------


def test_resolve_repo_path_non_sprint_returns_resolved_path(temp_work_path: Path) -> None:
    """Non-sprint mode applies the spec-gated validator and returns the resolved Path."""
    from software_engineering_team.api.routes.jobs import _resolve_repo_path

    resolved = _resolve_repo_path(str(temp_work_path), None)
    assert resolved == temp_work_path.resolve()


def test_resolve_repo_path_sprint_mode_skips_spec_gate(tmp_path: Path) -> None:
    """Sprint mode validates a code-only dir (no initial_spec.md) without a 400."""
    from software_engineering_team.api.routes.jobs import _resolve_repo_path

    code_only = tmp_path / "code_only"
    code_only.mkdir()
    resolved = _resolve_repo_path(str(code_only), "sprint-1")
    assert resolved == code_only.resolve()


def test_resolve_repo_path_non_sprint_missing_spec_is_400(tmp_path: Path) -> None:
    """Non-sprint mode rejects a code-only dir (missing spec) as a 400."""
    from fastapi import HTTPException

    from software_engineering_team.api.routes.jobs import _resolve_repo_path

    code_only = tmp_path / "code_only"
    code_only.mkdir()
    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path(str(code_only), None)
    assert exc.value.status_code == 400


def test_resolve_repo_path_invalid_path_is_400() -> None:
    """A non-existent path surfaces as a 400 regardless of sprint mode."""
    from fastapi import HTTPException

    from software_engineering_team.api.routes.jobs import _resolve_repo_path

    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path("/nonexistent/xyz", None)
    assert exc.value.status_code == 400
