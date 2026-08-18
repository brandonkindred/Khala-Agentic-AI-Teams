"""start_issue_grooming_workflow dispatches IssueGroomingWorkflow with the API's job_id.

The helper is a thin wrapper over start_workflow_sync; the contract that matters
is that it forwards the workflow run ref, a payload carrying the caller's job_id
and GitHub issue context, the deterministic workflow id, and the coding-team
task queue (the queue IssueGroomingWorkflow is registered on alongside
CodingTeamWorkflow).
"""

from __future__ import annotations

import pytest

from software_engineering_team.temporal import issue_grooming_start_workflow as sw
from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE


def test_start_issue_grooming_workflow_forwards_run_payload_id_and_queue(monkeypatch):
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured["workflow_run"] = workflow_run
        captured["args"] = args
        captured["workflow_id"] = workflow_id
        captured["task_queue"] = task_queue

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_issue_grooming_workflow("job-7", "acme", "widgets", 42)

    assert captured["workflow_run"] is sw.IssueGroomingWorkflow.run
    (payload,) = captured["args"]
    assert payload == {"job_id": "job-7", "owner": "acme", "repo": "widgets", "issue_number": 42}
    assert captured["workflow_id"] == f"{sw.WORKFLOW_ID_PREFIX}job-7"
    assert captured["workflow_id"] == "issue-grooming-job-7"
    assert captured["task_queue"] == TASK_QUEUE


def test_start_issue_grooming_workflow_requires_job_id(monkeypatch):
    monkeypatch.setattr(sw, "start_workflow_sync", lambda *a, **k: None)
    with pytest.raises(AssertionError, match="non-empty job_id"):
        sw.start_issue_grooming_workflow("", "acme", "widgets", 42)
