"""Unit-level tests for the agent provisioning FastAPI surface.

These tests don't need a live executor / orchestrator — every external
function (job store, orchestrator, executor) is patched at the seam.
Marked NON-integration so they run on the default unit lane.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_team_studio.agent_provisioning_team.api import main as api_main
from agent_team_studio.agent_provisioning_team.api.main import app
from agent_team_studio.agent_provisioning_team.models import (
    DeprovisionRequest,
    DeprovisionResponse,
    EnvironmentInfo,
    ProvisioningResult,
    ProvisionRequest,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_open_pre_patch_executions():
    """Default the rollout drain gate to "nothing open" for every test.

    ``find_open_pre_patch_executions`` needs a live Temporal client/loop;
    without this, every provision/deprovision test in this module would block
    for ``_DRAIN_GATE_CLIENT_READY_TIMEOUT_S`` before failing open. Tests that
    exercise the gate itself override this within their own ``with`` block.
    """
    with patch.object(api_main, "find_open_pre_patch_executions", return_value=[]):
        yield


def test_require_provision_starter_is_agent_provisioning_workflow_entry() -> None:
    """API provision/resume/restart must start AgentProvisioningWorkflow via the Temporal starter."""
    from agent_team_studio.agent_provisioning_team.temporal.start_workflow import (
        start_provisioning_workflow,
    )

    with patch("shared.temporal.client.is_temporal_enabled", return_value=True):
        starter = api_main._require_provision_starter()
    assert starter is start_provisioning_workflow


def test_require_provision_starter_returns_503_when_temporal_check_raises() -> None:
    with patch(
        "shared.temporal.client.is_temporal_enabled",
        side_effect=RuntimeError("misconfigured"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            api_main._require_provision_starter()
    assert exc_info.value.status_code == 503


def test_require_provision_starter_returns_503_when_temporal_disabled() -> None:
    with patch(
        "shared.temporal.client.is_temporal_enabled",
        return_value=False,
    ):
        with pytest.raises(HTTPException) as exc_info:
            api_main._require_provision_starter()
    assert exc_info.value.status_code == 503
    assert "Temporal" in str(exc_info.value.detail)


def test_require_deprovision_runner_returns_503_when_temporal_check_raises() -> None:
    with patch(
        "shared.temporal.client.is_temporal_enabled",
        side_effect=RuntimeError("misconfigured"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            api_main._require_deprovision_runner()
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# agent_id path-traversal guard (HTTP edge)
# ---------------------------------------------------------------------------


_TRAVERSAL_IDS = ["../../etc/passwd", "a/b", "..\\..\\x", "/etc/passwd", "..", "."]


# End-to-end via the real FastAPI stack: the request-model validator rejects a
# traversal agent_id in the POST body before any handler/Temporal code runs.
@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_provision_endpoint_returns_422_for_traversal_agent_id(bad_id: str) -> None:
    r = client.post("/provision", json={"agent_id": bad_id})
    assert r.status_code == 422


def test_get_environment_endpoint_returns_422_for_encoded_traversal() -> None:
    # ``%2e%2e`` reaches the handler as ``..`` (Starlette does not collapse it),
    # so the path-param guard surfaces a real 422 through the HTTP stack.
    r = client.get("/environments/%2e%2e")
    assert r.status_code == 422


def test_provision_endpoint_accepts_dotted_agent_id() -> None:
    """A legitimate dotted id passes validation and reaches the handler (503 without Temporal)."""
    r = client.post("/provision", json={"agent_id": "blog.writer"})
    assert r.status_code != 422


# Path params bypass the request-model validator, so the {agent_id} routes call
# ``_require_safe_agent_id`` themselves. These handlers are sync ``def``s, so
# calling them directly exercises that guard exactly as FastAPI's threadpool
# would (and reliably covers traversal shapes that URL-encoding would mangle).
@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_get_agent_status_rejects_traversal_agent_id(bad_id: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        api_main.get_agent_status(bad_id)
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_deprovision_agent_rejects_traversal_agent_id(bad_id: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        api_main.deprovision_agent(bad_id)
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_provision_request_rejects_traversal_agent_id(bad_id: str) -> None:
    """The request-model validator turns a traversal id into a 422 (ValidationError)."""
    with pytest.raises(ValidationError):
        ProvisionRequest(agent_id=bad_id)
    with pytest.raises(ValidationError):
        DeprovisionRequest(agent_id=bad_id)


@pytest.mark.parametrize("good_id", ["blog.writer", "agent-001", "a..b"])
def test_provision_request_allows_valid_agent_id(good_id: str) -> None:
    # Dotted ids — including a harmless embedded double-dot — are accepted.
    assert ProvisionRequest(agent_id=good_id).agent_id == good_id
    assert DeprovisionRequest(agent_id=good_id).agent_id == good_id


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
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        r = client.post("/provision", json={"agent_id": "ag-temporal"})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    mock_create_job.assert_called_once()
    assert mock_create_job.call_args.kwargs["agent_id"] == "ag-temporal"
    fake_starter.assert_called_once()


def test_provision_returns_503_when_temporal_disabled() -> None:
    """Provisioning is Temporal-only: no starter means HTTP 503, not a
    thread-mode fallback."""
    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(
            api_main,
            "_require_provision_starter",
            side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED_MESSAGE),
        ),
    ):
        r = client.post("/provision", json={"agent_id": "ag-no-temporal"})

    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]
    mock_create_job.assert_not_called()


# ---------------------------------------------------------------------------
# Rollout drain gate (find_open_pre_patch_executions)
# ---------------------------------------------------------------------------


def _fake_pre_patch_execution(agent_id: str = "ag-draining"):
    from agent_team_studio.agent_provisioning_team.shared.visibility_query import PrePatchExecution

    return PrePatchExecution(
        workflow_id="agent-provisioning-old-job",
        run_id="run-1",
        workflow_type="AgentProvisioningWorkflow",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        agent_id=agent_id,
    )


def test_provision_refused_when_pre_patch_execution_open() -> None:
    """A new /provision request must not race an open pre-patch execution."""
    fake_starter = MagicMock()

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
        patch.object(
            api_main,
            "find_open_pre_patch_executions",
            return_value=[_fake_pre_patch_execution("ag-draining")],
        ),
    ):
        r = client.post("/provision", json={"agent_id": "ag-draining"})

    assert r.status_code == 409
    assert r.headers["retry-after"]
    body = r.json()
    assert body["agent_id"] == "ag-draining"
    assert body["open_pre_patch_executions"] == 1
    mock_create_job.assert_not_called()
    fake_starter.assert_not_called()


def test_provision_proceeds_when_no_pre_patch_execution_open() -> None:
    """No open pre-patch execution: the request proceeds as normal."""
    fake_starter = MagicMock()

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
        patch.object(api_main, "find_open_pre_patch_executions", return_value=[]),
    ):
        r = client.post("/provision", json={"agent_id": "ag-clear"})

    assert r.status_code == 200
    mock_create_job.assert_called_once()
    fake_starter.assert_called_once()


def test_provision_proceeds_when_drain_gate_disabled_via_env(monkeypatch) -> None:
    """Disabling the gate lets a request proceed even with an open pre-patch execution."""
    monkeypatch.setenv(api_main.DRAIN_GATE_ENABLED_ENV_VAR, "false")
    fake_starter = MagicMock()

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
        patch.object(
            api_main,
            "find_open_pre_patch_executions",
            return_value=[_fake_pre_patch_execution()],
        ) as mock_find,
    ):
        r = client.post("/provision", json={"agent_id": "ag-gate-off"})

    assert r.status_code == 200
    mock_create_job.assert_called_once()
    fake_starter.assert_called_once()
    mock_find.assert_not_called()


def test_provision_proceeds_when_drain_gate_check_fails() -> None:
    """A visibility-query failure fails open rather than blocking all traffic."""
    fake_starter = MagicMock()

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
        patch.object(
            api_main,
            "find_open_pre_patch_executions",
            side_effect=RuntimeError("Temporal client not available"),
        ),
    ):
        r = client.post("/provision", json={"agent_id": "ag-query-fails"})

    assert r.status_code == 200
    mock_create_job.assert_called_once()
    fake_starter.assert_called_once()


def test_deprovision_refused_when_pre_patch_execution_open() -> None:
    """A new deprovision request must not race an open pre-patch execution."""
    fake_runner = MagicMock()

    with (
        patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner),
        patch.object(
            api_main,
            "find_open_pre_patch_executions",
            return_value=[_fake_pre_patch_execution("a1")],
        ),
    ):
        r = client.delete("/environments/a1")

    assert r.status_code == 409
    assert r.headers["retry-after"]
    assert r.json()["agent_id"] == "a1"
    fake_runner.assert_not_called()


def test_drain_gate_enabled_defaults_true_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(api_main.DRAIN_GATE_ENABLED_ENV_VAR, raising=False)
    assert api_main._drain_gate_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", "OFF"])
def test_drain_gate_enabled_false_for_recognized_disable_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(api_main.DRAIN_GATE_ENABLED_ENV_VAR, raw)
    assert api_main._drain_gate_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "garbage"])
def test_drain_gate_enabled_true_for_everything_else(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(api_main.DRAIN_GATE_ENABLED_ENV_VAR, raw)
    assert api_main._drain_gate_enabled() is True


def test_provision_held_while_pre_patch_open_then_proceeds_once_drained() -> None:
    """A request for the same agent_id is refused while a pre-patch execution is
    open and proceeds once the visibility query reports none remain."""
    fake_starter = MagicMock()
    agent_id = "ag-transition"

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
        patch.object(
            api_main,
            "find_open_pre_patch_executions",
            return_value=[_fake_pre_patch_execution(agent_id)],
        ),
    ):
        held = client.post("/provision", json={"agent_id": agent_id})

    assert held.status_code == 409
    mock_create_job.assert_not_called()
    fake_starter.assert_not_called()

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
        patch.object(api_main, "find_open_pre_patch_executions", return_value=[]),
    ):
        proceeded = client.post("/provision", json={"agent_id": agent_id})

    assert proceeded.status_code == 200
    mock_create_job.assert_called_once()
    fake_starter.assert_called_once()


def test_deprovision_held_while_pre_patch_open_then_proceeds_once_drained() -> None:
    """Same held-then-proceeds transition, exercised against the deprovision path."""
    fake_runner = MagicMock()
    agent_id = "ag-deprovision-transition"

    with (
        patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner),
        patch.object(
            api_main,
            "find_open_pre_patch_executions",
            return_value=[_fake_pre_patch_execution(agent_id)],
        ),
    ):
        held = client.delete(f"/environments/{agent_id}")

    assert held.status_code == 409
    fake_runner.assert_not_called()

    with (
        patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner),
        patch.object(api_main, "find_open_pre_patch_executions", return_value=[]),
    ):
        proceeded = client.delete(f"/environments/{agent_id}")

    assert proceeded.status_code == 200
    fake_runner.assert_called_once()


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
    with (
        patch.object(api_main, "_require_provision_starter", return_value=MagicMock()),
        patch.object(api_main, "get_job", return_value=None),
    ):
        r = client.post("/provision/job/missing/resume")
    assert r.status_code == 404


def test_resume_job_in_running_state_rejected_when_workflow_open() -> None:
    """Active Temporal jobs cannot be resumed (would kill the live run without compensate)."""
    with (
        patch.object(api_main, "_require_provision_starter", return_value=MagicMock()),
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "running",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
            },
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=True,
        ),
    ):
        r = client.post("/provision/job/j1/resume")
    assert r.status_code == 400
    assert "cannot be resumed" in r.json()["detail"]


def test_resume_pending_job_when_no_open_workflow() -> None:
    """Indeterminate start timeouts leave pending jobs recoverable if Temporal never opened."""
    starter = MagicMock()
    with (
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "pending",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
                "completed_phases": [],
                "phase_results": {},
            },
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
        patch.object(api_main, "_require_provision_starter", return_value=starter),
        patch.object(api_main, "update_job") as mock_update,
    ):
        r = client.post("/provision/job/j1/resume")
    assert r.status_code == 200
    starter.assert_called_once()
    assert starter.call_args.kwargs.get("replace_existing") is True
    mock_update.assert_called_once_with("j1", status="running", error=None)


def test_provision_start_timeout_does_not_mark_job_failed() -> None:
    """Indeterminate start timeouts leave the job pollable and return job_id."""
    import concurrent.futures

    with (
        patch("agent_team_studio.agent_provisioning_team.api.main.create_job") as mock_create_job,
        patch.object(
            api_main,
            "_require_provision_starter",
            return_value=MagicMock(side_effect=concurrent.futures.TimeoutError()),
        ),
        patch.object(api_main, "mark_job_failed") as mock_fail,
    ):
        r = client.post("/provision", json={"agent_id": "ag-timeout"})

    assert r.status_code == 200
    body = r.json()
    assert body["job_id"]
    assert body["status"] == "pending"
    assert "timed out" in body["message"].lower()
    assert body["job_id"] in body["message"]
    mock_create_job.assert_called_once()
    assert mock_create_job.call_args.kwargs["agent_id"] == "ag-timeout"
    mock_fail.assert_not_called()


def test_resume_job_missing_agent_or_manifest() -> None:
    with (
        patch.object(api_main, "_require_provision_starter", return_value=MagicMock()),
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "failed",
                "agent_id": "",  # missing
                "manifest_path": "default.yaml",
            },
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
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
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
        patch.object(api_main, "update_job") as mock_update,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        r = client.post("/provision/job/j1/resume")

    assert r.status_code == 200
    fake_starter.assert_called_once()
    mock_update.assert_called_once_with("j1", status="running", error=None)


def test_resume_job_rejects_when_workflow_still_open() -> None:
    with (
        patch.object(api_main, "_require_provision_starter", return_value=MagicMock()),
        patch.object(
            api_main,
            "get_job",
            return_value={
                "status": "failed",
                "agent_id": "a1",
                "manifest_path": "default.yaml",
            },
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=True,
        ),
    ):
        r = client.post("/provision/job/j1/resume")
    assert r.status_code == 400
    assert "active Temporal workflow" in r.json()["detail"]


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
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
        patch.object(
            api_main,
            "_require_provision_starter",
            side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED_MESSAGE),
        ),
    ):
        r = client.post("/provision/job/j1/resume")

    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /provision/job/{job_id}/restart
# ---------------------------------------------------------------------------


def test_restart_job_not_found() -> None:
    with (
        patch.object(api_main, "_require_provision_starter", return_value=MagicMock()),
        patch.object(api_main, "get_job", return_value=None),
    ):
        r = client.post("/provision/job/missing/restart")
    assert r.status_code == 404


def test_restart_job_missing_agent_or_manifest() -> None:
    with (
        patch.object(api_main, "_require_provision_starter", return_value=MagicMock()),
        patch.object(
            api_main,
            "get_job",
            return_value={"status": "completed", "agent_id": "a1", "manifest_path": ""},
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
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
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
        patch.object(api_main, "store_reset_job") as mock_reset,
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        r = client.post("/provision/job/j1/restart")
    assert r.status_code == 200
    mock_reset.assert_called_once_with("j1")
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
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.start_workflow.provisioning_workflow_is_open",
            return_value=False,
        ),
        patch.object(
            api_main,
            "_require_provision_starter",
            side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED_MESSAGE),
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
    fake_runner.assert_called_once_with("a1", False)


def test_deprovision_with_force_flag() -> None:
    captured = {}

    def fake_runner(agent_id, force=False):
        captured["agent_id"] = agent_id
        captured["force"] = force
        return {"agent_id": agent_id, "success": True, "details": {}, "error": None}

    with patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner):
        r = client.delete("/environments/a1?force=true")
    assert r.status_code == 200
    assert captured["agent_id"] == "a1"
    assert captured["force"] is True


def test_deprovision_returns_503_when_temporal_disabled() -> None:
    with patch.object(
        api_main,
        "_require_deprovision_runner",
        side_effect=HTTPException(status_code=503, detail=api_main._TEMPORAL_REQUIRED_MESSAGE),
    ):
        r = client.delete("/environments/a1")
    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /environments/{agent_id} GET
# ---------------------------------------------------------------------------


def test_deprovision_timeout_returns_success_false() -> None:
    """Client-side wait timeout must not surface as an unhandled 500."""
    import concurrent.futures

    with patch.object(
        api_main,
        "_require_deprovision_runner",
        return_value=MagicMock(side_effect=concurrent.futures.TimeoutError()),
    ):
        r = client.delete("/environments/a1")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "timed out" in body["error"].lower()


def test_get_environment_not_found() -> None:
    with patch.object(api_main, "_get_agent_status", return_value=None) as mock_status:
        r = client.get("/environments/missing")
    assert r.status_code == 404
    mock_status.assert_called_once_with("missing")


def test_get_environment_returns_status() -> None:
    with patch.object(
        api_main,
        "_get_agent_status",
        return_value={
            "agent_id": "a1",
            "status": "ready",
            "container_id": "c1",
            "container_name": "agent-a1",
            "tools_provisioned": ["postgresql"],
        },
    ) as mock_status:
        r = client.get("/environments/a1")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    mock_status.assert_called_once_with("a1")


# ---------------------------------------------------------------------------
# /environments GET (list)
# ---------------------------------------------------------------------------


def test_list_environments_empty() -> None:
    with patch.object(api_main, "_list_agents", return_value=[]) as mock_list:
        r = client.get("/environments")
    assert r.status_code == 200
    assert r.json()["agents"] == []
    mock_list.assert_called_once_with(status=None)


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

    with patch.object(api_main, "_list_agents", side_effect=fake_list):
        r = client.get("/environments?status=ready")
    assert r.status_code == 200
    assert captured["status"] == "ready"
    assert r.json()["agents"][0]["agent_id"] == "a1"


# ---------------------------------------------------------------------------
# lifespan — Temporal owns in-flight work
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_does_not_compensate_or_fail_jobs() -> None:
    """API process exit must not tear down Temporal-owned provision jobs."""
    mark_failed_calls: list[str] = []

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.job_store.mark_all_running_jobs_failed",
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

    assert mark_failed_calls == []
    assert not hasattr(api_main, "orchestrator")
    assert not hasattr(api_main, "_graceful_shutdown")
    assert not hasattr(api_main, "_safe_compensate")
    assert not hasattr(api_main, "COMPENSATE_TIMEOUT_S")


# -------------------------------------------------------------------------
# FastAPI lifespan enter/exit smoke.
# -------------------------------------------------------------------------


def test_lifespan_runs_cleanly(monkeypatch) -> None:
    """Entering + exiting the TestClient context runs the lifespan hook end-to-end."""
    monkeypatch.setattr(api_main, "_start_temporal_worker_backstop", lambda: None)
    with TestClient(api_main.app) as c:
        r = c.get("/health")
        assert r.status_code == 200


def test_startup_backstop_starts_temporal_worker(monkeypatch) -> None:
    """Standalone uvicorn runs need a lifespan backstop so TEMPORAL_ADDRESS-set
    processes still start the worker after import stopped auto-booting."""
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    started: list[bool] = []
    monkeypatch.setattr(
        worker_mod,
        "start_agent_provisioning_temporal_worker_thread",
        lambda: started.append(True),
    )

    api_main._start_temporal_worker_backstop()
    assert started == [True]


def test_startup_backstop_swallows_worker_failure(monkeypatch) -> None:
    """A worker-start failure in the backstop must not abort app boot."""
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    def _boom() -> bool:
        raise RuntimeError("temporal down")

    monkeypatch.setattr(worker_mod, "start_agent_provisioning_temporal_worker_thread", _boom)
    api_main._start_temporal_worker_backstop()


def test_lifespan_invokes_temporal_worker_backstop(monkeypatch) -> None:
    """Entering the TestClient context must call the Temporal worker backstop."""
    calls: list[bool] = []
    monkeypatch.setattr(api_main, "_start_temporal_worker_backstop", lambda: calls.append(True))
    with TestClient(api_main.app):
        pass
    assert calls == [True]
