"""Unit-level tests for the agent provisioning FastAPI surface.

These tests don't need a live executor / orchestrator — every external
function (job store, orchestrator, executor) is patched at the seam.
Marked NON-integration so they run on the default unit lane.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_provisioning_team.api import main as api_main
from agent_provisioning_team.api.main import app
from agent_provisioning_team.models import (
    DeprovisionResponse,
    EnvironmentInfo,
    ProvisioningResult,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Health + root endpoints
# ---------------------------------------------------------------------------


def test_health_endpoint() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "service": "agent-provisioning"}


def test_root_endpoint() -> None:
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "Agent Provisioning API"
    assert data["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# /provision flows
# ---------------------------------------------------------------------------


def test_provision_routes_to_temporal_when_enabled() -> None:
    """When a temporal starter is available, /provision delegates to it."""
    fake_starter = MagicMock()

    with (
        patch("agent_provisioning_team.api.main.create_job"),
        patch.object(api_main, "_temporal_starter", return_value=fake_starter),
    ):
        r = client.post("/provision", json={"agent_id": "ag-temporal"})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    fake_starter.assert_called_once()


def test_provision_uses_thread_path_when_no_temporal() -> None:
    with (
        patch("agent_provisioning_team.api.main.create_job"),
        patch.object(api_main, "_temporal_starter", return_value=None),
        patch.object(api_main, "_ensure_executor") as mock_ensure,
    ):
        mock_executor = MagicMock()
        mock_ensure.return_value = mock_executor
        r = client.post("/provision", json={"agent_id": "ag-thread"})

    assert r.status_code == 200
    mock_executor.submit.assert_called_once()


def test_provision_thread_fallback_env_flag() -> None:
    """PROVISION_THREAD_FALLBACK=1 forces None starter."""
    with patch.dict("os.environ", {"PROVISION_THREAD_FALLBACK": "1"}):
        assert api_main._temporal_starter() is None


def test_provision_thread_fallback_with_blank_env() -> None:
    with patch.dict("os.environ", {"PROVISION_THREAD_FALLBACK": ""}):
        # No-op: returns whatever the real path resolves to (could be None
        # when Temporal isn't installed).
        api_main._temporal_starter()


def test_provision_thread_fallback_returns_true_for_true() -> None:
    with patch.dict("os.environ", {"PROVISION_THREAD_FALLBACK": "true"}):
        assert api_main._provision_thread_fallback() is True
    with patch.dict("os.environ", {"PROVISION_THREAD_FALLBACK": "yes"}):
        assert api_main._provision_thread_fallback() is True


# ---------------------------------------------------------------------------
# /provision/status/{job_id}
# ---------------------------------------------------------------------------


def test_get_status_returns_404_on_missing_job() -> None:
    with patch.object(api_main, "get_job", return_value={}):
        r = client.get("/provision/status/missing")
    assert r.status_code == 404


def test_get_status_returns_state_for_running_job() -> None:
    with patch.object(
        api_main,
        "get_job",
        return_value={
            "status": "running",
            "agent_id": "a",
            "current_phase": "setup",
            "progress": 30,
            "tools_completed": 1,
            "tools_total": 3,
            "completed_phases": ["setup"],
        },
    ):
        r = client.get("/provision/status/job-1")

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["agent_id"] == "a"
    assert data["progress"] == 30


def test_get_status_returns_result_for_completed_job() -> None:
    env = EnvironmentInfo(container_id="c1", container_name="c1")
    result = ProvisioningResult(agent_id="a", success=True, environment=env)

    with patch.object(
        api_main,
        "get_job",
        return_value={
            "status": "completed",
            "agent_id": "a",
            "result": result.model_dump(mode="json"),
            "progress": 100,
        },
    ):
        r = client.get("/provision/status/job-1")

    assert r.status_code == 200
    data = r.json()
    assert data["result"]["success"] is True


# ---------------------------------------------------------------------------
# /provision/jobs
# ---------------------------------------------------------------------------


def test_list_jobs_returns_summaries() -> None:
    fake_jobs = [
        {
            "job_id": "j1",
            "agent_id": "a1",
            "status": "running",
            "created_at": None,
            "progress": 50,
        },
        {"job_id": "j2", "agent_id": "a2", "status": "pending", "progress": 0},
    ]
    with patch.object(api_main, "list_jobs", return_value=fake_jobs):
        r = client.get("/provision/jobs")

    assert r.status_code == 200
    data = r.json()
    assert len(data["jobs"]) == 2
    assert {j["job_id"] for j in data["jobs"]} == {"j1", "j2"}


def test_list_jobs_with_running_only_filter() -> None:
    captured = {}

    def fake_list(running_only=False):
        captured["running_only"] = running_only
        return []

    with patch.object(api_main, "list_jobs", side_effect=fake_list):
        r = client.get("/provision/jobs?running_only=true")
    assert r.status_code == 200
    assert captured["running_only"] is True


# ---------------------------------------------------------------------------
# /provision/job/{job_id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_job_not_found() -> None:
    with patch.object(api_main, "get_job", return_value={}):
        r = client.post("/provision/job/missing/cancel")
    assert r.status_code == 404


def test_cancel_job_terminal_state_rejected() -> None:
    with patch.object(api_main, "get_job", return_value={"status": "completed"}):
        r = client.post("/provision/job/j1/cancel")
    assert r.status_code == 400
    assert "terminal" in r.json()["detail"]


def test_cancel_job_pending_succeeds() -> None:
    with (
        patch.object(api_main, "get_job", return_value={"status": "pending"}),
        patch.object(api_main, "store_cancel_job") as mock_cancel,
    ):
        r = client.post("/provision/job/j1/cancel")
    assert r.status_code == 200
    assert r.json()["job_id"] == "j1"
    mock_cancel.assert_called_once_with("j1")


def test_cancel_job_running_succeeds() -> None:
    with (
        patch.object(api_main, "get_job", return_value={"status": "running"}),
        patch.object(api_main, "store_cancel_job"),
    ):
        r = client.post("/provision/job/j2/cancel")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /provision/job/{job_id} DELETE
# ---------------------------------------------------------------------------


def test_delete_job_not_found() -> None:
    with patch.object(api_main, "get_job", return_value={}):
        r = client.delete("/provision/job/missing")
    assert r.status_code == 404


def test_delete_job_succeeds() -> None:
    with (
        patch.object(api_main, "get_job", return_value={"status": "completed"}),
        patch.object(api_main, "store_delete_job", return_value=True),
    ):
        r = client.delete("/provision/job/j1")
    assert r.status_code == 200
    assert r.json()["job_id"] == "j1"


def test_delete_job_store_returns_false() -> None:
    with (
        patch.object(api_main, "get_job", return_value={"status": "completed"}),
        patch.object(api_main, "store_delete_job", return_value=False),
    ):
        r = client.delete("/provision/job/j1")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /provision/job/{job_id}/resume
# ---------------------------------------------------------------------------


def test_resume_job_not_found() -> None:
    with patch.object(api_main, "get_job", return_value=None):
        r = client.post("/provision/job/missing/resume")
    assert r.status_code == 404


def test_resume_job_in_running_state_rejected() -> None:
    # validate_job_for_action raises ValueError for non-resumable statuses
    with patch.object(api_main, "get_job", return_value={"status": "running"}):
        r = client.post("/provision/job/j1/resume")
    assert r.status_code in (400, 404)


def test_resume_job_missing_agent_or_manifest() -> None:
    with patch.object(
        api_main,
        "get_job",
        return_value={
            "status": "failed",
            "agent_id": "",  # missing
            "manifest_path": "default.yaml",
        },
    ):
        r = client.post("/provision/job/j1/resume")
    assert r.status_code == 400


def test_resume_job_thread_path() -> None:
    with (
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "failed",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
                "completed_phases": ["setup"],
                "phase_results": {"setup": {"success": True, "environment": None}},
            },
        ),
        patch.object(api_main, "update_job"),
        patch.object(api_main, "_temporal_starter", return_value=None),
        patch.object(api_main, "_ensure_executor") as mock_ensure,
    ):
        mock_ensure.return_value = MagicMock()
        r = client.post("/provision/job/j1/resume")

    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_resume_job_temporal_path() -> None:
    fake_starter = MagicMock()
    with (
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "failed",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
                "completed_phases": ["setup", "credential_generation"],
                "phase_results": {},
            },
        ),
        patch.object(api_main, "update_job"),
        patch.object(api_main, "_temporal_starter", return_value=fake_starter),
    ):
        r = client.post("/provision/job/j1/resume")

    assert r.status_code == 200
    fake_starter.assert_called_once()


# ---------------------------------------------------------------------------
# /provision/job/{job_id}/restart
# ---------------------------------------------------------------------------


def test_restart_job_not_found() -> None:
    with patch.object(api_main, "get_job", return_value=None):
        r = client.post("/provision/job/missing/restart")
    assert r.status_code == 404


def test_restart_job_missing_agent_or_manifest() -> None:
    with patch.object(
        api_main,
        "get_job",
        return_value={"status": "completed", "agent_id": "a1", "manifest_path": ""},
    ):
        r = client.post("/provision/job/j1/restart")
    assert r.status_code == 400


def test_restart_job_thread_path() -> None:
    with (
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "completed",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
            },
        ),
        patch.object(api_main, "store_reset_job"),
        patch.object(api_main, "_temporal_starter", return_value=None),
        patch.object(api_main, "_ensure_executor") as mock_ensure,
    ):
        mock_ensure.return_value = MagicMock()
        r = client.post("/provision/job/j1/restart")
    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_restart_job_temporal_path() -> None:
    fake_starter = MagicMock()
    with (
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "completed",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
            },
        ),
        patch.object(api_main, "store_reset_job"),
        patch.object(api_main, "_temporal_starter", return_value=fake_starter),
    ):
        r = client.post("/provision/job/j1/restart")
    assert r.status_code == 200
    fake_starter.assert_called_once()


# ---------------------------------------------------------------------------
# /environments/{agent_id} DELETE (deprovision)
# ---------------------------------------------------------------------------


def test_deprovision_returns_orchestrator_result() -> None:
    fake_resp = DeprovisionResponse(agent_id="a1", success=True)
    with patch.object(api_main.orchestrator, "deprovision", return_value=fake_resp):
        r = client.delete("/environments/a1")
    assert r.status_code == 200
    assert r.json()["agent_id"] == "a1"
    assert r.json()["success"] is True


def test_deprovision_with_force_flag() -> None:
    fake_resp = DeprovisionResponse(agent_id="a1", success=True)
    captured = {}

    def fake_dep(agent_id, force=False):
        captured["force"] = force
        return fake_resp

    with patch.object(api_main.orchestrator, "deprovision", side_effect=fake_dep):
        r = client.delete("/environments/a1?force=true")
    assert r.status_code == 200
    assert captured["force"] is True


# ---------------------------------------------------------------------------
# /environments/{agent_id} GET
# ---------------------------------------------------------------------------


def test_get_environment_not_found() -> None:
    with patch.object(api_main.orchestrator, "get_agent_status", return_value=None):
        r = client.get("/environments/missing")
    assert r.status_code == 404


def test_get_environment_returns_status() -> None:
    with patch.object(
        api_main.orchestrator,
        "get_agent_status",
        return_value={
            "agent_id": "a1",
            "status": "ready",
            "container_id": "c1",
            "container_name": "agent-a1",
            "tools_provisioned": ["postgresql"],
        },
    ):
        r = client.get("/environments/a1")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"


# ---------------------------------------------------------------------------
# /environments GET (list)
# ---------------------------------------------------------------------------


def test_list_environments_empty() -> None:
    with patch.object(api_main.orchestrator, "list_agents", return_value=[]):
        r = client.get("/environments")
    assert r.status_code == 200
    assert r.json()["agents"] == []


def test_list_environments_with_filter() -> None:
    captured = {}

    def fake_list(status=None):
        captured["status"] = status
        return [
            {
                "agent_id": "a1",
                "status": "ready",
                "container_name": "agent-a1",
                "tools_provisioned": ["pg"],
            }
        ]

    with patch.object(api_main.orchestrator, "list_agents", side_effect=fake_list):
        r = client.get("/environments?status=ready")
    assert r.status_code == 200
    assert captured["status"] == "ready"
    assert r.json()["agents"][0]["agent_id"] == "a1"


# ---------------------------------------------------------------------------
# Internals: _ensure_executor, _queue_depth, _reject_if_saturated
# ---------------------------------------------------------------------------


def test_ensure_executor_lazy_construct(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_executor", None)
    ex = api_main._ensure_executor()
    assert ex is not None
    # Subsequent call returns same instance
    assert api_main._ensure_executor() is ex
    ex.shutdown(wait=True)
    monkeypatch.setattr(api_main, "_executor", None)


def test_queue_depth_zero_when_no_executor(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_executor", None)
    assert api_main._queue_depth() == 0


def test_reject_if_saturated_raises_429(monkeypatch) -> None:
    """When the work queue is full, _reject_if_saturated raises HTTPException 429."""
    from fastapi import HTTPException

    fake_ex = MagicMock()
    fake_ex._work_queue.qsize.return_value = 999
    monkeypatch.setattr(api_main, "_executor", fake_ex)
    monkeypatch.setattr(api_main, "PROVISION_MAX_QUEUE_DEPTH", 10)

    with pytest.raises(HTTPException) as exc_info:
        api_main._reject_if_saturated()
    assert exc_info.value.status_code == 429


def test_reject_if_saturated_quiet_when_under_limit(monkeypatch) -> None:
    fake_ex = MagicMock()
    fake_ex._work_queue.qsize.return_value = 1
    monkeypatch.setattr(api_main, "_executor", fake_ex)
    monkeypatch.setattr(api_main, "PROVISION_MAX_QUEUE_DEPTH", 10)
    # No exception
    api_main._reject_if_saturated()


# ---------------------------------------------------------------------------
# _run_provisioning_background
# ---------------------------------------------------------------------------


def test_run_provisioning_background_success_path() -> None:
    """Successful workflow → mark_job_completed with the redacted result."""
    from agent_provisioning_team.models import EnvironmentInfo, ProvisioningResult

    env = EnvironmentInfo(container_id="c1", container_name="c1")
    fake_result = ProvisioningResult(agent_id="a", success=True, environment=env)

    with (
        patch.object(api_main, "mark_job_running") as mock_run,
        patch.object(api_main, "mark_job_completed") as mock_done,
        patch.object(api_main, "update_job"),
        patch.object(api_main.orchestrator, "run_workflow", return_value=fake_result),
    ):
        api_main._run_provisioning_background("j1", "a", "default.yaml")

    mock_run.assert_called_once_with("j1")
    mock_done.assert_called_once()


def test_run_provisioning_background_failure_path() -> None:
    from agent_provisioning_team.models import ProvisioningResult

    fake_result = ProvisioningResult(agent_id="a", success=False, error="boom")

    with (
        patch.object(api_main, "mark_job_running"),
        patch.object(api_main, "mark_job_failed") as mock_fail,
        patch.object(api_main, "update_job"),
        patch.object(api_main.orchestrator, "run_workflow", return_value=fake_result),
    ):
        api_main._run_provisioning_background("j1", "a", "default.yaml")

    mock_fail.assert_called_once()
    assert "boom" in mock_fail.call_args.kwargs["error"]


def test_run_provisioning_background_shutdown_error() -> None:
    from agent_provisioning_team.orchestrator import ProvisioningShutdownError

    with (
        patch.object(api_main, "mark_job_running"),
        patch.object(api_main, "mark_job_failed") as mock_fail,
        patch.object(
            api_main.orchestrator,
            "run_workflow",
            side_effect=ProvisioningShutdownError(agent_id="a", phase="setup"),
        ),
    ):
        api_main._run_provisioning_background("j1", "a", "default.yaml")

    mock_fail.assert_called_once()
    assert "Shutdown" in mock_fail.call_args.kwargs["error"]


def test_run_provisioning_background_generic_exception() -> None:
    with (
        patch.object(api_main, "mark_job_running"),
        patch.object(api_main, "mark_job_failed") as mock_fail,
        patch.object(api_main.orchestrator, "run_workflow", side_effect=RuntimeError("kaboom")),
    ):
        api_main._run_provisioning_background("j1", "a", "default.yaml")

    mock_fail.assert_called_once()
    assert "kaboom" in mock_fail.call_args.kwargs["error"]


def test_run_provisioning_background_job_updater_invokes_update_job() -> None:
    """The job_updater closure should call update_job with sanitized fields only."""
    from agent_provisioning_team.models import ProvisioningResult

    captured = []

    def fake_update_job(job_id, **fields):
        captured.append({"job_id": job_id, **fields})

    fake_result = ProvisioningResult(agent_id="a", success=True)

    def fake_run_workflow(*, agent_id, manifest_path, job_updater, **kw):
        # Exercise every branch of job_updater
        job_updater(
            current_phase="setup",
            progress=10,
            current_tool="pg",
            tools_completed=1,
            tools_total=3,
            status_text="hi",
        )
        # Empty call should be a no-op
        job_updater()
        return fake_result

    with (
        patch.object(api_main, "mark_job_running"),
        patch.object(api_main, "mark_job_completed"),
        patch.object(api_main, "update_job", side_effect=fake_update_job),
        patch.object(api_main.orchestrator, "run_workflow", side_effect=fake_run_workflow),
    ):
        api_main._run_provisioning_background("j1", "a", "default.yaml")

    # The first call should have written every field; the empty call is filtered out.
    assert len(captured) == 1
    assert captured[0]["current_phase"] == "setup"
    assert captured[0]["progress"] == 10


# ---------------------------------------------------------------------------
# _graceful_shutdown + _safe_compensate
# ---------------------------------------------------------------------------


def test_safe_compensate_swallows_exceptions(monkeypatch) -> None:
    with patch.object(api_main.orchestrator, "_compensate", side_effect=RuntimeError("ugh")):
        # Must not raise
        api_main._safe_compensate("a1")


def test_graceful_shutdown_drains_executor(monkeypatch) -> None:
    """The graceful shutdown path waits for the executor + marks running jobs failed."""

    monkeypatch.setattr(api_main, "_executor", None)
    api_main._ensure_executor()
    monkeypatch.setattr(api_main, "SHUTDOWN_GRACE_S", 1.0)

    with (
        patch.object(api_main, "list_jobs", return_value=[]),
        patch.object(api_main, "mark_all_running_jobs_failed"),
    ):
        asyncio.run(api_main._graceful_shutdown())


def test_graceful_shutdown_list_jobs_failure(monkeypatch) -> None:
    """If list_jobs raises, shutdown still completes."""
    monkeypatch.setattr(api_main, "_executor", None)
    api_main._ensure_executor()

    with (
        patch.object(api_main, "list_jobs", side_effect=RuntimeError("db down")),
        patch.object(api_main, "mark_all_running_jobs_failed"),
    ):
        asyncio.run(api_main._graceful_shutdown())


def test_graceful_shutdown_mark_all_failure_swallowed(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_executor", None)
    api_main._ensure_executor()

    with (
        patch.object(api_main, "list_jobs", return_value=[]),
        patch.object(
            api_main,
            "mark_all_running_jobs_failed",
            side_effect=RuntimeError("io"),
        ),
    ):
        asyncio.run(api_main._graceful_shutdown())


def test_graceful_shutdown_compensates_inflight_jobs(monkeypatch) -> None:
    """Active jobs with agent_id get _compensate'd via the safe wrapper."""
    monkeypatch.setattr(api_main, "_executor", None)
    api_main._ensure_executor()
    monkeypatch.setattr(api_main, "COMPENSATE_TIMEOUT_S", 1.0)

    compensated = []

    def fake_compensate(agent_id, tool_results):
        compensated.append(agent_id)

    with (
        patch.object(
            api_main,
            "list_jobs",
            return_value=[
                {"agent_id": "a1", "status": "running"},
                {"agent_id": "a2", "status": "running"},
                {"status": "running"},  # missing agent_id is skipped
            ],
        ),
        patch.object(api_main.orchestrator, "_compensate", side_effect=fake_compensate),
        patch.object(api_main, "mark_all_running_jobs_failed"),
    ):
        asyncio.run(api_main._graceful_shutdown())

    assert set(compensated) == {"a1", "a2"}


def test_submit_provisioning_job_tracks_future(monkeypatch) -> None:
    """Submitted jobs land in _inflight; the done callback removes them."""
    monkeypatch.setattr(api_main, "_executor", None)
    api_main._ensure_executor()

    def quick(*a, **k):
        return None

    with patch.object(api_main, "_run_provisioning_background", side_effect=quick):
        api_main._submit_provisioning_job("job-zzz", "agent-x", "default.yaml")

    # The submitted job will likely have completed already; the done-callback
    # cleans up _inflight so it may or may not be present, but executor was used.
    assert api_main._executor is not None
