"""Tests for Temporal integration: dispatch routes always start a workflow, no thread fallback.

Routed through the in-memory ``FakeJobServiceClient`` so they run as unit tests
without a live job service.  A real-Temporal smoke run is still possible by
starting the stack with ``TEMPORAL_ADDRESS`` set, POSTing /run-team, killing
the API process, restarting it, and resuming the job — see ARCHITECTURE.md
section "Temporal (durable execution)" for env and setup.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

# Import after path setup
from software_engineering_team.api import main as _api_main  # noqa: E402

app = _api_main.app


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def temp_work_path(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "initial_spec.md").write_text("# Task Manager API\n\nREST API for tasks.")
    return work


@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=False)
def test_run_team_dispatches_to_temporal_even_when_disabled(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """POST /run-team always calls start_run_team_workflow — no thread fallback when Temporal is "disabled"."""
    r = client.post("/run-team", json={"repo_path": str(temp_work_path)})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    mock_start_workflow.assert_called_once()
    args = mock_start_workflow.call_args[0]
    assert args[0] == data["job_id"]
    assert args[1] == str(temp_work_path)


@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_run_team_with_temporal_starts_workflow(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """When Temporal is enabled, POST /run-team calls start_run_team_workflow."""
    r = client.post("/run-team", json={"repo_path": str(temp_work_path)})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    mock_start_workflow.assert_called_once()
    args = mock_start_workflow.call_args[0]
    assert args[0] == data["job_id"]
    assert args[1] == str(temp_work_path)


@patch("software_engineering_team.api.routes.jobs._preflight_sprint_scope")
@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_run_team_with_sprint_id_succeeds_under_temporal(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    mock_preflight_sprint_scope: MagicMock,
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /run-team with sprint_id succeeds (no more 400 from the deleted Temporal-sprint guard)
    and forwards sprint_id to start_run_team_workflow."""
    work = tmp_path / "sprint_work"
    work.mkdir()

    r = client.post("/run-team", json={"repo_path": str(work), "sprint_id": "sprint-1"})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    mock_start_workflow.assert_called_once()
    assert mock_start_workflow.call_args.kwargs["sprint_id"] == "sprint-1"


@patch("software_engineering_team.api.routes.jobs._preflight_sprint_scope")
@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_run_team_with_valid_sprint_id_succeeds(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    mock_preflight_sprint_scope: MagicMock,
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /run-team with a valid sprint_id succeeds (200) and forwards sprint_id to
    start_run_team_workflow — the old blanket-reject guard is gone now that V2 can
    actually synthesize sprint scope."""
    work = tmp_path / "sprint_work_v2_valid"
    work.mkdir()

    r = client.post("/run-team", json={"repo_path": str(work), "sprint_id": "sprint-1"})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    mock_start_workflow.assert_called_once()
    assert mock_start_workflow.call_args.kwargs["sprint_id"] == "sprint-1"


@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_run_team_with_invalid_sprint_id_400s_before_dispatch(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    client: TestClient,
    tmp_path: Path,
) -> None:
    """POST /run-team with an unplanned sprint_id still fails fast (400) via
    _preflight_sprint_scope before any job/workflow is created — the blanket V2 guard is
    gone, but real pre-dispatch validation takes its place."""
    work = tmp_path / "sprint_work_v2_invalid"
    work.mkdir()

    sprint_view = MagicMock()
    sprint_view.stories = []
    fake_store = MagicMock()
    fake_store.get_sprint_with_stories.return_value = sprint_view

    fake_module = MagicMock()
    fake_module.TERMINAL_STORY_STATUSES = {"done", "cancelled"}
    fake_module.ProductDeliveryStorageUnavailable = type("E", (Exception,), {})
    fake_module.get_store = lambda: fake_store

    with patch.dict("sys.modules", {"product_delivery": fake_module}):
        r = client.post("/run-team", json={"repo_path": str(work), "sprint_id": "sprint-1"})

    assert r.status_code == 400
    mock_start_workflow.assert_not_called()


@patch("software_engineering_team.temporal.start_workflow.start_retry_failed_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_retry_failed_with_temporal_starts_workflow(
    mock_temporal_enabled: MagicMock,
    mock_start_retry: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """When Temporal is enabled, POST /run-team/{id}/retry-failed calls start_retry_failed_workflow."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = "test-retry-job"
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(
        job_id,
        status="failed",
        failed_tasks=[{"task_id": "t1"}],
        _all_tasks={"t1": {"id": "t1", "title": "T1"}},
    )

    r = client.post(f"/run-team/{job_id}/retry-failed")
    assert r.status_code == 200
    mock_start_retry.assert_called_once_with(job_id)


@patch(
    "software_engineering_team.temporal.start_workflow.cancel_run_team_workflow", return_value=True
)
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_cancel_with_temporal_cancels_workflow(
    mock_temporal_enabled: MagicMock,
    mock_cancel_workflow: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """When Temporal is enabled, POST /run-team/{id}/cancel also calls cancel_run_team_workflow."""
    from software_engineering_team.shared.job_store import create_job

    job_id = "test-cancel-job"
    create_job(job_id, str(temp_work_path), job_type="run_team")

    r = client.post(f"/run-team/{job_id}/cancel")
    assert r.status_code == 200
    mock_cancel_workflow.assert_called_once_with(job_id)


def test_resumable_statuses_include_failed() -> None:
    """RESUMABLE_STATUSES includes JOB_STATUS_FAILED so failed jobs can be resumed."""
    from software_engineering_team.shared.job_store import JOB_STATUS_FAILED

    assert JOB_STATUS_FAILED in _api_main.RESUMABLE_STATUSES


@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("shared.temporal.client.is_temporal_enabled", return_value=True)
def test_resume_failed_job_starts_workflow(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """A job in status failed can be resumed; POST resume starts RunTeamWorkflow and job becomes running."""
    from software_engineering_team.shared.job_store import create_job, get_job, update_job

    job_id = "test-resume-failed"
    create_job(job_id, str(temp_work_path), job_type="run_team")
    update_job(job_id, status=_api_main.JOB_STATUS_FAILED)

    r = client.post(f"/run-team/{job_id}/resume")
    assert r.status_code == 200
    mock_start_workflow.assert_called_once()
    args = mock_start_workflow.call_args[0]
    assert args[0] == job_id
    assert args[1] == str(temp_work_path)
    job = get_job(job_id)
    assert job is not None
    assert job.get("status") == _api_main.JOB_STATUS_RUNNING
