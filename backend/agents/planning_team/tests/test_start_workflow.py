"""Unit tests for the Planning start-workflow dispatcher.

``start_planning_workflow`` is now a thin wrapper over the shared sync→async
bridge (``shared.temporal.start_workflow_sync``): it forwards ``PlanningWorkflow.run``
with the positional workflow args, the ``planning-<job_id>`` workflow id, and the
Planning task queue, after enforcing its blank-input preconditions. The bridge's
own client-ready wait / ``run_coroutine_threadsafe`` behavior is covered by
``shared.temporal``'s tests.
"""

import pytest


def test_start_planning_workflow_delegates_to_shared_bridge(monkeypatch):
    """The wrapper forwards to ``start_workflow_sync`` with the workflow run ref,
    positional args (job_id first), the planning workflow id, and task queue."""
    from planning_team.temporal import PlanningWorkflow
    from planning_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_planning_workflow("job-1", "/tmp/ws", "Acme", "brief", None, False, True)

    assert captured["workflow_run"] is PlanningWorkflow.run
    assert captured["args"] == ("job-1", "/tmp/ws", "Acme", "brief", None, False, True)
    assert captured["workflow_id"] == "planning-job-1"
    assert captured["task_queue"] == sw.TASK_QUEUE


def test_start_planning_workflow_propagates_bridge_error(monkeypatch):
    """A dispatch failure from the shared bridge (e.g. worker not connected)
    propagates to the caller so the API can mark the job failed + return 500."""
    from planning_team.temporal import start_workflow as sw

    def _boom(*a, **k):
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(sw, "start_workflow_sync", _boom)

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        sw.start_planning_workflow("job-1", "/tmp/ws", None, "brief", None, False, False)


@pytest.mark.parametrize("job_id,repo_path", [("", "/tmp/ws"), ("job-1", "")])
def test_start_planning_workflow_rejects_blank_preconditions(job_id, repo_path, monkeypatch):
    """DbC: blank job_id or repo_path is a caller bug and is rejected upfront
    (explicit ValueError, so it holds under ``python -O``), before any dispatch."""
    from planning_team.temporal import start_workflow as sw

    # Guard: the precondition check must fire before the bridge is ever called.
    monkeypatch.setattr(
        sw, "start_workflow_sync", lambda *a, **k: pytest.fail("dispatch should not run")
    )

    with pytest.raises(ValueError):
        sw.start_planning_workflow(job_id, repo_path, None, "brief", None, False, False)
