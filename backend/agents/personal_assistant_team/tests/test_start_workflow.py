"""The team wrapper forwards to the shared ``start_workflow_sync`` bridge."""

from __future__ import annotations

import pytest


def test_start_assistant_workflow_delegates_to_shared_bridge(monkeypatch):
    from personal_assistant_team.temporal import PaAssistantWorkflow
    from personal_assistant_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_assistant_workflow("job-42", "user-9", "check my inbox", {"k": "v"})

    assert captured["workflow_run"] is PaAssistantWorkflow.run
    assert captured["args"] == ("job-42", "user-9", "check my inbox", {"k": "v"})
    assert captured["workflow_id"] == "pa-assistant-job-42"
    assert captured["task_queue"] == "personal-assistant"


def test_start_assistant_workflow_defaults_context_to_empty_dict(monkeypatch):
    from personal_assistant_team.temporal import start_workflow as sw

    captured: dict = {}
    monkeypatch.setattr(
        sw,
        "start_workflow_sync",
        lambda workflow_run, *args, workflow_id, task_queue, **_kw: captured.update(args=args),
    )

    sw.start_assistant_workflow("job-1", "user-1", "hi", None)

    assert captured["args"] == ("job-1", "user-1", "hi", {})


def test_start_assistant_workflow_propagates_runtime_error(monkeypatch):
    # start_assistant_workflow's own docstring documents that a RuntimeError
    # from start_workflow_sync (worker client never becomes available) is not
    # swallowed — it must propagate to the caller (the API endpoint, which has
    # its own handling for a failed dispatch).
    from personal_assistant_team.temporal import start_workflow as sw

    def _boom(workflow_run, *args, workflow_id, task_queue, **_kw):
        raise RuntimeError("Temporal worker client never became available")

    monkeypatch.setattr(sw, "start_workflow_sync", _boom)

    with pytest.raises(RuntimeError, match="worker client never became available"):
        sw.start_assistant_workflow("job-1", "user-1", "hi", {})
