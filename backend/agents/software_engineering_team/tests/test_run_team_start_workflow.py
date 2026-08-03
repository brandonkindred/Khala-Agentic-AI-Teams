"""start_run_team_workflow forwards sprint_id on both the V1 (RunTeamWorkflow) and
V2 (RunTeamWorkflowV2) dispatch paths — both workflows accept it as their trailing
positional arg.
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
    fake_client = _patch_client(monkeypatch)

    sw.start_run_team_workflow("job-1", "/repo", sprint_id="sprint-123")

    fake_client.start_workflow.assert_called_once()
    call = fake_client.start_workflow.call_args
    assert call.args[0] is RunTeamWorkflow.run
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
