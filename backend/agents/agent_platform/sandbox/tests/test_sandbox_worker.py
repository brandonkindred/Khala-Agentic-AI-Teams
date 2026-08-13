"""Unit tests for platform sandbox Temporal worker and queue constants."""

from __future__ import annotations

import importlib
from unittest.mock import patch


def test_sandbox_task_queue_default_differs_from_provisioning_queue() -> None:
    from agent_platform.sandbox.temporal import constants as sb_const
    from agent_team_studio.agent_provisioning_team.temporal import constants as prov_const

    assert isinstance(sb_const.SANDBOX_TASK_QUEUE, str)
    assert sb_const.SANDBOX_TASK_QUEUE
    assert sb_const.SANDBOX_TASK_QUEUE != prov_const.TASK_QUEUE
    assert sb_const.SANDBOX_WORKFLOW_ID_PREFIX == "agent-provisioning-"
    assert sb_const.SANDBOX_REAPER_WORKFLOW_ID == "agent-provisioning-sandbox-idle-reaper"


def test_sandbox_task_queue_env_override(monkeypatch) -> None:
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", "custom-sandbox-queue")
    from agent_platform.sandbox.temporal import constants as constants_mod

    try:
        reloaded = importlib.reload(constants_mod)
        assert reloaded.SANDBOX_TASK_QUEUE == "custom-sandbox-queue"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", raising=False)
        importlib.reload(constants_mod)


def test_start_sandbox_worker_thread_returns_false_when_disabled() -> None:
    import shared.temporal
    from agent_platform.sandbox.temporal import worker as worker_mod

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=False),
        patch.object(shared.temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_agent_platform_sandbox_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_sandbox_worker_thread_uses_distinct_team_key_and_queue() -> None:
    import shared.temporal
    from agent_platform.sandbox.temporal import SANDBOX_ACTIVITIES, SANDBOX_WORKFLOWS
    from agent_platform.sandbox.temporal import worker as worker_mod
    from agent_platform.sandbox.temporal.constants import SANDBOX_TASK_QUEUE
    from agent_team_studio.agent_provisioning_team.temporal.constants import TASK_QUEUE

    with (
        patch.object(worker_mod, "is_temporal_enabled", return_value=True),
        patch.object(shared.temporal, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker_mod.start_agent_platform_sandbox_temporal_worker_thread() is True
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert args[0] == "agent_provisioning_sandbox"
        assert args[0] != "agent_provisioning"
        assert kwargs["task_queue"] == SANDBOX_TASK_QUEUE
        assert kwargs["task_queue"] != TASK_QUEUE
        assert args[1] == SANDBOX_WORKFLOWS
        assert args[2] == SANDBOX_ACTIVITIES
