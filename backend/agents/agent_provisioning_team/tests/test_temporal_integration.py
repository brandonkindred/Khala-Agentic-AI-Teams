"""Temporal tests for the Agent Provisioning team.

Covers routing (including the Temporal-required 503 path), skip_phases/
prior_results plumbing on /resume, progress writes from v2 activities, and
Pattern A exports. Mocks Temporal at the HTTP boundary rather than spinning
up WorkflowEnvironment — matches the SE team's test_temporal_integration.py
style and keeps the suite fast.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_provisioning_team.api.main import app

client = TestClient(app)


@patch("agent_provisioning_team.api.main.create_job")
@patch("agent_provisioning_team.temporal.start_workflow.start_provisioning_workflow")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True)
def test_provision_routes_to_temporal_when_enabled(
    mock_enabled: MagicMock,
    mock_start: MagicMock,
    mock_create_job: MagicMock,
) -> None:
    resp = client.post("/provision", json={"agent_id": "t-temporal-1"})

    assert resp.status_code == 200
    mock_create_job.assert_called_once()
    create_kwargs = mock_create_job.call_args.kwargs or {}
    create_args = mock_create_job.call_args.args
    # create_job(job_id=..., agent_id=..., manifest_path=...)
    if create_kwargs:
        assert create_kwargs.get("agent_id") == "t-temporal-1"
        assert create_kwargs.get("manifest_path") == "default.yaml"
    else:
        assert create_args[1] == "t-temporal-1"
        assert create_args[2] == "default.yaml"
    mock_start.assert_called_once()
    args, kwargs = mock_start.call_args
    # Positional: (job_id, agent_id, manifest_path)
    assert args[1] == "t-temporal-1"
    assert args[2] == "default.yaml"
    assert kwargs.get("skip_phases") is None
    assert kwargs.get("prior_results") is None


@patch("agent_provisioning_team.api.main.create_job")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False)
def test_provision_returns_503_when_temporal_disabled(
    mock_enabled: MagicMock,
    mock_create_job: MagicMock,
) -> None:
    resp = client.post("/provision", json={"agent_id": "t-disabled"})

    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]
    mock_create_job.assert_not_called()


@patch("agent_provisioning_team.api.main.update_job")
@patch("agent_provisioning_team.api.main.get_job")
@patch("agent_provisioning_team.temporal.start_workflow.start_provisioning_workflow")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True)
def test_resume_passes_skip_phases_and_prior_results(
    mock_enabled: MagicMock,
    mock_start: MagicMock,
    mock_get_job: MagicMock,
    mock_update_job: MagicMock,
) -> None:
    mock_get_job.return_value = {
        "job_id": "job-resume-1",
        "agent_id": "a1",
        "manifest_path": "default.yaml",
        "status": "failed",
        "completed_phases": ["setup", "credential_generation"],
        "phase_results": {
            "setup": {"success": True, "environment": None},
            "credential_generation": {"success": True, "credentials": {}},
        },
    }

    resp = client.post("/provision/job/job-resume-1/resume")

    assert resp.status_code == 200
    mock_start.assert_called_once()
    _, kwargs = mock_start.call_args
    assert kwargs.get("skip_phases") == ["setup", "credential_generation"]
    assert kwargs.get("prior_results") == {
        "setup": {"success": True, "environment": None},
        "credential_generation": {"success": True, "credentials": {}},
    }


@patch("agent_provisioning_team.api.main.get_job")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False)
def test_resume_returns_503_when_temporal_disabled(
    mock_enabled: MagicMock,
    mock_get_job: MagicMock,
) -> None:
    mock_get_job.return_value = {
        "job_id": "job-resume-2",
        "agent_id": "a1",
        "manifest_path": "default.yaml",
        "status": "failed",
        "completed_phases": [],
        "phase_results": {},
    }

    resp = client.post("/provision/job/job-resume-2/resume")

    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]


@patch("agent_provisioning_team.api.main.get_job")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False)
def test_restart_returns_503_when_temporal_disabled(
    mock_enabled: MagicMock,
    mock_get_job: MagicMock,
) -> None:
    mock_get_job.return_value = {
        "job_id": "job-restart-1",
        "agent_id": "a1",
        "manifest_path": "default.yaml",
        "status": "completed",
    }

    resp = client.post("/provision/job/job-restart-1/restart")

    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]


def test_setup_activity_writes_progress_via_update_job() -> None:
    """Invoking setup_activity directly should push phase + progress into job_store."""
    from agent_provisioning_team.temporal import activities as t_acts

    recorded_updates: list[dict] = []
    recorded_running: list[str] = []
    recorded_completed: list[tuple] = []

    def fake_update(job_id, **fields):
        recorded_updates.append({"job_id": job_id, **fields})

    def fake_mark_running(job_id):
        recorded_running.append(job_id)

    def fake_add_completed(job_id, phase, result):
        recorded_completed.append((job_id, phase, result))

    class _FakeEnv:
        def model_dump(self):
            return {"container_id": "abc", "workspace_path": "/workspace"}

    class _FakeSetupResult:
        success = True
        environment = _FakeEnv()
        error = None

    class _FakeManifest:
        tools = []

    class _FakeOrch:
        environment_store = MagicMock()
        tool_agents = {"docker_provisioner": MagicMock()}
        credential_store = MagicMock()

    with (
        patch(
            "agent_provisioning_team.shared.job_store.update_job",
            side_effect=fake_update,
        ),
        patch(
            "agent_provisioning_team.shared.job_store.mark_job_running",
            side_effect=fake_mark_running,
        ),
        patch(
            "agent_provisioning_team.shared.job_store.add_completed_phase",
            side_effect=fake_add_completed,
        ),
        patch(
            "agent_provisioning_team.phases.setup.run_setup",
            return_value=_FakeSetupResult(),
        ),
        patch.object(
            t_acts,
            "_load_ctx",
            return_value=(_FakeOrch(), _FakeManifest()),
        ),
        # activity.heartbeat raises outside a live Temporal context; stub it.
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity("job-progress-1", "agent-1", "default.yaml")

    assert payload["success"] is True
    assert recorded_running == ["job-progress-1"]
    assert any(
        u.get("current_phase") == "setup" and u.get("progress") is not None
        for u in recorded_updates
    ), f"expected a setup/progress update, got {recorded_updates}"
    assert recorded_completed and recorded_completed[0][1] == "setup"


def test_setup_activity_restores_prior_snapshot_without_running_setup() -> None:
    """When prior_setup is passed, skip the real run_setup and return the restored payload."""
    from agent_provisioning_team.temporal import activities as t_acts

    recorded_updates: list[dict] = []

    with (
        patch(
            "agent_provisioning_team.shared.job_store.update_job",
            side_effect=lambda job_id, **f: recorded_updates.append({"job_id": job_id, **f}),
        ),
        patch(
            "agent_provisioning_team.shared.job_store.mark_job_running",
        ),
        patch("agent_provisioning_team.phases.setup.run_setup") as real_setup,
        patch.object(t_acts, "_load_ctx") as load_ctx,
    ):
        prior = {"success": True, "environment": None}
        payload = t_acts.setup_activity("job-resume", "agent-x", "default.yaml", prior_setup=prior)

    assert payload == {"success": True, "environment": None}
    real_setup.assert_not_called()
    load_ctx.assert_not_called()
    assert any(u.get("status_text", "").startswith("Restored") for u in recorded_updates)


def test_pattern_a_exports_workflows_and_activities() -> None:
    import agent_provisioning_team.temporal as t
    from agent_provisioning_team.temporal.activities import (
        audit_activity,
        credentials_activity,
        deliver_activity,
        documentation_activity,
        list_manifest_tools_activity,
        provision_tool_activity,
        setup_activity,
    )
    from agent_provisioning_team.temporal.workflows import (
        AgentDeprovisioningWorkflow,
        AgentProvisioningWorkflow,
    )

    provisioning = [w for w in t.WORKFLOWS if w.__name__ == "AgentProvisioningWorkflow"]
    deprovisioning = [w for w in t.WORKFLOWS if "Deprovisioning" in w.__name__]
    assert len(t.WORKFLOWS) == 2
    assert len(provisioning) == 1
    assert provisioning[0] is AgentProvisioningWorkflow
    assert len(deprovisioning) == 1
    assert deprovisioning[0] is AgentDeprovisioningWorkflow
    for fn in (
        setup_activity,
        list_manifest_tools_activity,
        credentials_activity,
        provision_tool_activity,
        audit_activity,
        documentation_activity,
        deliver_activity,
    ):
        assert fn in t.ACTIVITIES, f"{fn.__name__} missing from ACTIVITIES"
