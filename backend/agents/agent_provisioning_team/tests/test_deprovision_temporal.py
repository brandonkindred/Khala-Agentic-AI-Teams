"""Unit tests for the durable deprovision path.

Covers the ``deprovision_activity``, ``AgentDeprovisioningWorkflow``, the
``run_deprovision_workflow`` dispatch helper, and the ``deprovision_agent``
endpoint (Temporal-only: HTTP 503 when Temporal is disabled). No live
Temporal server is needed — we stub at the ``temporalio``/dispatch boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_provisioning_team.models import DeprovisionResponse

# ---------------------------------------------------------------------------
# deprovision_activity
# ---------------------------------------------------------------------------


def test_deprovision_activity_calls_orchestrator() -> None:
    from agent_provisioning_team.temporal import activities

    fake_orch = MagicMock()
    fake_orch.deprovision.return_value = DeprovisionResponse(
        agent_id="a", success=True, details={"tools": {"pg": True}}, error=None
    )

    with (
        patch(
            "agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch("temporalio.activity.heartbeat"),
    ):
        payload = activities.deprovision_activity("a", force=True)

    fake_orch.deprovision.assert_called_once_with("a", force=True)
    assert payload["agent_id"] == "a"
    assert payload["success"] is True
    assert payload["details"] == {"tools": {"pg": True}}


def test_deprovision_activity_rejects_blank_agent() -> None:
    from agent_provisioning_team.temporal import activities

    with patch("temporalio.activity.heartbeat"):
        with pytest.raises(AssertionError):
            activities.deprovision_activity("")


# ---------------------------------------------------------------------------
# AgentDeprovisioningWorkflow — direct .run() invocation with a stubbed activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprovision_workflow_returns_activity_result() -> None:
    from agent_provisioning_team.temporal import workflows as wf
    from agent_provisioning_team.temporal.workflows import PHASE_TIMEOUT

    captured: dict = {}

    async def fake_exec(activity_fn, *args, **kwargs):
        captured["name"] = getattr(activity_fn, "__name__", str(activity_fn))
        captured["args"] = kwargs.get("args")
        captured["schedule_to_close_timeout"] = kwargs.get("schedule_to_close_timeout")
        captured["retry_policy"] = kwargs.get("retry_policy")
        return {"agent_id": "a", "success": True, "details": {}, "error": None}

    with patch.object(wf.workflow, "execute_activity", new=fake_exec):
        result = await wf.AgentDeprovisioningWorkflow().run("a", True)

    assert result["success"] is True
    assert captured["name"] == "deprovision_activity"
    assert captured["args"] == ["a", True]
    assert captured["schedule_to_close_timeout"] == PHASE_TIMEOUT
    assert captured["retry_policy"] is wf.DEFAULT_RETRY_POLICY


@pytest.mark.asyncio
async def test_deprovision_workflow_rejects_blank_agent_id() -> None:
    from agent_provisioning_team.temporal import workflows as wf

    with pytest.raises(AssertionError):
        await wf.AgentDeprovisioningWorkflow().run("")


# ---------------------------------------------------------------------------
# run_deprovision_workflow dispatch
# ---------------------------------------------------------------------------


def test_run_deprovision_workflow_executes_and_returns() -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

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
    from agent_provisioning_team.temporal import start_workflow as sw

    with pytest.raises(AssertionError):
        sw.run_deprovision_workflow("")


def test_run_deprovision_workflow_ids_are_unique() -> None:
    from agent_provisioning_team.temporal import start_workflow as sw

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
    from agent_provisioning_team.temporal import start_workflow as sw
    from agent_provisioning_team.temporal.constants import DEPROVISION_CLIENT_TIMEOUT_S
    from agent_provisioning_team.temporal.workflows import PHASE_TIMEOUT

    captured: dict = {}

    def fake_execute(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured.update(kwargs)
        return {}

    with patch.object(sw, "execute_workflow_sync", side_effect=fake_execute):
        sw.run_deprovision_workflow("a")

    from agent_provisioning_team.temporal.constants import CLIENT_TIMEOUT_MARGIN_S

    assert captured["execute_timeout_s"] == DEPROVISION_CLIENT_TIMEOUT_S
    assert DEPROVISION_CLIENT_TIMEOUT_S >= PHASE_TIMEOUT.total_seconds() + CLIENT_TIMEOUT_MARGIN_S
    assert CLIENT_TIMEOUT_MARGIN_S >= 60


# ---------------------------------------------------------------------------
# deprovision_agent endpoint branch
# ---------------------------------------------------------------------------


def test_require_deprovision_runner_raises_503_when_temporal_disabled() -> None:
    from agent_provisioning_team.api import main

    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            main._require_deprovision_runner()
    assert exc_info.value.status_code == 503
    assert "Temporal" in exc_info.value.detail


def test_require_deprovision_runner_returns_callable_when_enabled() -> None:
    from agent_provisioning_team.api import main

    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True):
        runner = main._require_deprovision_runner()
    assert callable(runner)


def test_deprovision_agent_uses_temporal_when_enabled() -> None:
    from agent_provisioning_team.api import main

    dump = {"agent_id": "a", "success": True, "details": {}, "error": None}
    fake_runner = MagicMock(return_value=dump)

    with patch.object(main, "_require_deprovision_runner", return_value=fake_runner):
        resp = main.deprovision_agent("a", force=False)

    fake_runner.assert_called_once_with("a", False)
    assert isinstance(resp, DeprovisionResponse)
    assert resp.success is True


def test_deprovision_agent_returns_503_when_temporal_client_unavailable() -> None:
    """Client/loop not ready is a 503 (Temporal required), not success=False."""
    from agent_provisioning_team.api import main

    fake_runner = MagicMock(side_effect=RuntimeError("Temporal client not available"))

    with patch.object(main, "_require_deprovision_runner", return_value=fake_runner):
        with pytest.raises(HTTPException) as ei:
            main.deprovision_agent("a", force=True)

    assert ei.value.status_code == 503
    assert "Temporal" in str(ei.value.detail)


def test_deprovision_agent_degrades_gracefully_on_workflow_failure() -> None:
    """After Temporal is available, workflow/application failures return
    ``DeprovisionResponse(success=False)`` rather than an unhandled 500."""
    from agent_provisioning_team.api import main

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
    from agent_provisioning_team.api import main

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
    from agent_provisioning_team.api.main import app

    client = TestClient(app)
    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False):
        resp = client.delete("/environments/agent-1")

    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]
