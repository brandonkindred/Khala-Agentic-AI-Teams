"""start_run_team_workflow forwards sprint_id on both the V2 (RunTeamWorkflowV2, the
default) and V1 (RunTeamWorkflow, opt-out-only) dispatch paths — both workflows accept
it as their trailing positional arg.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from software_engineering_team.temporal import start_workflow as sw
from software_engineering_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX_RUN_TEAM
from software_engineering_team.temporal.workflows import RunTeamWorkflow, RunTeamWorkflowV2


def _patch_client(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(sw, "get_temporal_client", lambda: fake_client)
    monkeypatch.setattr(sw, "_run_async", lambda coro: None)
    return fake_client


def test_start_run_team_workflow_forwards_sprint_id(monkeypatch):
    """With SE_WORKFLOW_V2 unset, RunTeamWorkflowV2 is selected by default."""
    fake_client = _patch_client(monkeypatch)
    monkeypatch.delenv("SE_WORKFLOW_V2", raising=False)

    sw.start_run_team_workflow("job-1", "/repo", sprint_id="sprint-123")

    fake_client.start_workflow.assert_called_once()
    call = fake_client.start_workflow.call_args
    assert call.args[0] is RunTeamWorkflowV2.run
    assert call.kwargs["args"][-1] == "sprint-123"
    assert call.kwargs["id"] == f"{WORKFLOW_ID_PREFIX_RUN_TEAM}job-1"
    assert call.kwargs["task_queue"] == TASK_QUEUE


def test_start_run_team_workflow_defaults_sprint_id_to_none(monkeypatch):
    fake_client = _patch_client(monkeypatch)

    sw.start_run_team_workflow("job-2", "/repo")

    call = fake_client.start_workflow.call_args
    assert call.kwargs["args"][-1] is None


def test_start_run_team_workflow_v2_also_receives_sprint_id(monkeypatch):
    fake_client = _patch_client(monkeypatch)
    monkeypatch.setenv("SE_WORKFLOW_V2", "true")

    sw.start_run_team_workflow("job-3", "/repo", sprint_id="sprint-123")

    call = fake_client.start_workflow.call_args
    assert call.args[0] is RunTeamWorkflowV2.run
    assert call.kwargs["args"][-1] == "sprint-123"


def test_start_run_team_workflow_v1_when_explicitly_disabled(monkeypatch):
    """SE_WORKFLOW_V2 set to a falsy value keeps V1 startable for draining
    in-flight/legacy jobs."""
    fake_client = _patch_client(monkeypatch)
    monkeypatch.setenv("SE_WORKFLOW_V2", "false")

    sw.start_run_team_workflow("job-4", "/repo", sprint_id="sprint-123")

    call = fake_client.start_workflow.call_args
    assert call.args[0] is RunTeamWorkflow.run
    assert call.kwargs["args"][-1] == "sprint-123"


def test_is_workflow_v2_enabled_defaults_true_when_unset(monkeypatch):
    monkeypatch.delenv("SE_WORKFLOW_V2", raising=False)
    assert sw.is_workflow_v2_enabled() is True


def test_is_workflow_v2_enabled_true_for_blank(monkeypatch):
    monkeypatch.setenv("SE_WORKFLOW_V2", "")
    assert sw.is_workflow_v2_enabled() is True


def test_is_workflow_v2_enabled_true_for_garbage_or_truthy(monkeypatch):
    for value in ("true", "1", "yes", "garbage"):
        monkeypatch.setenv("SE_WORKFLOW_V2", value)
        assert sw.is_workflow_v2_enabled() is True, value


def test_is_workflow_v2_enabled_false_for_recognized_falsy_values(monkeypatch):
    for value in ("0", "false", "no", "FALSE", "  false  ", "No"):
        monkeypatch.setenv("SE_WORKFLOW_V2", value)
        assert sw.is_workflow_v2_enabled() is False, value
