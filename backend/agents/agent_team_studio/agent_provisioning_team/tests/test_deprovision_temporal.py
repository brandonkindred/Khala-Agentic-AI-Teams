"""Unit tests for the durable deprovision path.

Covers the ``deprovision_activity``, ``AgentDeprovisioningWorkflow``, the
``run_deprovision_workflow`` dispatch helper, and the ``deprovision_agent``
endpoint (Temporal-only: HTTP 503 when Temporal is disabled). No live
Temporal server is needed — we stub at the ``temporalio``/dispatch boundary.
"""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_team_studio.agent_provisioning_team.models import (
    DeprovisionCancelledError,
    DeprovisionResponse,
)


@pytest.fixture(autouse=True)
def _no_open_pre_patch_executions():
    """Default the rollout drain gate to "nothing open" for every test here.

    Without this, ``deprovision_agent`` would call the real
    ``find_open_pre_patch_executions``, which blocks on a Temporal client/loop
    that doesn't exist in this unit-test module.
    """
    from agent_team_studio.agent_provisioning_team.api import main

    with patch.object(main, "find_open_pre_patch_executions", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def _patched_true(monkeypatch):
    """Default every test to the post-lock-deploy replay branch.

    ``workflow.patched(...)`` needs a real workflow event loop; direct
    ``.run()`` calls here have none, so it must be stubbed like
    ``execute_activity``/``info``. ``True`` matches a fresh (non-replayed)
    execution — what nearly every test in this module wants — mirroring
    ``test_workflows_unit.py``'s identical fixture. Only tests that call
    ``AgentDeprovisioningWorkflow.run()`` directly touch ``workflow.patched``
    at all; this is a harmless no-op for the rest.
    """
    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf

    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: True)


async def _never_completing_sleep(_delta):
    """Stand-in for ``workflow.sleep`` that never resolves on its own.

    Keeps ``AgentDeprovisioningWorkflow._await_deprovision``'s handle-vs-timer
    race resolving on the activity handle in tests that aren't exercising the
    soft-timeout path — this timer task is simply cancelled, never awaited to
    completion, once the handle wins.
    """
    await asyncio.Future()


def _fake_activity_handle(result=None, *, error=None):
    """A real ``asyncio.Task`` standing in for a Temporal ``ActivityHandle``.

    ``asyncio.wait()`` requires genuine ``Future``/``Task`` semantics (done
    callbacks, ``.result()``, ``.cancel()``); wrapping a same-turn-resolving
    coroutine in a real task gives that without a live Temporal event loop.
    """

    async def _coro():
        if error is not None:
            raise error
        return result

    return asyncio.ensure_future(_coro())


# ---------------------------------------------------------------------------
# deprovision_activity
# ---------------------------------------------------------------------------


def test_deprovision_activity_calls_orchestrator() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.deprovision.return_value = DeprovisionResponse(
        agent_id="a", success=True, details={"tools": {"pg": True}}, error=None
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.deprovision_activity("a", force=True)

    fake_orch.deprovision.assert_called_once_with(
        "a", force=True, cancellation_checkpoint=ANY, fencing_token=None
    )
    assert payload["agent_id"] == "a"
    assert payload["success"] is True
    assert payload["details"] == {"tools": {"pg": True}}


def test_deprovision_activity_threads_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.deprovision.return_value = DeprovisionResponse(
        agent_id="a", success=True, details={}, error=None
    )

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        activities.deprovision_activity("a", force=False, fencing_token=8)

    fake_orch.deprovision.assert_called_once_with(
        "a", force=False, cancellation_checkpoint=ANY, fencing_token=8
    )


def test_deprovision_activity_rejects_blank_agent() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import activities

    with patch("temporalio.activity.heartbeat"):
        with pytest.raises(AssertionError):
            activities.deprovision_activity("")


def test_deprovision_activity_rejects_stale_fencing_token() -> None:
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
        patch("temporalio.activity.heartbeat"),
    ):
        with pytest.raises(StaleFencingTokenError):
            activities.deprovision_activity("a", fencing_token=1)

    fake_orch_cls.assert_not_called()


def test_deprovision_activity_checkpoint_heartbeats_and_checks_cancellation() -> None:
    """The checkpoint passed into the orchestrator heartbeats and polls
    ``activity.is_cancelled()``; when it signals cancellation, the resulting
    ``DeprovisionCancelledError`` propagates out of the activity uncaught
    rather than being swallowed into a soft-failure response."""
    from agent_team_studio.agent_provisioning_team.temporal import activities

    captured_checkpoint = {}

    def fake_deprovision(agent_id, force=False, cancellation_checkpoint=None, fencing_token=None):
        captured_checkpoint["fn"] = cancellation_checkpoint
        raise DeprovisionCancelledError(agent_id, {"tools": {}})

    fake_orch = MagicMock()
    fake_orch.deprovision.side_effect = fake_deprovision

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch("temporalio.activity.heartbeat") as fake_heartbeat,
        patch("temporalio.activity.is_cancelled", return_value=True) as fake_is_cancelled,
    ):
        with pytest.raises(DeprovisionCancelledError):
            activities.deprovision_activity("a")

        # Exercise the captured checkpoint directly to confirm it heartbeats
        # and defers to activity.is_cancelled() for the cancellation signal.
        assert captured_checkpoint["fn"]() is True
        assert fake_heartbeat.call_count >= 2
        fake_is_cancelled.assert_called()


# ---------------------------------------------------------------------------
# AgentDeprovisioningWorkflow — direct .run() invocation with a stubbed activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprovision_workflow_returns_activity_result() -> None:
    from types import SimpleNamespace

    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf
    from agent_team_studio.agent_provisioning_team.temporal.workflows import PHASE_TIMEOUT

    calls: list[dict] = []

    async def fake_exec(activity_fn, *args, **kwargs):
        name = getattr(activity_fn, "__name__", str(activity_fn))
        calls.append({"name": name, "args": kwargs.get("args")})
        return None

    def fake_start(activity_fn, *args, **kwargs):
        name = getattr(activity_fn, "__name__", str(activity_fn))
        calls.append(
            {
                "name": name,
                "args": kwargs.get("args"),
                "schedule_to_close_timeout": kwargs.get("schedule_to_close_timeout"),
                "retry_policy": kwargs.get("retry_policy"),
            }
        )
        return _fake_activity_handle(
            {"agent_id": "a", "success": True, "details": {}, "error": None}
        )

    fake_info = SimpleNamespace(workflow_id="agent-provisioning-deprovision-a-xyz")

    with (
        patch.object(wf.workflow, "execute_activity", new=fake_exec),
        patch.object(wf.workflow, "start_activity", new=fake_start),
        patch.object(wf.workflow, "sleep", new=_never_completing_sleep),
        patch.object(wf.workflow, "info", return_value=fake_info),
    ):
        result = await wf.AgentDeprovisioningWorkflow().run("a", True)

    assert result["success"] is True
    # Acquire the lock, run deprovision, release the lock — in that order.
    assert [c["name"] for c in calls] == [
        "acquire_agent_lock_activity",
        "deprovision_activity",
        "release_agent_lock_activity",
    ]
    captured = next(c for c in calls if c["name"] == "deprovision_activity")
    assert captured["args"] == ["a", True, None]
    assert captured["schedule_to_close_timeout"] == PHASE_TIMEOUT
    assert captured["retry_policy"] is wf.DEFAULT_RETRY_POLICY


@pytest.mark.asyncio
async def test_deprovision_workflow_rejects_blank_agent_id() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import workflows as wf

    with pytest.raises(AssertionError):
        await wf.AgentDeprovisioningWorkflow().run("")


# ---------------------------------------------------------------------------
# run_deprovision_workflow dispatch
# ---------------------------------------------------------------------------


def test_run_deprovision_workflow_executes_and_returns() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    sentinel = {"agent_id": "a", "success": True, "details": {}, "error": None}
    captured: dict = {}

    def fake_execute(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured["workflow_id"] = workflow_id
        captured["task_queue"] = task_queue
        captured["args"] = args
        return sentinel

    with patch.object(sw, "execute_workflow_sync", side_effect=fake_execute):
        out = sw.run_deprovision_workflow("agent-1", force=True)

    assert out is sentinel
    # A fresh, unique id under the deprovision prefix.
    assert captured["workflow_id"].startswith("agent-provisioning-deprovision-agent-1-")
    assert captured["task_queue"] == sw.TASK_QUEUE
    assert captured["args"] == ("agent-1", True)


def test_run_deprovision_workflow_rejects_blank_agent() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    with pytest.raises(AssertionError):
        sw.run_deprovision_workflow("")


def test_run_deprovision_workflow_ids_are_unique() -> None:
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw

    ids: list[str] = []

    def fake_execute(workflow_run, *args, workflow_id, task_queue, **kwargs):
        ids.append(workflow_id)
        return {}

    with patch.object(sw, "execute_workflow_sync", side_effect=fake_execute):
        sw.run_deprovision_workflow("a")
        sw.run_deprovision_workflow("a")

    assert ids[0] != ids[1]


def test_run_deprovision_workflow_uses_client_timeout_exceeding_phase_timeout() -> None:
    """The client wait must exceed AgentDeprovisioningWorkflow's
    schedule_to_close_timeout (PHASE_TIMEOUT, 20 minutes — which already caps
    the total time across DEFAULT_RETRY_POLICY's retries), or a legitimately
    slow-but-successful deprovision is mistaken for a hung one."""
    from agent_team_studio.agent_provisioning_team.temporal import start_workflow as sw
    from agent_team_studio.agent_provisioning_team.temporal.constants import (
        DEPROVISION_CLIENT_TIMEOUT_S,
    )
    from agent_team_studio.agent_provisioning_team.temporal.workflows import PHASE_TIMEOUT

    captured: dict = {}

    def fake_execute(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured.update(kwargs)
        return {}

    with patch.object(sw, "execute_workflow_sync", side_effect=fake_execute):
        sw.run_deprovision_workflow("a")

    from agent_team_studio.agent_provisioning_team.temporal.constants import CLIENT_TIMEOUT_MARGIN_S

    assert captured["execute_timeout_s"] == DEPROVISION_CLIENT_TIMEOUT_S
    assert DEPROVISION_CLIENT_TIMEOUT_S >= PHASE_TIMEOUT.total_seconds() + CLIENT_TIMEOUT_MARGIN_S
    assert CLIENT_TIMEOUT_MARGIN_S >= 60


# ---------------------------------------------------------------------------
# deprovision_agent endpoint branch
# ---------------------------------------------------------------------------


def test_require_deprovision_runner_raises_503_when_temporal_disabled() -> None:
    from agent_team_studio.agent_provisioning_team.api import main

    with patch("shared.temporal.client.is_temporal_enabled", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            main._require_deprovision_runner()
    assert exc_info.value.status_code == 503
    assert "Temporal" in exc_info.value.detail


def test_require_deprovision_runner_returns_callable_when_enabled() -> None:
    from agent_team_studio.agent_provisioning_team.api import main

    with patch("shared.temporal.client.is_temporal_enabled", return_value=True):
        runner = main._require_deprovision_runner()
    assert callable(runner)


def test_deprovision_agent_uses_temporal_when_enabled() -> None:
    from agent_team_studio.agent_provisioning_team.api import main

    dump = {"agent_id": "a", "success": True, "details": {}, "error": None}
    fake_runner = MagicMock(return_value=dump)

    with patch.object(main, "_require_deprovision_runner", return_value=fake_runner):
        resp = main.deprovision_agent("a", force=False)

    fake_runner.assert_called_once_with("a", False)
    assert isinstance(resp, DeprovisionResponse)
    assert resp.success is True


def test_deprovision_agent_returns_503_when_temporal_client_unavailable() -> None:
    """Client/loop not ready is a 503 (Temporal required), not success=False."""
    from agent_team_studio.agent_provisioning_team.api import main

    fake_runner = MagicMock(side_effect=RuntimeError("Temporal client not available"))

    with patch.object(main, "_require_deprovision_runner", return_value=fake_runner):
        with pytest.raises(HTTPException) as ei:
            main.deprovision_agent("a", force=True)

    assert ei.value.status_code == 503
    assert "Temporal" in str(ei.value.detail)


def test_deprovision_agent_degrades_gracefully_on_workflow_failure() -> None:
    """After Temporal is available, workflow/application failures return
    ``DeprovisionResponse(success=False)`` rather than an unhandled 500."""
    from agent_team_studio.agent_provisioning_team.api import main

    fake_runner = MagicMock(side_effect=RuntimeError("workflow crashed mid-run"))

    with patch.object(main, "_require_deprovision_runner", return_value=fake_runner):
        resp = main.deprovision_agent("a", force=True)

    fake_runner.assert_called_once_with("a", True)
    assert isinstance(resp, DeprovisionResponse)
    assert resp.agent_id == "a"
    assert resp.success is False
    assert "workflow crashed mid-run" in resp.error


def test_deprovision_agent_handles_invalid_workflow_payload() -> None:
    """Malformed workflow dicts become success=False instead of an unhandled 500."""
    from agent_team_studio.agent_provisioning_team.api import main

    fake_runner = MagicMock(return_value={"unexpected": True})

    with patch.object(main, "_require_deprovision_runner", return_value=fake_runner):
        resp = main.deprovision_agent("a", force=False)

    assert isinstance(resp, DeprovisionResponse)
    assert resp.agent_id == "a"
    assert resp.success is False
    assert "Invalid deprovision workflow response" in (resp.error or "")


def test_deprovision_agent_endpoint_returns_503_when_temporal_disabled() -> None:
    """DELETE /environments/{agent_id} is Temporal-only: 503, not an
    in-process orchestrator fallback, when Temporal is disabled."""
    from agent_team_studio.agent_provisioning_team.api.main import app

    client = TestClient(app)
    with patch("shared.temporal.client.is_temporal_enabled", return_value=False):
        resp = client.delete("/environments/agent-1")

    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]
