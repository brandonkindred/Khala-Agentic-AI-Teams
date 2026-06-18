"""Unit-level tests for Temporal activities, client, worker, and start_workflow.

These tests do not need a live Temporal server; we mock at the
``temporalio`` boundary (``activity.heartbeat``, ``Client.connect``) and
verify the contracts each surface exposes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_task_queue_default() -> None:
    from agent_provisioning_team.temporal import constants

    assert isinstance(constants.TASK_QUEUE, str)
    assert constants.TASK_QUEUE
    assert constants.WORKFLOW_ID_PREFIX == "agent-provisioning-"


def test_task_queue_env_override(monkeypatch) -> None:
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "custom-queue")
    # Force re-import to pick up env override.
    import importlib

    from agent_provisioning_team.temporal import constants as constants_mod

    reloaded = importlib.reload(constants_mod)
    assert reloaded.TASK_QUEUE == "custom-queue"

    # Restore original module by reloading without the env var
    monkeypatch.delenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", raising=False)
    importlib.reload(reloaded)


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------


def test_client_helpers_default_env(monkeypatch) -> None:
    from agent_provisioning_team.temporal import client

    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    assert client.get_temporal_address() is None
    assert client.get_temporal_namespace() == "default"
    assert client.is_temporal_enabled() is False


def test_client_helpers_with_env(monkeypatch) -> None:
    from agent_provisioning_team.temporal import client

    monkeypatch.setenv("TEMPORAL_ADDRESS", "  localhost:7233  ")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "  myns  ")
    assert client.get_temporal_address() == "localhost:7233"
    assert client.get_temporal_namespace() == "myns"
    assert client.is_temporal_enabled() is True


def test_client_get_and_set() -> None:
    from agent_provisioning_team.temporal import client

    sentinel = MagicMock(name="fake-client")
    client.set_temporal_client(sentinel)
    assert client.get_temporal_client() is sentinel
    client.set_temporal_client(None)
    assert client.get_temporal_client() is None


def test_loop_get_and_set() -> None:
    from agent_provisioning_team.temporal import client

    loop = asyncio.new_event_loop()
    try:
        client.set_temporal_loop(loop)
        assert client.get_temporal_loop() is loop
    finally:
        client.set_temporal_loop(None)
        loop.close()
    assert client.get_temporal_loop() is None


def test_connect_temporal_client_returns_none_when_address_blank(monkeypatch) -> None:
    from agent_provisioning_team.temporal import client

    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)

    async def _run():
        result = await client.connect_temporal_client()
        return result

    assert asyncio.run(_run()) is None


def test_connect_temporal_client_connects_when_address_set(monkeypatch) -> None:
    from agent_provisioning_team.temporal import client

    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "myns")

    fake_client = MagicMock(name="connected-client")

    with patch("temporalio.client.Client") as mock_client_cls:
        mock_client_cls.connect = AsyncMock(return_value=fake_client)

        async def _run():
            return await client.connect_temporal_client()

        result = asyncio.run(_run())

    assert result is fake_client
    mock_client_cls.connect.assert_awaited_once_with("localhost:7233", namespace="myns")


def test_connect_temporal_client_raises_on_failure(monkeypatch) -> None:
    from agent_provisioning_team.temporal import client

    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")

    with patch("temporalio.client.Client") as mock_client_cls:
        mock_client_cls.connect = AsyncMock(side_effect=RuntimeError("boom"))

        async def _run():
            return await client.connect_temporal_client()

        with pytest.raises(RuntimeError):
            asyncio.run(_run())


# ---------------------------------------------------------------------------
# start_workflow.py
# ---------------------------------------------------------------------------


def test_run_async_raises_without_client(monkeypatch) -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    with (
        patch.object(sw, "get_temporal_loop", return_value=None),
        patch.object(sw, "get_temporal_client", return_value=None),
    ):
        coro = asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="Temporal client not available"):
                sw._run_async(coro)
        finally:
            coro.close()


def test_start_provisioning_workflow_passes_args(monkeypatch) -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    fake_client = MagicMock()
    fake_client.start_workflow = AsyncMock()
    loop = asyncio.new_event_loop()

    captured: dict = {}

    def fake_run_async(coro):
        # Just close the coroutine without scheduling — we only need to
        # observe that the right call was queued.
        coro.close()
        captured["called"] = True

    with (
        patch.object(sw, "get_temporal_client", return_value=fake_client),
        patch.object(sw, "get_temporal_loop", return_value=loop),
        patch.object(sw, "_run_async", side_effect=fake_run_async),
    ):
        sw.start_provisioning_workflow(
            "job-1",
            "agent-1",
            "default.yaml",
            skip_phases=["setup"],
            prior_results={"setup": {"success": True}},
        )

    assert captured["called"] is True
    loop.close()


def test_start_provisioning_workflow_raises_without_client() -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    with patch.object(sw, "get_temporal_client", return_value=None):
        with pytest.raises(RuntimeError, match="Temporal client not available"):
            sw.start_provisioning_workflow("j", "a", "default.yaml")


# ---------------------------------------------------------------------------
# worker.py
# ---------------------------------------------------------------------------


def test_create_worker_returns_none_when_disabled() -> None:
    from agent_provisioning_team.temporal import worker as worker_mod

    with patch.object(worker_mod, "is_temporal_enabled", return_value=False):
        assert worker_mod.create_agent_provisioning_worker(client=MagicMock()) is None


def test_create_worker_returns_none_when_no_client() -> None:
    from agent_provisioning_team.temporal import worker as worker_mod

    with patch.object(worker_mod, "is_temporal_enabled", return_value=True):
        assert worker_mod.create_agent_provisioning_worker(client=None) is None


def test_create_worker_constructs_worker_when_enabled() -> None:
    from agent_provisioning_team.temporal import worker as worker_mod

    fake_worker = MagicMock(name="fake-worker")
    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(worker_mod, "Worker", return_value=fake_worker) as mock_worker_cls,
    ):
        result = worker_mod.create_agent_provisioning_worker(client=MagicMock())

    assert result is fake_worker
    mock_worker_cls.assert_called_once()
    # Ensure activities / workflows lists make it through
    _, kwargs = mock_worker_cls.call_args
    assert kwargs["task_queue"] == worker_mod.TASK_QUEUE
    assert worker_mod.AgentProvisioningWorkflow in kwargs["workflows"]
    assert worker_mod.AgentProvisioningWorkflowV2 in kwargs["workflows"]
    assert len(kwargs["activities"]) == 8


def test_start_worker_thread_no_op_when_disabled() -> None:
    import shared_temporal
    from agent_provisioning_team.temporal import worker as worker_mod

    # Patch the delegate too: otherwise the assertion passes even if the
    # function's own is_temporal_enabled() guard is removed, because
    # start_team_worker has its own gate. Asserting it is NOT called proves the
    # early return comes from the function's guard.
    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=False),
        patch.object(shared_temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_worker_thread_delegates_to_start_team_worker() -> None:
    """The entrypoint contract (TEAM_TEMPORAL_WORKER_FUNC) resolves to a real,
    idempotent function that boots the worker via shared_temporal."""
    import shared_temporal
    from agent_provisioning_team.temporal import worker as worker_mod

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(shared_temporal, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_temporal_worker_thread() is True
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert args[0] == "agent_provisioning"
        assert kwargs["task_queue"] == worker_mod.TASK_QUEUE


# ---------------------------------------------------------------------------
# activities.py — v1 + v2 surfaces
# ---------------------------------------------------------------------------


def test_v1_activity_delegates_to_run_provisioning_background() -> None:
    from agent_provisioning_team.temporal import activities

    with patch("agent_provisioning_team.api.main._run_provisioning_background") as mock_bg:
        activities.run_provisioning_activity("job-1", "agent-1", "default.yaml")

    mock_bg.assert_called_once_with("job-1", "agent-1", "default.yaml")


def test_safe_swallows_exceptions() -> None:
    from agent_provisioning_team.temporal import activities

    with patch.object(activities._js, "create_job", side_effect=RuntimeError("boom")):
        # Must not raise — this is "best-effort job_store call".
        activities._safe("create_job", "j", "a", "m")


def test_restored_writes_status_update() -> None:
    from agent_provisioning_team.temporal import activities

    with patch.object(activities, "_safe") as mock_safe:
        activities._restored("j-1", "setup", 15)

    # Should have called update_job once
    mock_safe.assert_called_with(
        "update_job",
        "j-1",
        current_phase="setup",
        progress=15,
        status_text="Restored setup from previous run",
    )


def test_credentials_activity_v2_restores_from_prior() -> None:
    from agent_provisioning_team.temporal import activities

    with (
        patch.object(activities, "_safe"),
        patch("temporalio.activity.heartbeat"),
    ):
        prior = {"success": True, "credentials": {}}
        payload = activities.credentials_activity_v2(
            "j", "a", "default.yaml", prior_credentials=prior
        )

    assert payload == {"success": True, "credentials": {}}


def test_credentials_activity_v2_runs_when_no_prior() -> None:
    from agent_provisioning_team.models import CredentialGenerationResult, GeneratedCredentials
    from agent_provisioning_team.temporal import activities

    fake_result = CredentialGenerationResult(
        success=True,
        credentials={"pg": GeneratedCredentials(tool_name="pg", username="u", password="p")},
    )

    fake_manifest = MagicMock()
    fake_orch = MagicMock()
    fake_orch.credential_store = MagicMock()

    with (
        patch.object(activities, "_safe"),
        patch(
            "agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=fake_manifest,
        ),
        patch(
            "agent_provisioning_team.phases.credential_generation.run_credential_generation",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.credentials_activity_v2("j", "a", "default.yaml")

    assert payload["success"] is True
    assert "pg" in payload["credentials"]


def test_credentials_activity_v2_raises_on_failure() -> None:
    from agent_provisioning_team.models import CredentialGenerationResult
    from agent_provisioning_team.temporal import activities

    fake_result = CredentialGenerationResult(success=False, credentials={}, error="cred boom")

    with (
        patch.object(activities, "_safe"),
        patch("agent_provisioning_team.orchestrator.ProvisioningOrchestrator"),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest", return_value=MagicMock()
        ),
        patch(
            "agent_provisioning_team.phases.credential_generation.run_credential_generation",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="cred boom"):
            activities.credentials_activity_v2("j", "a", "default.yaml")


def test_provision_tool_activity_calls_provisioner() -> None:
    from agent_provisioning_team.models import GeneratedCredentials, ToolProvisionResult
    from agent_provisioning_team.temporal import activities

    fake_provisioner = MagicMock()
    fake_provisioner.provision.return_value = ToolProvisionResult(
        tool_name="pg", success=True, provisioner_key="postgres_provisioner"
    )

    fake_tool = MagicMock()
    fake_tool.provisioner = "postgres_provisioner"
    fake_tool.config = {}
    fake_manifest = MagicMock()
    fake_manifest.get_tool.return_value = fake_tool

    with (
        patch.object(activities, "_safe"),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=fake_manifest,
        ),
        patch(
            "agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={"postgres_provisioner": fake_provisioner},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="pg", username="u", password="p")
        payload = activities.provision_tool_activity(
            "j",
            "a",
            "pg",
            "default.yaml",
            credentials_dump=creds.model_dump(),
            tools_completed_so_far=0,
            tools_total=1,
        )

    assert payload["success"] is True
    fake_provisioner.provision.assert_called_once()


def test_provision_tool_activity_raises_when_tool_missing() -> None:
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.temporal import activities

    fake_manifest = MagicMock()
    fake_manifest.get_tool.return_value = None

    with (
        patch.object(activities, "_safe"),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=fake_manifest,
        ),
        patch(
            "agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="x")
        with pytest.raises(RuntimeError, match="not in manifest"):
            activities.provision_tool_activity(
                "j", "a", "x", "default.yaml", credentials_dump=creds.model_dump()
            )


def test_provision_tool_activity_raises_when_provisioner_missing() -> None:
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.temporal import activities

    fake_tool = MagicMock()
    fake_tool.provisioner = "unknown_provisioner"
    fake_tool.config = {}
    fake_manifest = MagicMock()
    fake_manifest.get_tool.return_value = fake_tool

    with (
        patch.object(activities, "_safe"),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=fake_manifest,
        ),
        patch(
            "agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="x")
        with pytest.raises(RuntimeError, match="unknown provisioner"):
            activities.provision_tool_activity(
                "j", "a", "x", "default.yaml", credentials_dump=creds.model_dump()
            )


def test_audit_activity_v2_restores_from_prior() -> None:
    from agent_provisioning_team.models import AccessAuditResult
    from agent_provisioning_team.temporal import activities

    prior = AccessAuditResult(passed=True, verifications=[]).model_dump()
    with (
        patch.object(activities, "_safe"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.audit_activity_v2(
            "j", "a", "default.yaml", tool_results_dump=[], prior_audit=prior
        )

    assert payload["passed"] is True


def test_audit_activity_v2_runs_when_no_prior() -> None:
    from agent_provisioning_team.models import AccessAuditResult
    from agent_provisioning_team.temporal import activities

    fake_result = AccessAuditResult(passed=True, verifications=[])

    with (
        patch.object(activities, "_safe"),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=MagicMock(),
        ),
        patch(
            "agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={},
        ),
        patch(
            "agent_provisioning_team.phases.access_audit.run_access_audit",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.audit_activity_v2("j", "a", "default.yaml", tool_results_dump=[])

    assert payload["passed"] is True


def test_documentation_activity_v2_restores_from_prior() -> None:
    from agent_provisioning_team.temporal import activities

    prior = {"success": True, "onboarding": None}
    with (
        patch.object(activities, "_safe"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.documentation_activity_v2(
            "j",
            "a",
            "default.yaml",
            credentials_dump={},
            tool_results_dump=[],
            workspace_path="/ws",
            prior_documentation=prior,
        )

    assert payload == {"success": True, "onboarding": None}


def test_documentation_activity_v2_runs_when_no_prior() -> None:
    from agent_provisioning_team.models import DocumentationResult, OnboardingPacket
    from agent_provisioning_team.temporal import activities

    onboarding = OnboardingPacket(summary="s", tools=[], environment_variables={})
    fake_result = DocumentationResult(success=True, onboarding=onboarding)

    with (
        patch.object(activities, "_safe"),
        patch(
            "agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=MagicMock(),
        ),
        patch(
            "agent_provisioning_team.phases.documentation.run_documentation",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.documentation_activity_v2(
            "j",
            "a",
            "default.yaml",
            credentials_dump={},
            tool_results_dump=[],
            workspace_path="/ws",
        )

    assert payload["success"] is True
    assert payload["onboarding"]["summary"] == "s"


def test_deliver_activity_v2_success_path() -> None:
    from agent_provisioning_team.models import (
        DeliverResult,
        EnvironmentInfo,
        ProvisioningResult,
    )
    from agent_provisioning_team.temporal import activities

    env = EnvironmentInfo(container_id="c1", container_name="c1")
    fake_deliver = DeliverResult(success=True)
    final = ProvisioningResult(agent_id="a", success=True, environment=env)

    with (
        patch.object(activities, "_safe") as mock_safe,
        patch("agent_provisioning_team.phases.deliver.run_deliver", return_value=fake_deliver),
        patch("agent_provisioning_team.phases.deliver.build_final_result", return_value=final),
        patch(
            "agent_provisioning_team.phases.deliver.redact_credentials_for_response",
            return_value=final,
        ),
        patch("agent_provisioning_team.orchestrator.ProvisioningOrchestrator"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.deliver_activity_v2(
            "j",
            "a",
            environment_dump=env.model_dump(),
            credentials_dump={},
            tool_results_dump=[],
            audit_dump=None,
            onboarding_dump=None,
        )

    assert payload == {"success": True, "error": None}
    # Should have called mark_job_completed
    calls = [c.args[0] for c in mock_safe.call_args_list]
    assert "mark_job_completed" in calls


def test_deliver_activity_v2_failure_path() -> None:
    from agent_provisioning_team.models import (
        DeliverResult,
        ProvisioningResult,
    )
    from agent_provisioning_team.temporal import activities

    fake_deliver = DeliverResult(success=False, error="oops")
    final = ProvisioningResult(agent_id="a", success=False, error="oops")

    with (
        patch.object(activities, "_safe") as mock_safe,
        patch("agent_provisioning_team.phases.deliver.run_deliver", return_value=fake_deliver),
        patch("agent_provisioning_team.phases.deliver.build_final_result", return_value=final),
        patch("agent_provisioning_team.orchestrator.ProvisioningOrchestrator"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.deliver_activity_v2(
            "j",
            "a",
            environment_dump=None,
            credentials_dump={},
            tool_results_dump=[],
            audit_dump=None,
            onboarding_dump=None,
        )

    assert payload == {"success": False, "error": "oops"}
    calls = [c.args[0] for c in mock_safe.call_args_list]
    assert "mark_job_failed" in calls


def test_compensate_activity_v2_invokes_orchestrator() -> None:
    from agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    with patch(
        "agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
        return_value=fake_orch,
    ):
        activities.compensate_activity_v2(
            "agent-1",
            [
                {"tool_name": "pg", "provisioner_key": "postgres_provisioner"},
                {"tool_name": "redis", "provisioner_key": "redis_provisioner"},
            ],
        )

    fake_orch._compensate.assert_called_once()
    args, _ = fake_orch._compensate.call_args
    assert args[0] == "agent-1"
    shims = args[1]
    assert len(shims) == 2
    assert shims[0].tool_name == "pg"
    assert shims[0].provisioner_key == "postgres_provisioner"
    assert shims[0].success is True
