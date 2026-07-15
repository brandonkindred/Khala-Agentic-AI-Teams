"""Unit-level tests for the agent provisioning FastAPI surface.

These tests don't need a live executor / orchestrator — every external
function (job store, orchestrator, executor) is patched at the seam.
Marked NON-integration so they run on the default unit lane.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import HTTPException
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
    """When Temporal is available, /provision delegates to its starter."""
    fake_starter = MagicMock()

    with (
        patch("agent_provisioning_team.api.main.create_job"),
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        r = client.post("/provision", json={"agent_id": "ag-temporal"})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    fake_starter.assert_called_once()


def test_provision_returns_503_when_temporal_disabled() -> None:
    """Provisioning is Temporal-only: no starter means HTTP 503, not a
    thread-mode fallback."""
    with (
        patch("agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(
            api_main,
            "_require_provision_starter",
            side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED),
        ),
    ):
        r = client.post("/provision", json={"agent_id": "ag-no-temporal"})

    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]
    mock_create_job.assert_not_called()


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
    assert r.status_code == 400


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
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        r = client.post("/provision/job/j1/resume")

    assert r.status_code == 200
    fake_starter.assert_called_once()


def test_resume_job_returns_503_when_temporal_disabled() -> None:
    with (
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "failed",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
                "completed_phases": [],
                "phase_results": {},
            },
        ),
        patch.object(
            api_main,
            "_require_provision_starter",
            side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED),
        ),
    ):
        r = client.post("/provision/job/j1/resume")

    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]


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
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        r = client.post("/provision/job/j1/restart")
    assert r.status_code == 200
    fake_starter.assert_called_once()


def test_restart_job_returns_503_when_temporal_disabled() -> None:
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
        patch.object(
            api_main,
            "_require_provision_starter",
            side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED),
        ),
    ):
        r = client.post("/provision/job/j1/restart")
    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /environments/{agent_id} DELETE (deprovision)
# ---------------------------------------------------------------------------


def test_deprovision_returns_runner_result() -> None:
    fake_resp = DeprovisionResponse(agent_id="a1", success=True)
    fake_runner = MagicMock(return_value=fake_resp.model_dump())
    with patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner):
        r = client.delete("/environments/a1")
    assert r.status_code == 200
    assert r.json()["agent_id"] == "a1"
    assert r.json()["success"] is True


def test_deprovision_with_force_flag() -> None:
    captured = {}

    def fake_runner(agent_id, force=False):
        captured["force"] = force
        return {"agent_id": agent_id, "success": True, "details": {}, "error": None}

    with patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner):
        r = client.delete("/environments/a1?force=true")
    assert r.status_code == 200
    assert captured["force"] is True


def test_deprovision_returns_503_when_temporal_disabled() -> None:
    with patch.object(
        api_main,
        "_require_deprovision_runner",
        side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED),
    ):
        r = client.delete("/environments/a1")
    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]


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
# lifespan — Temporal owns in-flight work
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_does_not_compensate_or_fail_jobs() -> None:
    """API process exit must not tear down Temporal-owned provision jobs."""
    compensate_calls: list[str] = []
    mark_failed_calls: list[str] = []

    with (
        patch.object(
            api_main.orchestrator,
            "_compensate",
            side_effect=lambda agent_id, tool_results: compensate_calls.append(agent_id),
        ),
        patch(
            "agent_provisioning_team.shared.job_store.mark_all_running_jobs_failed",
            side_effect=lambda reason: mark_failed_calls.append(reason),
        ),
        patch.object(
            api_main,
            "list_jobs",
            return_value=[{"agent_id": "a1", "status": "running"}],
        ),
    ):
        with TestClient(api_main.app):
            pass

    assert compensate_calls == []
    assert mark_failed_calls == []
    assert not hasattr(api_main, "_graceful_shutdown")
    assert not hasattr(api_main, "_safe_compensate")
    assert not hasattr(api_main, "COMPENSATE_TIMEOUT_S")
