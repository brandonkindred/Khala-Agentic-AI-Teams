"""Tests for Temporal integration: when enabled, API starts workflows instead of threads.

Optional integration test with a real Temporal server (e.g. Docker): start the stack with
TEMPORAL_ADDRESS set, POST /run-team to start a job, kill the API process, restart it,
then verify the workflow continues or the job can be resumed via POST /run-team/{id}/resume.
See ARCHITECTURE.md section \"Temporal (durable execution)\" for env and setup."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

# Import after path setup
import importlib.util
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
    work = tmp_path / "work"
    work.mkdir()
    (work / "initial_spec.md").write_text("# Task Manager API\n\nREST API for tasks.")
    return work


@patch("software_engineering_team.temporal.client.is_temporal_enabled", return_value=False)
def test_run_team_without_temporal_starts_thread(
    mock_temporal_enabled: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """When Temporal is not enabled, POST /run-team starts a background thread."""
    with patch("threading.Thread", wraps=_api_main.threading.Thread) as mock_thread_class:
        r = client.post("/run-team", json={"repo_path": str(temp_work_path)})
        assert r.status_code == 200
        assert "job_id" in r.json()
        # Thread should have been constructed (target=_run_orchestrator_background)
        assert mock_thread_class.call_count >= 1
        found_orchestrator_thread = any(
            (c[1].get("target") == _api_main._run_orchestrator_background) or
            (c[0] and len(c[0]) >= 2 and c[0][0] == _api_main._run_orchestrator_background)
            for c in mock_thread_class.call_args_list
        )
        assert found_orchestrator_thread


@patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow")
@patch("software_engineering_team.temporal.client.is_temporal_enabled", return_value=True)
def test_run_team_with_temporal_starts_workflow(
    mock_temporal_enabled: MagicMock,
    mock_start_workflow: MagicMock,
    client: TestClient,
    temp_work_path: Path,
) -> None:
    """When Temporal is enabled, POST /run-team calls start_run_team_workflow and does not start orchestrator thread."""
    with patch("threading.Thread", wraps=_api_main.threading.Thread) as mock_thread_class:
        r = client.post("/run-team", json={"repo_path": str(temp_work_path)})
        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data
        mock_start_workflow.assert_called_once()
        args = mock_start_workflow.call_args[0]
        assert args[0] == data["job_id"]
        assert args[1] == str(temp_work_path)
        # No thread for orchestrator (thread_calls with target _run_orchestrator_background)
        orchestrator_thread_calls = [
            c for c in mock_thread_class.call_args_list
            if c[1].get("target") == _api_main._run_orchestrator_background
        ]
        assert len(orchestrator_thread_calls) == 0


@patch("software_engineering_team.temporal.start_workflow.start_retry_failed_workflow")
@patch("software_engineering_team.temporal.client.is_temporal_enabled", return_value=True)
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
    update_job(job_id, status="failed", failed_tasks=[{"task_id": "t1"}], _all_tasks={"t1": {"id": "t1", "title": "T1"}})

    r = client.post(f"/run-team/{job_id}/retry-failed")
    assert r.status_code == 200
    mock_start_retry.assert_called_once_with(job_id)


@patch("software_engineering_team.temporal.start_workflow.cancel_run_team_workflow", return_value=True)
@patch("software_engineering_team.temporal.client.is_temporal_enabled", return_value=True)
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
@patch("software_engineering_team.temporal.client.is_temporal_enabled", return_value=True)
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
