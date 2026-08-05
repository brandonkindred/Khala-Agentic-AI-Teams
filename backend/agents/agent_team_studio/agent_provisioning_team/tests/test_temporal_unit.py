"""Unit-level tests for Temporal activities, client, worker, and start_workflow.

These tests do not need a live Temporal server; we mock at the
``temporalio`` boundary (``activity.heartbeat``, ``Client.connect``) and
verify the contracts each surface exposes.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_task_queue_default() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import constants

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

    from agent_team_studio.agent_provisioning_team.temporal import constants as constants_mod

    try:
        reloaded = importlib.reload(constants_mod)
        assert reloaded.TASK_QUEUE == "custom-queue"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", raising=False)
        importlib.reload(constants_mod)


def test_sandbox_task_queue_default_differs_from_general_queue() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import constants

    assert isinstance(constants.SANDBOX_TASK_QUEUE, str)
    assert constants.SANDBOX_TASK_QUEUE
    assert constants.SANDBOX_TASK_QUEUE != constants.TASK_QUEUE


def test_sandbox_task_queue_env_override(monkeypatch) -> None:
    # Same restore-must-always-run rationale as test_task_queue_env_override.
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", "custom-sandbox-queue")
    import importlib

    from agent_team_studio.agent_provisioning_team.temporal import constants as constants_mod

    try:
        reloaded = importlib.reload(constants_mod)
        assert reloaded.SANDBOX_TASK_QUEUE == "custom-sandbox-queue"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", raising=False)
        importlib.reload(constants_mod)


def test_workflows_activities_exclude_sandbox_items() -> None:
    """P1 regression: WORKFLOWS/ACTIVITIES (served by the main provisioning
    worker that team_service boots) must never include sandbox
    workflows/activities — those run only on SANDBOX_TASK_QUEUE via a worker
    booted solely inside the unified API process."""
    from agent_team_studio.agent_provisioning_team import temporal as temporal_pkg

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


def test_activities_includes_every_activity_a_workflow_schedules() -> None:
    """P1 regression: every activity name AgentProvisioningWorkflow /
    AgentDeprovisioningWorkflow schedules via workflow.execute_activity must
    be present in the canonical ACTIVITIES list registered with the worker
    (temporal/__init__.py) — an activity missing here has no worker able to
    execute it, so the scheduling workflow hangs until its activity timeout
    on every real run. Unit tests that stub workflow.execute_activity (see
    test_workflows_unit.py) cannot catch this class of omission themselves,
    since they never consult ACTIVITIES."""
    import re

    from agent_team_studio.agent_provisioning_team import temporal as temporal_pkg
    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf

    source = inspect.getsource(wf)
    scheduled = set(re.findall(r"_activities\.(\w+_activity)\b", source))
    assert scheduled, "expected to find at least one _activities.*_activity reference"

    activity_names = {getattr(a, "__name__", str(a)) for a in temporal_pkg.ACTIVITIES}
    missing = scheduled - activity_names
    assert not missing, (
        f"activities scheduled by workflows.py but missing from ACTIVITIES: {missing}"
    )


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------


def test_client_helpers_default_env(monkeypatch) -> None:
    from shared.temporal import client

    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    assert client.get_temporal_address() is None
    assert client.get_temporal_namespace() == "default"
    assert client.is_temporal_enabled() is False


def test_client_helpers_with_env(monkeypatch) -> None:
    from shared.temporal import client

    monkeypatch.setenv("TEMPORAL_ADDRESS", "  localhost:7233  ")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "  myns  ")
    assert client.get_temporal_address() == "localhost:7233"
    assert client.get_temporal_namespace() == "myns"
    assert client.is_temporal_enabled() is True


def test_client_get_and_set() -> None:
    from shared.temporal import client

    sentinel = MagicMock(name="fake-client")
    client.set_temporal_client(sentinel)
    assert client.get_temporal_client() is sentinel
    client.set_temporal_client(None)
    assert client.get_temporal_client() is None


def test_loop_get_and_set() -> None:
    from shared.temporal import client

    loop = asyncio.new_event_loop()
    try:
        client.set_temporal_loop(loop)
        assert client.get_temporal_loop() is loop
    finally:
        client.set_temporal_loop(None)
        loop.close()
    assert client.get_temporal_loop() is None


def test_connect_temporal_client_returns_none_when_address_blank(monkeypatch) -> None:
    from shared.temporal import client

    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)

    async def _run():
        result = await client.connect_temporal_client()
        return result

    assert asyncio.run(_run()) is None


def test_connect_temporal_client_connects_when_address_set(monkeypatch) -> None:
    from shared.temporal import client

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
    # now gets (see shared.temporal.codec) — not this test's concern.
    mock_client_cls.connect.assert_awaited_once_with(
        "localhost:7233", namespace="myns", data_converter=ANY
    )


def test_connect_temporal_client_raises_on_failure(monkeypatch) -> None:
    from shared.temporal import client

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
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    with patch.object(
        sw,
        "_await_client",
        side_effect=RuntimeError("Temporal client not available; is the team's worker running?"),
    ):
        coro = asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="Temporal client not available"):
                sw._run_async(coro)
        finally:
            coro.close()


def test_start_provisioning_workflow_passes_args(monkeypatch) -> None:
    """Provision starter must start AgentProvisioningWorkflow (single type, no V1/V2)."""
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw
    from agent_team_studio.agent_provisioning_team.temporal.workflows import (
        AgentProvisioningWorkflow,
    )

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


def test_start_provisioning_workflow_replace_existing_allows_duplicate() -> None:
    """Resume/restart reuses a closed workflow id without terminating a live sibling."""
    from temporalio.common import WorkflowIDReusePolicy

    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw
    from agent_team_studio.agent_provisioning_team.temporal.workflows import (
        AgentProvisioningWorkflow,
    )

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
        assert captured_start["kwargs"]["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE
        assert "id_conflict_policy" not in captured_start["kwargs"]
    finally:
        loop.close()


def test_start_provisioning_workflow_raises_without_client() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    with patch.object(
        sw,
        "_await_client",
        side_effect=RuntimeError("Temporal client not available; is the team's worker running?"),
    ):
        with pytest.raises(RuntimeError, match="Temporal client not available"):
            sw.start_provisioning_workflow("j", "a", "default.yaml")


def test_start_workflow_timeout_s_defaults_and_clamps(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

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
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    monkeypatch.setenv("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "999")
    with patch("builtins.float", side_effect=OverflowError("too large")):
        assert sw._start_workflow_timeout_s() == 30.0


def test_provisioning_workflow_is_open_not_found() -> None:
    from temporalio.service import RPCError, RPCStatusCode

    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

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
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    with (
        patch.object(sw, "get_temporal_client", return_value=None),
        patch.object(sw, "get_temporal_loop", return_value=None),
    ):
        assert sw.provisioning_workflow_is_open("job-1") is True


# ---------------------------------------------------------------------------
# worker.py
# ---------------------------------------------------------------------------


def test_create_worker_returns_none_when_disabled() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    with patch.object(worker_mod, "is_temporal_enabled", return_value=False):
        assert worker_mod.create_agent_provisioning_worker(client=MagicMock()) is None


def test_create_worker_returns_none_when_no_client() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    with patch.object(worker_mod, "is_temporal_enabled", return_value=True):
        assert worker_mod.create_agent_provisioning_worker(client=None) is None


def test_create_worker_constructs_worker_when_enabled() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import ACTIVITIES, WORKFLOWS
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

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
    from agent_team_studio.agent_provisioning_team.temporal.workflows import (
        AgentDeprovisioningWorkflow,
        AgentProvisioningWorkflow,
    )

    assert set(kwargs["workflows"]) == {AgentProvisioningWorkflow, AgentDeprovisioningWorkflow}


def test_start_worker_thread_no_op_when_disabled() -> None:
    import shared.temporal
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    # Patch the delegate too: otherwise the assertion passes even if the
    # function's own is_temporal_enabled() guard is removed, because
    # start_team_worker has its own gate. Asserting it is NOT called proves the
    # early return comes from the function's guard.
    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=False),
        patch.object(shared.temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_worker_thread_delegates_to_start_team_worker() -> None:
    """The entrypoint contract (TEAM_TEMPORAL_WORKER_FUNC) resolves to a real,
    idempotent function that boots the worker via shared.temporal."""
    import shared.temporal
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(shared.temporal, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_temporal_worker_thread() is True
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert args[0] == "agent_provisioning"
        assert kwargs["task_queue"] == worker_mod.TASK_QUEUE


def test_start_sandbox_worker_thread_returns_false_when_disabled() -> None:
    import shared.temporal
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=False),
        patch.object(shared.temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_agent_provisioning_sandbox_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_sandbox_worker_thread_uses_distinct_team_key_and_queue() -> None:
    """Must use a DIFFERENT team key and task queue from the general
    provisioning worker (P1 fix): sandbox activities must never be servable
    by the standalone agent-provisioning-service team container, which also
    calls start_agent_provisioning_temporal_worker_thread on TASK_QUEUE."""
    import shared.temporal
    from agent_team_studio.agent_provisioning_team.temporal import (
        SANDBOX_ACTIVITIES,
        SANDBOX_WORKFLOWS,
    )
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod
    from agent_team_studio.agent_provisioning_team.temporal.constants import (
        SANDBOX_TASK_QUEUE,
        TASK_QUEUE,
    )

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(shared.temporal, "start_team_worker", return_value=True) as mock_start,
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
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with patch.object(activities._js, "create_job", side_effect=RuntimeError("boom")):
        # Must not raise — this is "best-effort job_store call".
        activities._best_effort_job_store(activities._js.create_job, "j", "a", "m")


def test_best_effort_job_store_skips_non_callable(caplog) -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with caplog.at_level("ERROR"):
        activities._best_effort_job_store(None, "j")  # type: ignore[arg-type]
    assert any("not callable" in r.message for r in caplog.records)


def test_best_effort_job_store_does_not_log_credential_payloads(caplog) -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

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
    from agent_team_studio.agent_provisioning_team.temporal import activities

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


# ---------------------------------------------------------------------------
# Per-agent_id lock activities
# ---------------------------------------------------------------------------


def test_acquire_agent_lock_activity_uses_configured_ttl() -> None:
    """P1 regression: the AGENT_PROVISIONING_LOCK_TTL_S constant must reach
    AgentLockStore's constructor, not just get parsed and discarded."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    captured: dict = {}

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            captured["ttl_seconds"] = ttl_seconds

        def acquire(self, agent_id, owner):
            captured["acquire_args"] = (agent_id, owner)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch("agent_team_studio.agent_provisioning_team.temporal.constants.LOCK_TTL_S", 321),
        patch("temporalio.activity.heartbeat"),
    ):
        activities.acquire_agent_lock_activity("job-1", "agent-1")

    assert captured["ttl_seconds"] == 321
    assert captured["acquire_args"] == ("agent-1", "job-1")


def test_acquire_agent_lock_activity_returns_fencing_token() -> None:
    """The workflow needs the minted token to thread into every subsequent
    mutating activity call."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def acquire(self, agent_id, owner):
            return 42

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        token = activities.acquire_agent_lock_activity("job-1", "agent-1")

    assert token == 42


def test_release_agent_lock_activity_forwards_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    captured: dict = {}

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def release(self, agent_id, owner, fencing_token=None):
            captured["fencing_token"] = fencing_token

    with patch(
        "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
    ):
        activities.release_agent_lock_activity("job-1", "agent-1", fencing_token=7)

    assert captured["fencing_token"] == 7


def test_release_agent_lock_activity_uses_configured_ttl() -> None:
    """P1 regression: same TTL-wiring requirement as acquire, for release."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    captured: dict = {}

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            captured["ttl_seconds"] = ttl_seconds

        def release(self, agent_id, owner, fencing_token=None):
            captured["release_args"] = (agent_id, owner)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch("agent_team_studio.agent_provisioning_team.temporal.constants.LOCK_TTL_S", 321),
    ):
        activities.release_agent_lock_activity("job-1", "agent-1")

    assert captured["ttl_seconds"] == 321
    assert captured["release_args"] == ("agent-1", "job-1")


def test_acquire_agent_lock_activity_translates_busy_error() -> None:
    """A busy lock surfaces as a plain (retryable) RuntimeError, not
    AgentLockBusyError, so Temporal's retry policy keeps polling."""
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockBusyError
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def acquire(self, agent_id, owner):
            raise AgentLockBusyError(agent_id, "other-job")

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="other-job"):
            activities.acquire_agent_lock_activity("job-1", "agent-1")


def test_credentials_activity_restores_from_prior() -> None:
    from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials
    from agent_team_studio.agent_provisioning_team.temporal import activities

    stored = {
        "pg": GeneratedCredentials(tool_name="pg", username="u", password="secret"),
    }
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.get_stored_credentials",
            return_value=stored,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        prior = {"success": True, "tool_names": ["pg"], "credentials": {}}
        payload = activities.credentials_activity("j", "a", "default.yaml", prior_credentials=prior)

    assert payload["success"] is True
    assert payload["credentials"]["pg"]["password"] == "secret"


def test_credentials_activity_checkpoint_omits_plaintext_secrets() -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        CredentialGenerationResult,
        GeneratedCredentials,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_result = CredentialGenerationResult(
        success=True,
        credentials={"pg": GeneratedCredentials(tool_name="pg", username="u", password="p")},
    )
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch.object(activities, "_load_ctx", return_value=(MagicMock(), MagicMock())),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.run_credential_generation",
            return_value=fake_result,
        ) as mock_run,
        patch("temporalio.activity.heartbeat"),
    ):
        specs = [{"name": "pg", "provisioner": "postgres_provisioner", "config": {}}]
        payload = activities.credentials_activity(
            "j", "a", "default.yaml", prior_credentials=None, tool_specs=specs
        )

    assert payload["credentials"]["pg"]["password"] == "p"
    checkpoint = mock_phase.call_args.args[2]
    assert checkpoint["tool_names"] == ["pg"]
    assert checkpoint["credentials"] == {}
    assert mock_run.call_args.kwargs["tool_names"] == ["pg"]


def test_credentials_activity_raises_on_failure() -> None:
    from agent_team_studio.agent_provisioning_team.models import CredentialGenerationResult
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_result = CredentialGenerationResult(success=False, credentials={}, error="cred boom")

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities, "_load_ctx", return_value=(MagicMock(), MagicMock())),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.run_credential_generation",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="cred boom"):
            activities.credentials_activity("j", "a", "default.yaml")


def test_credentials_activity_rejects_stale_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def check_fencing_token(self, agent_id, token):
            raise StaleFencingTokenError(agent_id, "agent_lock", token, token + 1)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.run_credential_generation"
        ) as fake_run,
    ):
        with pytest.raises(StaleFencingTokenError):
            activities.credentials_activity("j", "a", "default.yaml", fencing_token=1)

    fake_run.assert_not_called()


def test_deliver_activity_rejects_stale_fencing_token() -> None:
    """deliver_activity must reject a stale token up front via the lock-store
    preflight, before run_deliver's own EnvironmentStore check — closing the
    window where a reclaiming owner has bumped the lock but not yet the env."""
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def check_fencing_token(self, agent_id, token):
            raise StaleFencingTokenError(agent_id, "agent_lock", token, token + 1)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch.object(activities, "_best_effort_job_store"),
        patch("agent_team_studio.agent_provisioning_team.phases.deliver.run_deliver") as fake_run,
    ):
        with pytest.raises(StaleFencingTokenError):
            activities.deliver_activity("j", "a", None, {}, [], None, None, fencing_token=1)

    # Rejected before any deliver-phase mutation ran.
    fake_run.assert_not_called()


def test_provision_tool_activity_calls_provisioner() -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_provisioner = MagicMock()
    fake_provisioner.provision.return_value = ToolProvisionResult(
        tool_name="generic", success=True, provisioner_key=None
    )

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
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


def test_provision_tool_activity_threads_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_provisioner = MagicMock()
    fake_provisioner.provision.return_value = ToolProvisionResult(
        tool_name="generic", success=True, provisioner_key=None
    )

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={"generic_provisioner": fake_provisioner},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="api_token", username="u", password="p")
        activities.provision_tool_activity(
            "j",
            "a",
            "api_token",
            credentials_dump=creds.model_dump(),
            tools_total=1,
            provisioner="generic_provisioner",
            tool_config={},
            fencing_token=13,
        )

    assert fake_provisioner.provision.call_args.kwargs["fencing_token"] == 13


def test_provision_tool_activity_injects_job_id_for_docker_provisioner() -> None:
    """The write side of the labeling contract: provision_tool_activity must
    actually stash job_id into the config dict it hands to
    DockerProvisionerTool.provision, under docker_provisioner.JOB_ID_CONFIG_KEY
    -- every test elsewhere only exercises the read side (_do_provision
    handling a hand-built config), never this construction.
    """
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
    )

    fake_provisioner = MagicMock()
    fake_provisioner.provision.return_value = ToolProvisionResult(
        tool_name="docker", success=True, provisioner_key=None
    )

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={"docker_provisioner": fake_provisioner},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="docker")
        activities.provision_tool_activity(
            "job-77",
            "a",
            "docker",
            credentials_dump=creds.model_dump(),
            tools_total=1,
            provisioner="docker_provisioner",
            tool_config={"base_image": "python:3.11"},
        )

    _args, kwargs = fake_provisioner.provision.call_args
    assert kwargs["config"][JOB_ID_CONFIG_KEY] == "job-77"
    assert kwargs["config"]["base_image"] == "python:3.11"


def test_provision_tool_activity_does_not_inject_job_id_for_non_docker_provisioner() -> None:
    """Regression guard: job_id must NOT be injected for any provisioner other
    than docker_provisioner. generic_provisioner in particular echoes its
    whole config dict verbatim into persisted/returned state with no
    redaction for an unrecognized key -- injecting unconditionally would leak
    the internal job_id into checkpoints and API responses.
    """
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
    )

    fake_provisioner = MagicMock()
    fake_provisioner.provision.return_value = ToolProvisionResult(
        tool_name="generic", success=True, provisioner_key=None
    )

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={"generic_provisioner": fake_provisioner},
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        creds = GeneratedCredentials(tool_name="api_token", username="u", password="p")
        activities.provision_tool_activity(
            "job-77",
            "a",
            "api_token",
            credentials_dump=creds.model_dump(),
            tools_total=1,
            provisioner="generic_provisioner",
            tool_config={},
        )

    _args, kwargs = fake_provisioner.provision.call_args
    assert JOB_ID_CONFIG_KEY not in kwargs["config"]


def test_provision_tool_activity_raises_when_provisioner_missing() -> None:
    from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
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


def test_provision_tool_activity_rejects_stale_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def check_fencing_token(self, agent_id, token):
            raise StaleFencingTokenError(agent_id, "agent_lock", token, token + 1)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents"
        ) as fake_registry,
    ):
        with pytest.raises(StaleFencingTokenError):
            activities.provision_tool_activity(
                "j",
                "a",
                "x",
                credentials_dump={},
                tools_total=1,
                provisioner="postgres_provisioner",
                fencing_token=1,
            )

    fake_registry.assert_not_called()


def test_audit_activity_restores_from_prior() -> None:
    from agent_team_studio.agent_provisioning_team.models import AccessAuditResult
    from agent_team_studio.agent_provisioning_team.temporal import activities

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
    from agent_team_studio.agent_provisioning_team.models import AccessAuditResult
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_result = AccessAuditResult(passed=True, verifications=[])

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=MagicMock(),
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_agent_registry.build_default_tool_agents",
            return_value={},
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.access_audit.run_access_audit",
            return_value=fake_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.audit_activity("j", "a", "default.yaml", tool_results_dump=[])

    assert payload["passed"] is True


def test_documentation_activity_restores_from_prior() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

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
    from agent_team_studio.agent_provisioning_team.models import (
        DocumentationResult,
        OnboardingPacket,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    onboarding = OnboardingPacket(summary="s", tools=[], environment_variables={})
    fake_result = DocumentationResult(success=True, onboarding=onboarding)

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.tool_manifest.load_manifest",
            return_value=MagicMock(),
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.documentation.run_documentation",
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
    from agent_team_studio.agent_provisioning_team.models import (
        DeliverResult,
        EnvironmentInfo,
        ProvisioningResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    env = EnvironmentInfo(container_id="c1", container_name="c1")
    fake_deliver = DeliverResult(success=True)
    final = ProvisioningResult(agent_id="a", success=True, environment=env)

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "mark_job_completed") as mock_completed,
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.run_deliver",
            return_value=fake_deliver,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.build_final_result",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.redact_credentials_for_response",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore"
        ),
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


def test_deliver_activity_threads_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        DeliverResult,
        EnvironmentInfo,
        ProvisioningResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    env = EnvironmentInfo(container_id="c1", container_name="c1")
    fake_deliver = DeliverResult(success=True)
    final = ProvisioningResult(agent_id="a", success=True, environment=env)

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "mark_job_completed"),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.run_deliver",
            return_value=fake_deliver,
        ) as mock_run_deliver,
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.build_final_result",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.redact_credentials_for_response",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore"
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        activities.deliver_activity(
            "j",
            "a",
            environment_dump=env.model_dump(),
            credentials_dump={},
            tool_results_dump=[],
            audit_dump=None,
            onboarding_dump=None,
            fencing_token=17,
        )

    assert mock_run_deliver.call_args.kwargs["fencing_token"] == 17


def test_deliver_activity_failure_path() -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        DeliverResult,
        ProvisioningResult,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_deliver = DeliverResult(success=False, error="oops")
    final = ProvisioningResult(agent_id="a", success=False, error="oops")

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "mark_job_failed") as mock_failed,
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.run_deliver",
            return_value=fake_deliver,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.build_final_result",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore"
        ),
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
    from agent_team_studio.agent_provisioning_team.models import DeliverResult, ProvisioningResult
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_deliver = DeliverResult(success=True)
    final = ProvisioningResult(agent_id="a", success=True)

    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(
            activities._js,
            "mark_job_completed",
            side_effect=RuntimeError("store down"),
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.run_deliver",
            return_value=fake_deliver,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.build_final_result",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.deliver.redact_credentials_for_response",
            return_value=final,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore"
        ),
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
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = True
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.return_value = True
    fake_orch.tool_agents.get.return_value._container_exists.return_value = False
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases") as mock_clear,
    ):
        activities.compensate_activity(
            "agent-1",
            [
                {"tool_name": "pg", "provisioner_key": "postgres_provisioner"},
                {"tool_name": "redis", "provisioner_key": "redis_provisioner"},
            ],
            job_id="job-1",
        )

    fake_orch.compensate.assert_called_once()
    args, kwargs = fake_orch.compensate.call_args
    assert args[0] == "agent-1"
    shims = args[1]
    assert len(shims) == 2
    assert shims[0].tool_name == "pg"
    assert shims[0].provisioner_key == "postgres_provisioner"
    assert shims[0].success is True
    assert kwargs["tear_down_environment"] is True
    mock_clear.assert_called_once_with("job-1")
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_called_once_with(
        "agent-1"
    )


def test_compensate_activity_threads_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    with patch(
        "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
        return_value=fake_orch,
    ):
        activities.compensate_activity("agent-1", [], fencing_token=5)

    fake_orch.compensate.assert_called_once_with(
        "agent-1", [], tear_down_environment=True, fencing_token=5
    )


def test_compensate_activity_rejects_stale_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def check_fencing_token(self, agent_id, token):
            raise StaleFencingTokenError(agent_id, "agent_lock", token, token + 1)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator"
        ) as fake_orch_cls,
    ):
        with pytest.raises(StaleFencingTokenError):
            activities.compensate_activity("agent-1", [], fencing_token=1)

    fake_orch_cls.assert_not_called()


def test_compensate_activity_clears_phases_so_resume_reruns_credentials() -> None:
    """After compensate tears down CredentialStore, completed phases must not skip."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator"
        ) as Orch,
        patch.object(activities._js, "clear_completed_phases") as mock_clear,
    ):
        Orch.return_value.compensate = MagicMock()
        Orch.return_value.environment_store.get.return_value = None
        Orch.return_value.environment_store.readable.return_value = True
        Orch.return_value.tool_agents.get.return_value.verify_and_remove_orphan.return_value = True
        Orch.return_value.tool_agents.get.return_value._container_exists.return_value = False
        activities.compensate_activity("a1", [], job_id="j-comp")

    mock_clear.assert_called_once_with("j-comp")


def test_compensate_activity_raises_when_docker_state_survives() -> None:
    """compensate() never raises even when its own docker teardown fails — this
    activity must verify teardown independently and raise itself, or Temporal
    considers compensation successful and never retries a leaked container."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = True
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.return_value = False
    fake_orch.tool_agents.get.return_value._container_exists.return_value = False
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases") as mock_clear,
    ):
        with pytest.raises(RuntimeError, match="did not complete"):
            activities.compensate_activity("a1", [], job_id="j-comp")

    # Checkpoints are cleared BEFORE this verification step, specifically so
    # a raise here (this step alone failing) cannot leave stale, resumable
    # checkpoints over state compensate() may have already torn down.
    mock_clear.assert_called_once_with("j-comp")


def test_compensate_activity_skips_docker_verification_when_env_record_survives() -> None:
    """A live EnvironmentStore record means the container may still be legitimately owned.

    Checked fresh (not inferred from tear_down_environment or from whether
    compensate() happened to raise internally): a genuinely pre-existing
    environment leaves its record untouched by compensate() either way, and
    a record whose removal itself failed inside compensate() also still has
    one. Either way, an independent by-name probe can't distinguish "an
    orphan with no state row" from "the container this surviving record
    still legitimately references" — so it must be skipped entirely rather
    than risk deleting the latter.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = MagicMock()  # a record exists
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases") as mock_clear,
    ):
        activities.compensate_activity("a1", [], job_id="j-comp", tear_down_environment=True)

    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_not_called()
    mock_clear.assert_called_once_with("j-comp")


def test_compensate_activity_skips_docker_verification_when_registry_unreadable() -> None:
    """An unreadable registry location is not proof no record exists — stays conservative."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = False
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases"),
    ):
        activities.compensate_activity("a1", [], job_id="j-comp", tear_down_environment=True)

    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_not_called()


def test_compensate_activity_reclaims_self_leaked_container_by_job_id_label() -> None:
    """No EnvironmentStore record, and DockerProvisionerTool.is_pre_existing
    says the container is NOT pre-existing (e.g. its khala.job_id label
    matches this run's own job_id, or it's confirmed absent) -- this is
    unambiguously this attempt's own leak (setup created it, failed before
    registration completed, and its own local rollback also failed to
    remove it) or nothing to protect at all. Either way, verification must
    proceed to verify_and_remove_orphan, not treat it as "possibly
    pre-existing" the way is_pre_existing()=True would.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = True
    fake_orch.tool_agents.get.return_value.is_pre_existing.return_value = False
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.return_value = True
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases"),
    ):
        activities.compensate_activity("a1", [], job_id="j-comp", tear_down_environment=False)

    fake_orch.tool_agents.get.return_value.is_pre_existing.assert_called_once_with("a1", "j-comp")
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_called_once_with("a1")


def test_compensate_activity_skips_orphan_probe_when_deterministic_container_still_exists() -> None:
    """No EnvironmentStore record, but DockerProvisionerTool.is_pre_existing
    says the container might still predate this run (e.g. no label, or a
    different job's label) -- an absent record alone must not be treated as
    proof the container is an orphan, so verify_and_remove_orphan must not
    run.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = True
    fake_orch.tool_agents.get.return_value.is_pre_existing.return_value = True
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases"),
    ):
        activities.compensate_activity("a1", [], job_id="j-comp", tear_down_environment=False)

    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_not_called()


def test_compensate_activity_still_consults_is_pre_existing_without_job_id() -> None:
    """job_id omitted (None) is passed straight through to is_pre_existing --
    which itself falls back to its own conservative "protect" default for a
    missing job_id -- rather than compensate_activity trying to special-case
    that decision itself.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = True
    fake_orch.tool_agents.get.return_value.is_pre_existing.return_value = True
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases"),
    ):
        activities.compensate_activity("a1", [], job_id=None, tear_down_environment=False)

    fake_orch.tool_agents.get.return_value.is_pre_existing.assert_called_once_with("a1", None)
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_not_called()


def test_compensate_activity_raises_when_tear_down_environment_true_and_container_survives() -> (
    None
):
    """When ownership is already settled (tear_down_environment=True), a
    surviving container with no EnvironmentStore record must be treated as
    this run's own leaked orphan — not protected by the ownership check that
    exists only to cover the genuinely-ambiguous tear_down_environment=False
    case.

    Regression guard: the check used to run unconditionally, so a container
    that was still alive after both local rollback and orchestrator-level
    compensate() failed to remove it would flip record_may_exist to True
    here, suppressing verify_and_remove_orphan and letting
    compensate_activity return successfully — masking a real leak instead of
    raising for Temporal to retry.
    """
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.environment_store.get.return_value = None
    fake_orch.environment_store.readable.return_value = True
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.return_value = False
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases"),
    ):
        with pytest.raises(RuntimeError, match="did not complete"):
            activities.compensate_activity("a1", [], job_id="j-comp", tear_down_environment=True)

    # is_pre_existing must never run when ownership is already settled —
    # only verify_and_remove_orphan decides the outcome.
    fake_orch.tool_agents.get.return_value.is_pre_existing.assert_not_called()
    fake_orch.tool_agents.get.return_value.verify_and_remove_orphan.assert_called_once_with("a1")


def test_compensate_activity_skips_verification_without_docker_provisioner() -> None:
    """No docker_provisioner registered (e.g. a non-docker tool manifest) must
    not spuriously raise — there is nothing to verify."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.tool_agents = {}
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch.object(activities._js, "clear_completed_phases") as mock_clear,
    ):
        activities.compensate_activity("a1", [], job_id="j-comp")

    mock_clear.assert_called_once_with("j-comp")


def test_credentials_activity_migrates_legacy_plaintext_and_redacts_checkpoint() -> None:
    from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials
    from agent_team_studio.agent_provisioning_team.temporal import activities

    legacy = {
        "pg": GeneratedCredentials(tool_name="pg", username="u", password="legacy-secret"),
    }
    store = MagicMock()
    with (
        patch.object(activities, "_best_effort_job_store"),
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.get_stored_credentials",
            return_value={},
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.CredentialStore",
            return_value=store,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        prior = {
            "success": True,
            "tool_names": ["pg"],
            "credentials": {k: v.model_dump() for k, v in legacy.items()},
        }
        payload = activities.credentials_activity("j", "a", "default.yaml", prior_credentials=prior)

    assert payload["credentials"]["pg"]["password"] == "legacy-secret"
    store.store_credentials.assert_called_once()
    mock_phase.assert_called_once_with(
        "j",
        "credential_generation",
        {"success": True, "tool_names": ["pg"], "credentials": {}},
    )


def test_mark_job_failed_activity_writes_job_store() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with patch.object(activities._js, "mark_job_failed") as mock_fail:
        activities.mark_job_failed_activity("job-1", "boom")

    mock_fail.assert_called_once_with("job-1", error="boom")


def test_mark_job_failed_activity_raises_when_job_store_fails() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with patch.object(
        activities._js,
        "mark_job_failed",
        side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            activities.mark_job_failed_activity("job-1", "boom")


def test_mark_job_failed_activity_rejects_empty_inputs() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with pytest.raises(AssertionError):
        activities.mark_job_failed_activity("", "err")
    with pytest.raises(AssertionError):
        activities.mark_job_failed_activity("job-1", "")


def test_record_account_provisioning_rejects_stale_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def check_fencing_token(self, agent_id, token):
            raise StaleFencingTokenError(agent_id, "agent_lock", token, token + 1)

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch.object(activities._js, "add_completed_phase") as mock_phase,
    ):
        with pytest.raises(StaleFencingTokenError):
            activities.record_account_provisioning_activity(
                "job-1", [], agent_id="agent-1", fencing_token=1
            )

    mock_phase.assert_not_called()


def test_record_account_provisioning_sets_tool_counts() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    results = [
        {"tool_name": "postgresql", "success": True},
        {"tool_name": "redis", "success": True},
    ]
    sanitized = [
        {
            "tool_name": "postgresql",
            "success": True,
            "permissions": [],
            "error": None,
            "provisioner_key": None,
            "credentials": None,
            "details": {},
        },
        {
            "tool_name": "redis",
            "success": True,
            "permissions": [],
            "error": None,
            "provisioner_key": None,
            "credentials": None,
            "details": {},
        },
    ]
    fake_env = MagicMock()
    with (
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch.object(activities._js, "update_job") as mock_update,
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=fake_env,
        ),
    ):
        out = activities.record_account_provisioning_activity("job-1", results, "agent-1")

    assert out == {"success": True, "tool_results": sanitized}
    mock_phase.assert_called_once_with(
        "job-1",
        "account_provisioning",
        {"success": True, "tool_results": sanitized},
    )
    mock_update.assert_called_once_with(
        "job-1",
        progress=60,
        status_text="Account provisioning complete",
        current_tool=None,
        tools_completed=2,
        tools_total=2,
    )
    fake_env.add_tools.assert_called_once_with(
        "agent-1", ["postgresql", "redis"], fencing_token=None
    )


def test_record_account_provisioning_strips_plaintext_secrets() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    results = [
        {
            "tool_name": "postgresql",
            "success": True,
            "provisioner_key": "postgres_provisioner",
            "permissions": ["read"],
            "credentials": {
                "tool_name": "postgresql",
                "username": "u",
                "password": "s3cret",
                "connection_string": "postgres://u:s3cret@host/db",
            },
            "details": {
                "connection_string": "postgres://u:s3cret@host/db",
                "db_name": "ok",
            },
        }
    ]
    with (
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch.object(activities._js, "update_job"),
    ):
        out = activities.record_account_provisioning_activity("job-1", results)

    stored = mock_phase.call_args.args[2]["tool_results"][0]
    assert stored["credentials"] is None
    assert stored["details"]["connection_string"] == "***"
    assert stored["details"]["db_name"] == "ok"
    assert stored["provisioner_key"] == "postgres_provisioner"
    assert "s3cret" not in str(out)


def test_record_account_provisioning_persists_enriched_credentials(tmp_path: Path) -> None:
    """Enriched fields survive in CredentialStore after the sanitized checkpoint."""
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        get_stored_credentials,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.temporal import activities

    results = [
        {
            "tool_name": "postgresql",
            "success": True,
            "provisioner_key": "postgres_provisioner",
            "permissions": [],
            "credentials": {
                "tool_name": "postgresql",
                "username": "u",
                "password": "p",
                "connection_string": "postgres://u:p@host/db",
                "extra": {"db": "app"},
            },
            "details": {},
        }
    ]
    store = CredentialStore(storage_dir=tmp_path)
    with (
        patch.object(activities._js, "add_completed_phase") as mock_phase,
        patch.object(activities._js, "update_job"),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore"
        ) as mock_env_cls,
        patch(
            "agent_team_studio.agent_provisioning_team.phases.credential_generation.CredentialStore",
            return_value=store,
        ),
    ):
        mock_env_cls.return_value = MagicMock()
        out = activities.record_account_provisioning_activity("job-1", results, "agent-1")

    assert out["tool_results"][0]["credentials"] is None
    assert mock_phase.call_args.args[2]["tool_results"][0]["credentials"] is None
    restored = get_stored_credentials("agent-1", credential_store=store)
    assert restored["postgresql"].connection_string == "postgres://u:p@host/db"
    assert restored["postgresql"].extra == {"db": "app"}


def test_record_account_provisioning_raises_when_job_store_fails() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    results = [{"tool_name": "postgresql", "success": True}]
    with patch.object(
        activities._js,
        "add_completed_phase",
        side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            activities.record_account_provisioning_activity("job-1", results, "agent-1")


def test_list_manifest_tools_activity_returns_ordered_names(tmp_path) -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

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
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with pytest.raises(AssertionError):
        activities.list_manifest_tools_activity("")


# ---------------------------------------------------------------------------
# check_existing_environment_activity
# ---------------------------------------------------------------------------


def test_check_existing_environment_activity_true_when_running(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo,
        EnvironmentStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        EnvironmentInfo(
            agent_id="a1", container_id="c1", container_name="agent-a1", status="running"
        )
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=env_store,
        ),
        patch.object(DockerProvisionerTool, "_container_exists", return_value=True),
    ):
        assert t_acts.check_existing_environment_activity("a1") is True


def test_check_existing_environment_activity_delegates_false_to_is_pre_existing(
    tmp_path: Path,
) -> None:
    """No record at all — the record and its container are two
    independently-losable things (a prior compensation could have removed
    the record but not the container, or the record file could simply be
    lost to a disk issue), so this defers to DockerProvisionerTool's own
    label-aware ownership check rather than assuming absence just because
    EnvironmentStore has nothing.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=EnvironmentStore(storage_dir=tmp_path),
        ),
        patch.object(DockerProvisionerTool, "is_pre_existing", return_value=False) as mock_ipe,
    ):
        assert t_acts.check_existing_environment_activity("missing-agent", job_id="job-1") is False

    mock_ipe.assert_called_once_with("missing-agent", "job-1")


def test_check_existing_environment_activity_delegates_true_to_is_pre_existing(
    tmp_path: Path,
) -> None:
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=EnvironmentStore(storage_dir=tmp_path),
        ),
        patch.object(DockerProvisionerTool, "is_pre_existing", return_value=True) as mock_ipe,
    ):
        assert t_acts.check_existing_environment_activity("orphan-agent", job_id="job-1") is True

    mock_ipe.assert_called_once_with("orphan-agent", "job-1")


def test_check_existing_environment_activity_passes_none_job_id_when_omitted(
    tmp_path: Path,
) -> None:
    """job_id omitted (the pre-labeling-primitive call shape) must still reach
    is_pre_existing as an explicit None, not be silently dropped."""
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=EnvironmentStore(storage_dir=tmp_path),
        ),
        patch.object(DockerProvisionerTool, "is_pre_existing", return_value=True) as mock_ipe,
    ):
        assert t_acts.check_existing_environment_activity("a9") is True

    mock_ipe.assert_called_once_with("a9", None)


def test_check_existing_environment_activity_true_when_not_running(tmp_path: Path) -> None:
    """A non-running record (e.g. stopped) still counts as "existing" for this check.

    run_setup only fast-paths on status=="running", but docker.provision()'s
    own idempotency state (independent of EnvironmentStore) can still resolve
    to reusing that same container regardless of this record's status — so a
    "stopped" record must still be treated as pre-existing, or a later
    phase's failure could tear down a container that predates this run.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo,
        EnvironmentStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        EnvironmentInfo(
            agent_id="a2", container_id="c2", container_name="agent-a2", status="stopped"
        )
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=env_store,
        ),
        patch.object(DockerProvisionerTool, "_container_exists", return_value=True),
    ):
        assert t_acts.check_existing_environment_activity("a2") is True


def test_check_existing_environment_activity_false_when_record_stale_and_container_gone(
    tmp_path: Path,
) -> None:
    """A record whose backing container is CONFIRMED gone is stale, not pre-existing.

    A record can survive after its container is destroyed out-of-band (or
    the docker-level idempotency state is separately lost). run_setup would
    then create an entirely fresh container and overwrite the record — so
    trusting the stale record alone would misreport a brand-new container
    THIS run creates as "pre-existing", leaking it if a later phase fails
    (tear_down_environment=False would skip tearing it down).
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo,
        EnvironmentStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        EnvironmentInfo(
            agent_id="a2b", container_id="c-stale", container_name="agent-a2b", status="stopped"
        )
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=env_store,
        ),
        patch.object(DockerProvisionerTool, "_container_exists", return_value=False),
    ):
        assert t_acts.check_existing_environment_activity("a2b") is False


def test_check_existing_environment_activity_true_when_container_probe_inconclusive(
    tmp_path: Path,
) -> None:
    """A record whose container liveness can't be determined is treated as pre-existing.

    _container_exists returns None (daemon unreachable, probe timeout) when
    it can't tell — that is not proof the container is gone, so this must
    stay conservative rather than risk destroying a live one.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo,
        EnvironmentStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        EnvironmentInfo(
            agent_id="a2c", container_id="c-unknown", container_name="agent-a2c", status="stopped"
        )
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=env_store,
        ),
        patch.object(DockerProvisionerTool, "_container_exists", return_value=None),
    ):
        assert t_acts.check_existing_environment_activity("a2c") is True


def test_check_existing_environment_activity_true_when_ready(tmp_path: Path) -> None:
    """A delivered ("ready") environment must count as pre-existing too.

    phases/deliver.py moves a completed environment from "running" to
    "ready" — a check that only recognized "running" would report a fully
    delivered agent as nonexistent, letting a later provisioning attempt's
    failure destroy it via workflow-level compensation.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo,
        EnvironmentStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        EnvironmentInfo(agent_id="a3", container_id="c3", container_name="agent-a3", status="ready")
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=env_store,
        ),
        patch.object(DockerProvisionerTool, "_container_exists", return_value=True),
    ):
        assert t_acts.check_existing_environment_activity("a3") is True


def test_check_existing_environment_activity_true_when_registry_unreadable(
    tmp_path: Path,
) -> None:
    """An unreadable registry location must NOT be treated as confirmed absence.

    get() maps both "genuinely nothing here" and "something's here but we
    can't read it" to None — conflating those would let compensation destroy
    a healthy environment whose record simply can't be read right now.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    env_store = EnvironmentStore(storage_dir=tmp_path)
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.environment_store.EnvironmentStore",
            return_value=env_store,
        ),
        patch.object(env_store, "readable", return_value=False),
    ):
        assert t_acts.check_existing_environment_activity("a4") is True


def test_check_existing_environment_activity_rejects_empty_agent_id() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    with pytest.raises(AssertionError):
        t_acts.check_existing_environment_activity("")


# ---------------------------------------------------------------------------
# setup_activity — moved from test_workflows_unit
# ---------------------------------------------------------------------------


def test_setup_activity_progress_path() -> None:
    """Fresh setup writes progress / completed phase into job_store and returns env dump."""
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

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
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
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


def test_setup_activity_threads_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_setup_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1"),
    )
    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts._js, "add_completed_phase"),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            return_value=fake_setup_result,
        ) as mock_run_setup,
        patch("temporalio.activity.heartbeat"),
    ):
        t_acts.setup_activity("j", "a", "default.yaml", fencing_token=21)

    assert mock_run_setup.call_args.kwargs["fencing_token"] == 21


def test_setup_activity_passes_its_own_job_id_to_run_setup() -> None:
    """setup_activity must forward its own job_id into run_setup, or the
    khala.job_id label never reaches the real environment container -- the
    only code path check_existing_environment_activity/compensate_activity
    actually inspect.
    """
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_setup_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1"),
    )
    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts._js, "add_completed_phase"),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            return_value=fake_setup_result,
        ) as mock_run_setup,
        patch("temporalio.activity.heartbeat"),
    ):
        t_acts.setup_activity("job-77", "a", "default.yaml")

    _args, kwargs = mock_run_setup.call_args
    assert kwargs["job_id"] == "job-77"


def test_setup_activity_checkpoints_inside_run_setup_rollback_boundary() -> None:
    """The durable checkpoint write is passed to run_setup as on_registered.

    A fresh setup must run the job-store checkpoint write inside run_setup's
    own atomic rollback boundary (so a checkpoint failure tears the container
    back down) rather than after run_setup has already returned — this
    exercises that setup_activity actually wires the hook through and that a
    single checkpoint write happens (not a duplicate fallback write).
    """
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_env = EnvironmentInfo(container_id="c1", container_name="c1")
    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    captured_hook = {}

    def fake_run_setup(**kwargs):
        captured_hook["on_registered"] = kwargs["on_registered"]
        kwargs["on_registered"](fake_env)
        return SetupResult(success=True, environment=fake_env)

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts._js, "add_completed_phase") as mock_phase,
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            side_effect=fake_run_setup,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity("j", "a", "default.yaml")

    assert payload["success"] is True
    assert payload["environment"]["container_id"] == "c1"
    assert "on_registered" in captured_hook
    # Checkpointed inside the hook — the post-return fallback must not also fire.
    mock_phase.assert_called_once()
    assert mock_phase.call_args.args[0] == "j"
    assert mock_phase.call_args.args[1] == "setup"


def test_setup_activity_retry_fast_path_returns_prior_stronger_checkpoint() -> None:
    """A retry's reused=True fast path must not overwrite — or return — a
    weaker result when an earlier reused=False checkpoint already exists.

    If the first attempt of this activity creates a container and durably
    checkpoints via on_registered (reused=False), but Temporal loses that
    attempt's completion response and retries, the retry's fast path reuses
    the same container (reused=True) and does NOT call on_registered. The
    fallback write below must not blindly overwrite the earlier, stronger
    "this job created it" evidence already on record with this weaker one —
    a later resume reading phase_results would otherwise lose track of the
    fact that this job created the environment. The activity's own RETURN
    VALUE must also surface that earlier, stronger checkpoint rather than
    this retry's own weaker reused=True result: the workflow corrects a
    conservative pre_existing_environment from THIS activity's return value
    alone, never from job_store directly, so returning the weaker result
    here would silently drop the stronger evidence for the rest of the run.
    """
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    # Fast-path result: reused=True, no on_registered call — this retry's
    # own (weaker) outcome.
    reused_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1", reused=True),
    )
    # The earlier attempt's durable checkpoint: reused=False, proof this
    # job's own earlier try created the container fresh.
    prior_checkpoint = {
        "success": True,
        "environment": EnvironmentInfo(
            container_id="c1", container_name="c1", reused=False
        ).model_dump(),
    }

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts._js, "add_completed_phase") as mock_phase,
        patch.object(
            t_acts._js,
            "get_job",
            return_value={
                "completed_phases": ["setup"],
                "phase_results": {"setup": prior_checkpoint},
            },
        ),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            return_value=reused_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity("j", "a", "default.yaml")

    # The return value surfaces the earlier, STRONGER checkpoint — not this
    # retry's own weaker reused=True outcome — so the workflow's
    # pre_existing_environment correction still fires downstream.
    assert payload["environment"]["reused"] is False
    # The durable checkpoint (already recorded by an earlier attempt) is
    # still left untouched — not overwritten with this weaker evidence.
    mock_phase.assert_not_called()


def test_setup_activity_retry_fast_path_falls_back_without_checkpoint_payload() -> None:
    """A checkpointed phase with no recoverable payload falls back to this call's
    own result rather than returning nothing.

    completed_phases and phase_results are always written atomically together
    by add_completed_phase, so this is a defensive-only edge case — but the
    fast path must never surface a missing/malformed payload as if it were
    the stronger evidence.
    """
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    reused_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1", reused=True),
    )

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts._js, "add_completed_phase") as mock_phase,
        patch.object(
            t_acts._js,
            "get_job",
            # completed_phases says "setup" is checkpointed, but phase_results
            # has no recoverable entry for it.
            return_value={"completed_phases": ["setup"], "phase_results": {}},
        ),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            return_value=reused_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = t_acts.setup_activity("j", "a", "default.yaml")

    assert payload["environment"]["reused"] is True
    mock_phase.assert_not_called()


def test_setup_activity_fast_path_checkpoints_when_none_exists_yet() -> None:
    """A fast path with NO prior checkpoint still durably records its own result."""
    from agent_team_studio.agent_provisioning_team.models import EnvironmentInfo, SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    reused_result = SetupResult(
        success=True,
        environment=EnvironmentInfo(container_id="c1", container_name="c1", reused=True),
    )

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts._js, "add_completed_phase") as mock_phase,
        patch.object(t_acts._js, "get_job", return_value={"completed_phases": []}),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            return_value=reused_result,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        t_acts.setup_activity("j", "a", "default.yaml")

    mock_phase.assert_called_once_with("j", "setup", {"success": True, "environment": ANY})


def test_setup_activity_raises_when_setup_fails() -> None:
    """Failed setup raises RuntimeError so Temporal can retry the activity."""
    from agent_team_studio.agent_provisioning_team.models import SetupResult
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    fake_orch = MagicMock()
    fake_orch.environment_store = MagicMock()
    fake_orch.tool_agents = {"docker_provisioner": MagicMock()}
    fake_manifest = MagicMock()

    with (
        patch.object(t_acts, "_best_effort_job_store"),
        patch.object(t_acts, "_load_ctx", return_value=(fake_orch, fake_manifest)),
        patch(
            "agent_team_studio.agent_provisioning_team.phases.setup.run_setup",
            return_value=SetupResult(success=False, error="setup boom"),
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(RuntimeError, match="setup boom"):
            t_acts.setup_activity("j", "a", "default.yaml")


def test_setup_activity_rejects_stale_fencing_token() -> None:
    """A stale token is rejected before anything else runs, including the
    prior_setup restore path — a reclaimed lease has no legitimate claim to
    write any state for the agent_id, not just fresh Docker setup."""
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    class _FakeStore:
        def __init__(self, ttl_seconds=None):
            pass

        def check_fencing_token(self, agent_id, token):
            raise StaleFencingTokenError(agent_id, "agent_lock", token, token + 1)

    prior = {"success": True, "environment": None}
    with (
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore
        ),
        patch.object(t_acts, "_best_effort_job_store") as mock_job_store,
    ):
        with pytest.raises(StaleFencingTokenError):
            t_acts.setup_activity("j", "a", "default.yaml", prior_setup=prior, fencing_token=1)

    mock_job_store.assert_not_called()


def test_setup_activity_restores_from_prior() -> None:
    """When prior_setup is provided, setup is skipped and the snapshot is restored."""
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

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

    import agent_team_studio.agent_provisioning_team

    root = Path(agent_team_studio.agent_provisioning_team.__file__).resolve().parent
    forbidden = (
        "AgentProvisioningWorkflowV2",
        "_activity_v2",
        "PROVISION_THREAD_FALLBACK",
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


# -------------------------------------------------------------------------
# start_workflow._run_async success path and activities._load_ctx loading.
# -------------------------------------------------------------------------


def test_run_async_runs_via_loop() -> None:
    """Drive ``_run_async`` without a background event-loop thread (CI-safe)."""
    from concurrent.futures import Future

    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    fake_client = MagicMock()
    fake_loop = MagicMock()
    fut: Future = Future()
    fut.set_result("OK")
    # Use a closed coroutine placeholder — run_coroutine_threadsafe is mocked.
    coro = asyncio.sleep(0)

    with (
        patch.object(sw, "_await_client", return_value=(fake_client, fake_loop)),
        patch.object(sw.asyncio, "run_coroutine_threadsafe", return_value=fut) as mock_rcts,
    ):
        result = sw._run_async(coro)
    assert result == "OK"
    mock_rcts.assert_called_once()
    assert mock_rcts.call_args.args[0] is coro
    assert mock_rcts.call_args.args[1] is fake_loop
    coro.close()


def test_load_ctx_returns_orchestrator_and_manifest(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities as t_acts

    # Build a minimal manifest YAML on disk so load_manifest finds something.
    f = tmp_path / "m.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: pg
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
""",
        encoding="utf-8",
    )

    orch, manifest = t_acts._load_ctx(str(f))
    assert orch is not None
    assert manifest is not None
    assert manifest.tool_names == ["pg"]
