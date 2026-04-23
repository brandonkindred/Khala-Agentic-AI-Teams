"""Temporal integration tests for the Agent Provisioning team.

Covers routing, skip_phases/prior_results plumbing on /resume, the
PROVISION_THREAD_FALLBACK escape hatch, progress writes from v2
activities, and Pattern A exports. Mocks Temporal at the HTTP boundary
rather than spinning up WorkflowEnvironment — matches the SE team's
test_temporal_integration.py style and keeps the suite fast.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_provisioning_team.api import main as api_main
from agent_provisioning_team.api.main import app

client = TestClient(app)


@patch("agent_provisioning_team.api.main.create_job")
@patch("agent_provisioning_team.temporal.start_workflow.start_provisioning_workflow")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True)
def test_provision_routes_to_v2_when_temporal_enabled(
    mock_enabled: MagicMock,
    mock_start: MagicMock,
    mock_create_job: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.delenv("PROVISION_THREAD_FALLBACK", raising=False)
    resp = client.post("/provision", json={"agent_id": "t-temporal-1"})

    assert resp.status_code == 200
    mock_start.assert_called_once()
    args, kwargs = mock_start.call_args
    # Positional: (job_id, agent_id, manifest_path, access_tier_str)
    assert args[1] == "t-temporal-1"
    assert args[3] == "standard"  # AccessTier.STANDARD.value, not the enum
    assert kwargs.get("skip_phases") is None
    assert kwargs.get("prior_results") is None


@patch("agent_provisioning_team.api.main.update_job")
@patch("agent_provisioning_team.api.main.get_job")
@patch("agent_provisioning_team.temporal.start_workflow.start_provisioning_workflow")
@patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True)
def test_resume_passes_skip_phases_and_prior_results(
    mock_enabled: MagicMock,
    mock_start: MagicMock,
    mock_get_job: MagicMock,
    mock_update_job: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.delenv("PROVISION_THREAD_FALLBACK", raising=False)
    mock_get_job.return_value = {
        "job_id": "job-resume-1",
        "agent_id": "a1",
        "manifest_path": "default.yaml",
        "access_tier": "standard",
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


def test_provision_falls_back_to_thread_path_when_flag_set(monkeypatch) -> None:
    monkeypatch.setenv("PROVISION_THREAD_FALLBACK", "1")

    with (
        patch(
            "agent_provisioning_team.temporal.client.is_temporal_enabled",
            return_value=True,
        ),
        patch(
            "agent_provisioning_team.temporal.start_workflow.start_provisioning_workflow"
        ) as mock_start,
        patch("agent_provisioning_team.api.main.create_job"),
        patch("agent_provisioning_team.api.main._ensure_executor") as mock_ensure,
    ):
        mock_executor = MagicMock()
        mock_ensure.return_value = mock_executor

        resp = client.post("/provision", json={"agent_id": "t-fallback"})

    assert resp.status_code == 200
    # Fallback forces the thread path, so the Temporal starter must NOT be called.
    mock_start.assert_not_called()
    # And the executor must have received the submission.
    assert mock_executor.submit.called
    submitted_fn = mock_executor.submit.call_args[0][0]
    assert submitted_fn is api_main._run_provisioning_background


def test_setup_activity_v2_writes_progress_via_update_job() -> None:
    """Invoking setup_activity_v2 directly should push phase + progress into job_store."""
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
            return_value=(_FakeOrch(), _FakeManifest(), "standard"),
        ),
        # activity.heartbeat raises outside a live Temporal context; stub it.
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity_v2("job-progress-1", "agent-1", "default.yaml", "standard")

    assert payload["success"] is True
    assert recorded_running == ["job-progress-1"]
    assert any(
        u.get("current_phase") == "setup" and u.get("progress") is not None
        for u in recorded_updates
    ), f"expected a setup/progress update, got {recorded_updates}"
    assert recorded_completed and recorded_completed[0][1] == "setup"


def test_setup_activity_v2_restores_prior_snapshot_without_running_setup() -> None:
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
        payload = t_acts.setup_activity_v2(
            "job-resume", "agent-x", "default.yaml", "standard", prior_setup=prior
        )

    assert payload == {"success": True, "environment": None}
    real_setup.assert_not_called()
    load_ctx.assert_not_called()
    assert any(u.get("status_text", "").startswith("Restored") for u in recorded_updates)


def test_pattern_a_exports_workflows_and_activities() -> None:
    import agent_provisioning_team.temporal as t
    from agent_provisioning_team.temporal.activities import (
        audit_activity_v2,
        credentials_activity_v2,
        deliver_activity_v2,
        documentation_activity_v2,
        provision_tool_activity,
        setup_activity_v2,
    )
    from agent_provisioning_team.temporal.workflows import (
        AgentProvisioningWorkflow,
        AgentProvisioningWorkflowV2,
    )

    assert AgentProvisioningWorkflow in t.WORKFLOWS
    assert AgentProvisioningWorkflowV2 in t.WORKFLOWS
    for fn in (
        setup_activity_v2,
        credentials_activity_v2,
        provision_tool_activity,
        audit_activity_v2,
        documentation_activity_v2,
        deliver_activity_v2,
    ):
        assert fn in t.ACTIVITIES, f"{fn.__name__} missing from ACTIVITIES"
