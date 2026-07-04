"""start_coding_team_workflow dispatches CodingTeamWorkflow with the API's job_id.

The helper is a thin wrapper over start_workflow_sync; the contract that matters
is that it forwards the workflow run ref, a payload carrying the caller's job_id
(so the client polls the row the orchestrator writes), the deterministic
workflow id, and the coding-team task queue.
"""

from __future__ import annotations

import pytest

from coding_team.temporal import start_workflow as sw
from coding_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX


def test_start_coding_team_workflow_forwards_run_payload_id_and_queue(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["workflow_run"] = workflow_run
        captured["args"] = args
        captured["workflow_id"] = workflow_id
        captured["task_queue"] = task_queue

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    plan = {"objective": "ship it"}
    sw.start_coding_team_workflow("job-7", "/repo", plan)

    assert captured["workflow_run"] is sw.CodingTeamWorkflow.run
    (payload,) = captured["args"]
    assert payload == {"job_id": "job-7", "repo_path": "/repo", "plan_input": plan}
    assert captured["workflow_id"] == f"{WORKFLOW_ID_PREFIX}job-7"
    assert captured["workflow_id"] == "coding_team-job-7"
    assert captured["task_queue"] == TASK_QUEUE


def test_start_coding_team_workflow_requires_job_id(monkeypatch):
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(AssertionError, match="non-empty job_id"):
        sw.start_coding_team_workflow("", "/repo", {"objective": "x"})


def test_start_coding_team_workflow_requires_repo_path(monkeypatch):
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(AssertionError, match="non-empty repo_path"):
        sw.start_coding_team_workflow("job-7", "", {"objective": "x"})
