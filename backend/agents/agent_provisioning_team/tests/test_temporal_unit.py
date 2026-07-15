"""Unit-level tests for Temporal activities, client, worker, and start_workflow.

These tests do not need a live Temporal server; we mock at the
``temporalio`` boundary (``activity.heartbeat``, ``Client.connect``) and
verify the contracts each surface exposes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
    # importlib.reload mutates the shared constants module in place, so the
    # restore-reload MUST run even if the assertion below fails, or every
    # later test in this worker process that reads constants.TASK_QUEUE by
    # attribute access (not a frozen `from X import Y` binding) would
    # observe the overridden value for the rest of the session.
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "custom-queue")
    import importlib

    from agent_provisioning_team.temporal import constants as constants_mod

    try:
        reloaded = importlib.reload(constants_mod)
        assert reloaded.TASK_QUEUE == "custom-queue"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", raising=False)
        importlib.reload(constants_mod)


def test_sandbox_task_queue_default_differs_from_general_queue() -> None:
    from agent_provisioning_team.temporal import constants

    assert isinstance(constants.SANDBOX_TASK_QUEUE, str)
    assert constants.SANDBOX_TASK_QUEUE
    assert constants.SANDBOX_TASK_QUEUE != constants.TASK_QUEUE


def test_sandbox_task_queue_env_override(monkeypatch) -> None:
    # Same restore-must-always-run rationale as test_task_queue_env_override.
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", "custom-sandbox-queue")
    import importlib

    from agent_provisioning_team.temporal import constants as constants_mod

    try:
        reloaded = importlib.reload(constants_mod)
        assert reloaded.SANDBOX_TASK_QUEUE == "custom-sandbox-queue"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", raising=False)
        importlib.reload(constants_mod)


def test_workflows_activities_exclude_sandbox_items() -> None:
    """P1 regression: WORKFLOWS/ACTIVITIES (served by Pattern A's auto-boot,
    which also fires inside the standalone agent-provisioning-service team
    container) must never include sandbox workflows/activities — those run
    only on SANDBOX_TASK_QUEUE via a worker booted solely inside the unified
    API process."""
    from agent_provisioning_team import temporal as temporal_pkg

    workflow_names = {w.__name__ for w in temporal_pkg.WORKFLOWS}
    activity_names = {getattr(a, "__name__", str(a)) for a in temporal_pkg.ACTIVITIES}
    sandbox_workflow_names = {w.__name__ for w in temporal_pkg.SANDBOX_WORKFLOWS}
    sandbox_activity_names = {
        getattr(a, "__name__", str(a)) for a in temporal_pkg.SANDBOX_ACTIVITIES
    }

    assert workflow_names.isdisjoint(sandbox_workflow_names)
    assert activity_names.isdisjoint(sandbox_activity_names)
    assert sandbox_workflow_names == {
        "SandboxAcquireWorkflow",
        "SandboxTeardownWorkflow",
        "SandboxReaperWorkflow",
    }
    assert sandbox_activity_names == {
        "sandbox_acquire_activity",
        "sandbox_teardown_activity",
        "sandbox_reap_activity",
    }


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
    # data_converter is the shared gzip-codec DataConverter every team's client
    # now gets (see shared_temporal.codec) — not this test's concern.
    mock_client_cls.connect.assert_awaited_once_with(
        "localhost:7233", namespace="myns", data_converter=ANY
    )


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
    """Provision starter must start AgentProvisioningWorkflow (single type, no V1/V2)."""
    from agent_provisioning_team.temporal import start_workflow as sw
    from agent_provisioning_team.temporal.workflows import AgentProvisioningWorkflow

    fake_client = MagicMock()
    loop = asyncio.new_event_loop()
    captured_start: dict = {}

    async def capture_start_workflow(*args, **kwargs):
        captured_start["args"] = args
        captured_start["kwargs"] = kwargs

    fake_client.start_workflow = capture_start_workflow

    def fake_run_async(coro):
        loop.run_until_complete(coro)

    try:
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

        assert captured_start["args"][0] is AgentProvisioningWorkflow.run
        assert captured_start["kwargs"]["args"] == [
            "job-1",
            "agent-1",
            "default.yaml",
            ["setup"],
            {"setup": {"success": True}},
        ]
        assert captured_start["kwargs"]["id"] == "agent-provisioning-job-1"
        assert captured_start["kwargs"]["task_queue"] == sw.TASK_QUEUE
        assert "id_reuse_policy" not in captured_start["kwargs"]
    finally:
        loop.close()


def test_start_provisioning_workflow_replace_existing_terminates() -> None:
    from temporalio.common import WorkflowIDReusePolicy

    from agent_provisioning_team.temporal import start_workflow as sw
    from agent_provisioning_team.temporal.workflows import AgentProvisioningWorkflow

    fake_client = MagicMock()
    loop = asyncio.new_event_loop()
    captured_start: dict = {}

    async def capture_start_workflow(*args, **kwargs):
        captured_start["args"] = args
        captured_start["kwargs"] = kwargs

    fake_client.start_workflow = capture_start_workflow

    def fake_run_async(coro):
        loop.run_until_complete(coro)

    try:
        with (
            patch.object(sw, "get_temporal_client", return_value=fake_client),
            patch.object(sw, "get_temporal_loop", return_value=loop),
            patch.object(sw, "_run_async", side_effect=fake_run_async),
        ):
            sw.start_provisioning_workflow(
                "job-1",
                "agent-1",
                "default.yaml",
                replace_existing=True,
            )

        assert captured_start["args"][0] is AgentProvisioningWorkflow.run
        assert (
            captured_start["kwargs"]["id_reuse_policy"]
            is WorkflowIDReusePolicy.TERMINATE_IF_RUNNING
        )
        assert "id_conflict_policy" not in captured_start["kwargs"]
    finally:
        loop.close()


def test_start_provisioning_workflow_raises_without_client() -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    with patch.object(sw, "get_temporal_client", return_value=None):
        with pytest.raises(RuntimeError, match="Temporal client not available"):
            sw.start_provisioning_workflow("j", "a", "default.yaml")


def test_start_workflow_timeout_s_defaults_and_clamps(monkeypatch) -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    monkeypatch.delenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", raising=False)
    assert sw._start_workflow_timeout_s() == 30.0

    monkeypatch.setenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "0.25")
    assert sw._start_workflow_timeout_s() == 1.0

    monkeypatch.setenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "12.5")
    assert sw._start_workflow_timeout_s() == 12.5

    monkeypatch.setenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "not-a-number")
    assert sw._start_workflow_timeout_s() == 30.0

    monkeypatch.setenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "inf")
    assert sw._start_workflow_timeout_s() == 30.0


def test_start_workflow_timeout_s_handles_overflow(monkeypatch) -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    monkeypatch.setenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "999")
    with patch("builtins.float", side_effect=OverflowError("too large")):
        assert sw._start_workflow_timeout_s() == 30.0


def test_provisioning_workflow_is_open_not_found() -> None:
    from temporalio.service import RPCError, RPCStatusCode

    from agent_provisioning_team.temporal import start_workflow as sw

    err = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    future = MagicMock()
    future.result.side_effect = err

    def _submit(coro, _loop):
        coro.close()
        return future

    with (
        patch.object(sw, "get_temporal_client", return_value=MagicMock()),
        patch.object(sw, "get_temporal_loop", return_value=MagicMock()),
        patch.object(asyncio, "run_coroutine_threadsafe", side_effect=_submit),
    ):
        assert sw.provisioning_workflow_is_open("job-1") is False


def test_provisioning_workflow_is_open_when_client_missing() -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

    with (
        patch.object(sw, "get_temporal_client", return_value=None),
        patch.object(sw, "get_temporal_loop", return_value=None),
    ):
        assert sw.provisioning_workflow_is_open("job-1") is True


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
    from agent_provisioning_team.temporal import ACTIVITIES, WORKFLOWS
    from agent_provisioning_team.temporal import worker as worker_mod

    fake_worker = MagicMock(name="fake-worker")
    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(worker_mod, "Worker", return_value=fake_worker) as mock_worker_cls,
    ):
        result = worker_mod.create_agent_provisioning_worker(client=MagicMock())

    assert result is fake_worker
    mock_worker_cls.assert_called_once()
    # The worker must serve exactly the canonical WORKFLOWS/ACTIVITIES lists
    # from temporal/__init__.py — not a separately maintained copy — so the
    # two can never silently drift out of sync.
    _, kwargs = mock_worker_cls.call_args
    assert kwargs["task_queue"] == worker_mod.TASK_QUEUE
    assert kwargs["workflows"] is WORKFLOWS
    assert kwargs["activities"] is ACTIVITIES
    # Provisioning/deprovision only — sandbox workflows/activities are
    # deliberately excluded (they run on their own SANDBOX_TASK_QUEUE via a
    # separately-booted worker; see start_agent_provisioning_sandbox_temporal_worker_thread).
    from agent_provisioning_team.temporal.workflows import (
        AgentDeprovisioningWorkflow,
        AgentProvisioningWorkflow,
    )

    assert set(kwargs["workflows"]) == {AgentProvisioningWorkflow, AgentDeprovisioningWorkflow}


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


def test_start_sandbox_worker_thread_returns_false_when_disabled() -> None:
    import shared_temporal
    from agent_provisioning_team.temporal import worker as worker_mod

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=False),
        patch.object(shared_temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_sandbox_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_sandbox_worker_thread_uses_distinct_team_key_and_queue() -> None:
    """Must use a DIFFERENT team key and task queue from the general
    provisioning worker (P1 fix): sandbox activities must never be servable
    by the standalone agent-provisioning-service team container, which also
    calls start_agent_provisioning_temporal_worker_thread on TASK_QUEUE."""
    import shared_temporal
    from agent_provisioning_team.temporal import SANDBOX_ACTIVITIES, SANDBOX_WORKFLOWS
    from agent_provisioning_team.temporal import worker as worker_mod
    from agent_provisioning_team.temporal.constants import SANDBOX_TASK_QUEUE, TASK_QUEUE

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(shared_temporal, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_sandbox_temporal_worker_thread() is True
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert args[0] == "agent_provisioning_sandbox"
        assert args[0] != "agent_provisioning"
        assert kwargs["task_queue"] == SANDBOX_TASK_QUEUE
        assert kwargs["task_queue"] != TASK_QUEUE
        assert args[1] == SANDBOX_WORKFLOWS
        assert args[2] == SANDBOX_ACTIVITIES


# ---------------------------------------------------------------------------
# activities.py — per-phase activity surfaces
# ---------------------------------------------------------------------------


def test_best_effort_job_store_swallows_exceptions() -> None:
    from agent_provisioning_team.temporal import activities

    with patch.object(activities._js, "create_job", side_effect=RuntimeError("boom")):
        # Must not raise — this is "best-effort job_store call".
        activities._best_effort_job_store(activities._js.create_job, "j", "a", "m")


def test_best_effort_job_store_does_not_log_credential_payloads(caplog) -> None:
    from agent_provisioning_team.temporal import activities

    secrets = {"password": "s3cret", "api_key": "ak-live"}
    with (
        caplog.at_level("ERROR"),
        patch.object(activities._js, "add_completed_phase", side_effect=RuntimeError("boom")),
    ):
        activities._best_effort_job_store(
            activities._js.add_completed_phase,
            "job-1",
            "credential_generation",
            secrets,
        )
    joined = " ".join(r.message for r in caplog.records)
    assert "s3cret" not in joined
    assert "ak-live" not in joined
    assert "dict" in joined


def test_record_phase_restored_writes_status_update() -> None:
    from agent_provisioning_team.temporal import activities

    with patch.object(activities, "_best_effort_job_store") as mock_safe:
        activities._record_phase_restored("j-1", "setup", 15)

    # Should have called update_job once
    mock_safe.assert_called_with(
        activities._js.update_job,
        "j-1",
        current_phase="setup",
        progress=15,
        status_text="Restored setup from previous run",
    )


def test_credentials_activity_restores_from_prior() -> None:
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.temporal import activities

    stored = {
        "pg": GeneratedCredentials(tool_name="pg", username="u", password="secret"),
    }
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_provisioning_team.phases.credential_generation.get_stored_credentials",
            return_value=stored,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        prior = {"success": True, "tool_names": ["pg"], "credentials": {}}
        payload = activities.credentials_activity(
            "j", "a", "default.yaml", prior_credentials=prior
        )

    assert payload["success"] is True
    assert payload["credentials"]["pg"]["password"] == "secret"


def test_credentials_activity_checkpoint_omits_plaintext_secrets() -> None:
    from agent_provisioning_team.models import CredentialGenerationResult, GeneratedCredentials
    from agent_provisioning_team.temporal import activities

    fake_result = CredentialGenerationResult(
        success=True,
        credentials={"pg": GeneratedCredentials(tool_name="pg", username="u", password="p")},
    )
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch.object(activities, "_load_ctx", return_value=(MagicMock(), MagicMock())),
        patch(
            "agent_provisioning_team.phases.credential_generation.run_credential_generation",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.credentials_activity("j", "a", "default.yaml")

    assert payload["credentials"]["pg"]["password"] == "p"
    checkpoint = mock_phase.call_args.args[2]
    assert checkpoint["tool_names"] == ["pg"]
    assert checkpoint["credentials"] == {}


def test_credentials_activity_raises_on_failure() -> None:
    from agent_provisioning_team.models import CredentialGenerationResult
    from agent_provisioning_team.temporal import activities

    fake_result = CredentialGenerationResult(success=False, credentials={}, error="cred boom")

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities, "_load_ctx", return_value=(MagicMock(), MagicMock())),
        patch(
            "agent_provisioning_team.phases.credential_generation.run_credential_generation",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="cred boom"):
            activities.credentials_activity("j", "a", "default.yaml")


def test_provision_tool_activity_calls_provisioner() -> None:
    from agent_provisioning_team.models import GeneratedCredentials, ToolProvisionResult
    from agent_provisioning_team.temporal import activities

    fake_provisioner = MagicMock()
    fake_provisioner.provision.return_value = ToolProvisionResult(
        tool_name="generic", success=True, provisioner_key=None
    )

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={"generic_provisioner": fake_provisioner},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="api_token", username="u", password="p")
        payload = activities.provision_tool_activity(
            "j",
            "a",
            "api_token",
            credentials_dump=creds.model_dump(),
            tools_total=1,
            provisioner="generic_provisioner",
            tool_config={},
        )

    assert payload["success"] is True
    assert payload["tool_name"] == "api_token"
    assert payload["provisioner_key"] == "generic_provisioner"
    fake_provisioner.provision.assert_called_once()


def test_provision_tool_activity_raises_when_provisioner_missing() -> None:
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.temporal import activities

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="x")
        with pytest.raises(RuntimeError, match="unknown provisioner"):
            activities.provision_tool_activity(
                "j",
                "a",
                "x",
                credentials_dump=creds.model_dump(),
                tools_total=1,
                provisioner="unknown_provisioner",
            )


def test_audit_activity_restores_from_prior() -> None:
    from agent_provisioning_team.models import AccessAuditResult
    from agent_provisioning_team.temporal import activities

    prior = AccessAuditResult(passed=True, verifications=[]).model_dump()
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.audit_activity(
            "j", "a", "default.yaml", tool_results_dump=[], prior_audit=prior
        )

    assert payload["passed"] is True


def test_audit_activity_runs_when_no_prior() -> None:
    from agent_provisioning_team.models import AccessAuditResult
    from agent_provisioning_team.temporal import activities

    fake_result = AccessAuditResult(passed=True, verifications=[])

    with (
        patch.object(activities, "_best_effort_job_store"),
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
        payload = activities.audit_activity("j", "a", "default.yaml", tool_results_dump=[])

    assert payload["passed"] is True


def test_documentation_activity_restores_from_prior() -> None:
    from agent_provisioning_team.temporal import activities

    prior = {"success": True, "onboarding": None}
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.documentation_activity(
            "j",
            "a",
            "default.yaml",
            credentials_dump={},
            tool_results_dump=[],
            workspace_path="/ws",
            prior_documentation=prior,
        )

    assert payload == {"success": True, "onboarding": None}


def test_documentation_activity_runs_when_no_prior() -> None:
    from agent_provisioning_team.models import DocumentationResult, OnboardingPacket
    from agent_provisioning_team.temporal import activities

    onboarding = OnboardingPacket(summary="s", tools=[], environment_variables={})
    fake_result = DocumentationResult(success=True, onboarding=onboarding)

    with (
        patch.object(activities, "_best_effort_job_store"),
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
        payload = activities.documentation_activity(
            "j",
            "a",
            "default.yaml",
            credentials_dump={},
            tool_results_dump=[],
            workspace_path="/ws",
        )

    assert payload["success"] is True
    assert payload["onboarding"]["summary"] == "s"


def test_deliver_activity_success_path() -> None:
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
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "mark_job_completed") as mock_completed,
        patch("agent_provisioning_team.phases.deliver.run_deliver", return_value=fake_deliver),
        patch("agent_provisioning_team.phases.deliver.build_final_result", return_value=final),
        patch(
            "agent_provisioning_team.phases.deliver.redact_credentials_for_response",
            return_value=final,
        ),
        patch("agent_provisioning_team.shared.environment_store.EnvironmentStore"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.deliver_activity(
            "j",
            "a",
            environment_dump=env.model_dump(),
            credentials_dump={},
            tool_results_dump=[],
            audit_dump=None,
            onboarding_dump=None,
        )

    assert payload == {"success": True, "error": None}
    mock_completed.assert_called_once()


def test_deliver_activity_failure_path() -> None:
    from agent_provisioning_team.models import (
        DeliverResult,
        ProvisioningResult,
    )
    from agent_provisioning_team.temporal import activities

    fake_deliver = DeliverResult(success=False, error="oops")
    final = ProvisioningResult(agent_id="a", success=False, error="oops")

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "mark_job_failed") as mock_failed,
        patch("agent_provisioning_team.phases.deliver.run_deliver", return_value=fake_deliver),
        patch("agent_provisioning_team.phases.deliver.build_final_result", return_value=final),
        patch("agent_provisioning_team.shared.environment_store.EnvironmentStore"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.deliver_activity(
            "j",
            "a",
            environment_dump=None,
            credentials_dump={},
            tool_results_dump=[],
            audit_dump=None,
            onboarding_dump=None,
        )

    assert payload == {"success": False, "error": "oops"}
    mock_failed.assert_called_once_with("j", error="oops")


def test_deliver_activity_raises_when_terminal_job_store_fails() -> None:
    from agent_provisioning_team.models import DeliverResult, ProvisioningResult
    from agent_provisioning_team.temporal import activities

    fake_deliver = DeliverResult(success=True)
    final = ProvisioningResult(agent_id="a", success=True)

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(
            activities._js,
            "mark_job_completed",
            side_effect=RuntimeError("store down"),
        ),
        patch("agent_provisioning_team.phases.deliver.run_deliver", return_value=fake_deliver),
        patch("agent_provisioning_team.phases.deliver.build_final_result", return_value=final),
        patch(
            "agent_provisioning_team.phases.deliver.redact_credentials_for_response",
            return_value=final,
        ),
        patch("agent_provisioning_team.shared.environment_store.EnvironmentStore"),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            activities.deliver_activity(
                "j",
                "a",
                environment_dump=None,
                credentials_dump={},
                tool_results_dump=[],
                audit_dump=None,
                onboarding_dump=None,
            )


def test_compensate_activity_invokes_orchestrator() -> None:
    from agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    with patch(
        "agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
        return_value=fake_orch,
    ):
        activities.compensate_activity(
            "agent-1",
            [
                {"tool_name": "pg", "provisioner_key": "postgres_provisioner"},
                {"tool_name": "redis", "provisioner_key": "redis_provisioner"},
            ],
        )

    fake_orch.compensate.assert_called_once()
    args, _ = fake_orch.compensate.call_args
    assert args[0] == "agent-1"
    shims = args[1]
    assert len(shims) == 2
    assert shims[0].tool_name == "pg"
    assert shims[0].provisioner_key == "postgres_provisioner"
    assert shims[0].success is True


def test_mark_job_failed_activity_writes_job_store() -> None:
    from agent_provisioning_team.temporal import activities

    with patch.object(activities._js, "mark_job_failed") as mock_fail:
        activities.mark_job_failed_activity("job-1", "boom")

    mock_fail.assert_called_once_with("job-1", error="boom")


def test_mark_job_failed_activity_raises_when_job_store_fails() -> None:
    from agent_provisioning_team.temporal import activities

    with patch.object(
        activities._js,
        "mark_job_failed",
        side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            activities.mark_job_failed_activity("job-1", "boom")


def test_mark_job_failed_activity_rejects_empty_inputs() -> None:
    from agent_provisioning_team.temporal import activities

    with pytest.raises(AssertionError):
        activities.mark_job_failed_activity("", "err")
    with pytest.raises(AssertionError):
        activities.mark_job_failed_activity("job-1", "")


def test_record_account_provisioning_sets_tool_counts() -> None:
    from agent_provisioning_team.temporal import activities

    results = [
        {"tool_name": "postgresql", "success": True},
        {"tool_name": "redis", "success": True},
    ]
    fake_env = MagicMock()
    with (
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch.object(activities._js, "update_job") as mock_update,
        patch(
            "agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=fake_env,
        ),
    ):
        out = activities.record_account_provisioning_activity("job-1", results, "agent-1")

    assert out == {"success": True, "tool_results": results}
    mock_phase.assert_called_once_with(
        "job-1",
        "account_provisioning",
        {"success": True, "tool_results": results},
    )
    mock_update.assert_called_once_with(
        "job-1",
        progress=60,
        status_text="Account provisioning complete",
        current_tool=None,
        tools_completed=2,
        tools_total=2,
    )
    fake_env.add_tools.assert_called_once_with("agent-1", ["postgresql", "redis"])


def test_record_account_provisioning_raises_when_job_store_fails() -> None:
    from agent_provisioning_team.temporal import activities

    results = [{"tool_name": "postgresql", "success": True}]
    with patch.object(
        activities._js,
        "add_completed_phase",
        side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            activities.record_account_provisioning_activity("job-1", results, "agent-1")


def test_list_manifest_tools_activity_returns_ordered_names(tmp_path) -> None:
    from agent_provisioning_team.temporal import activities

    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        """
version: "1.0"
tools:
  - name: postgresql
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
  - name: redis
    provisioner: redis_provisioner
    config: {key_prefix: "k:"}
""",
        encoding="utf-8",
    )
    out = activities.list_manifest_tools_activity(str(manifest))
    assert [t["name"] for t in out] == ["postgresql", "redis"]
    assert out[0]["provisioner"] == "postgres_provisioner"
    assert out[1]["provisioner"] == "redis_provisioner"
    assert isinstance(out[0]["config"], dict)


def test_list_manifest_tools_activity_rejects_empty_path() -> None:
    from agent_provisioning_team.temporal import activities

    with pytest.raises(AssertionError):
        activities.list_manifest_tools_activity("")

# ---------------------------------------------------------------------------
# setup_activity — moved from test_workflows_unit
# ---------------------------------------------------------------------------


def test_setup_activity_progress_path() -> None:
    """Fresh setup writes progress / completed phase into job_store and returns env dump."""
    from agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_provisioning_team.temporal import activities as t_acts

    fake_setup_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1"),
    )
    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    recorded = []

    def fake_safe(fn, *args, **kwargs):
        recorded.append({"fn": getattr(fn, "__name__", fn), "args": args, "kwargs": kwargs})

    with (
        patch.object(t_acts, "_best_effort_job_store", side_effect=fake_safe),
        patch.object(t_acts._js, "add_completed_phase") as mock_phase,
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_provisioning_team.phases.setup.run_setup",
            return_value=fake_setup_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity("j", "a", "default.yaml")

    assert payload["success"] is True
    assert payload["environment"]["container_id"] == "c1"
    # mark_job_running + update_job were invoked as best-effort; checkpoint is durable.
    fn_names = [r["fn"] for r in recorded]
    assert "mark_job_running" in fn_names
    assert "update_job" in fn_names
    mock_phase.assert_called_once()


def test_setup_activity_raises_when_setup_fails() -> None:
    """Failed setup raises RuntimeError so Temporal can retry the activity."""
    from agent_provisioning_team.models import SetupResult
    from agent_provisioning_team.temporal import activities as t_acts

    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_provisioning_team.phases.setup.run_setup",
            return_value=SetupResult(success=False, error="setup boom"),
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="setup boom"):
            t_acts.setup_activity("j", "a", "default.yaml")


def test_setup_activity_restores_from_prior() -> None:
    """When prior_setup is provided, setup is skipped and the snapshot is restored."""
    from agent_provisioning_team.temporal import activities as t_acts

    prior = {
        "success": True,
        "environment": {
            "container_id": "c1",
            "container_name": "c1",
            "workspace_path": "/w",
            "status": "running",
        },
    }
    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity("j", "a", "default.yaml", prior_setup=prior)
    assert payload["success"] is True
    assert payload["environment"]["container_id"] == "c1"


def test_no_legacy_v2_or_thread_fallback_symbols() -> None:
    """Hard cutover acceptance: no V2 type, v2 activities, or thread fallback knob.

    Full package scan also lives in ``test_orchestrator``; this keeps the
    Temporal suite self-contained for the same criterion.
    """
    from pathlib import Path

    import agent_provisioning_team

    root = Path(agent_provisioning_team.__file__).resolve().parent
    forbidden = (
        "AgentProvisioningWorkflow" + "V2",
        "_activity" + "_v2",
        "PROVISION_THREAD_" + "FALLBACK",
    )
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(root)}:{token}")
    assert hits == [], f"legacy cutover symbols still present: {hits}"


