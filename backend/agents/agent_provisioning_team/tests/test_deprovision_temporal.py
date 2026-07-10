"""Unit tests for the durable deprovision path.

Covers the ``deprovision_activity``, ``AgentDeprovisioningWorkflow``, the
``run_deprovision_workflow`` dispatch helper, and the ``deprovision_agent``
endpoint's Temporal-vs-thread branch. No live Temporal server is needed — we
stub at the ``temporalio``/dispatch boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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

    captured: dict = {}

    async def fake_exec(activity_fn, *args, **kwargs):
        captured["name"] = getattr(activity_fn, "__name__", str(activity_fn))
        captured["args"] = kwargs.get("args")
        return {"agent_id": "a", "success": True, "details": {}, "error": None}

    with patch.object(wf.workflow, "execute_activity", new=fake_exec):
        result = await wf.AgentDeprovisioningWorkflow().run("a", True)

    assert result["success"] is True
    assert captured["name"] == "deprovision_activity"
    assert captured["args"] == ["a", True]


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


# ---------------------------------------------------------------------------
# deprovision_agent endpoint branch
# ---------------------------------------------------------------------------


def test_deprovision_starter_none_when_temporal_disabled() -> None:
    from agent_provisioning_team.api import main

    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False):
        assert main._deprovision_starter() is None


def test_deprovision_starter_none_with_thread_fallback(monkeypatch) -> None:
    from agent_provisioning_team.api import main

    monkeypatch.setenv("PROVISION_THREAD_FALLBACK", "true")
    assert main._deprovision_starter() is None


def test_deprovision_starter_returns_callable_when_enabled(monkeypatch) -> None:
    from agent_provisioning_team.api import main

    monkeypatch.delenv("PROVISION_THREAD_FALLBACK", raising=False)
    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=True):
        starter = main._deprovision_starter()
    assert callable(starter)


def test_deprovision_agent_uses_temporal_when_enabled() -> None:
    from agent_provisioning_team.api import main

    dump = {"agent_id": "a", "success": True, "details": {}, "error": None}
    fake_starter = MagicMock(return_value=dump)

    with patch.object(main, "_deprovision_starter", return_value=fake_starter):
        resp = main.deprovision_agent("a", force=False)

    fake_starter.assert_called_once_with("a", False)
    assert isinstance(resp, DeprovisionResponse)
    assert resp.success is True


def test_deprovision_agent_falls_back_to_orchestrator() -> None:
    from agent_provisioning_team.api import main

    fallback = DeprovisionResponse(agent_id="a", success=True, details={}, error=None)

    with (
        patch.object(main, "_deprovision_starter", return_value=None),
        patch.object(main.orchestrator, "deprovision", return_value=fallback) as mock_dep,
    ):
        resp = main.deprovision_agent("a", force=True)

    mock_dep.assert_called_once_with("a", force=True)
    assert resp is fallback
