"""Regression tests for the job_matching Temporal bootstrap.

The package must not repeat the two bugs the ``user_agent_founder`` team
already fixed:

1. **Self-bootstrapping a worker at import.** Importing the ``temporal``
   package used to call ``shared_temporal.start_team_worker(...)`` at module
   load, racing the first ``start_*_workflow`` call against the async client
   connect. Worker boot is the team_service entrypoint's job (or the registry),
   never a side effect of import.
2. **``os.getenv`` at workflow-module top level.** The temporalio sandbox
   re-imports the workflow module during workflow registration and aborts with
   ``__call__ on os.getenv restricted`` if the module calls ``os.getenv`` at
   import time.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package (and submodules) must NOT spin up a worker thread."""
    import shared_temporal

    _purge("job_matching_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("job_matching_team.temporal")
        importlib.import_module("job_matching_team.temporal.workflows")
        importlib.import_module("job_matching_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the "
            f"first request and a temporalio sandbox os.getenv violation when "
            f"the workflow registers."
        )


def test_package_init_does_not_call_os_getenv_at_import_time():
    """The package __init__ is replayed by the temporalio sandbox; it must not
    call os.getenv at import (that trips the sandbox during registration)."""
    _purge("job_matching_team.temporal")
    import os

    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("job_matching_team.temporal.workflows")
        spy.reset_mock()
        importlib.import_module("job_matching_team.temporal")
        assert spy.call_count == 0, (
            f"job_matching_team.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — this trips the temporalio "
            f"workflow sandbox during workflow registration."
        )


def test_task_queue_matches_registry_convention():
    """The registry starts workers on ``f'{team}-queue'``; the exported
    TASK_QUEUE (used by worker + dispatch) must agree, or dispatched workflows
    would land on a queue no worker polls."""
    from job_matching_team.temporal import TASK_QUEUE

    assert TASK_QUEUE == "job_matching-queue"


def test_team_registered_in_temporal_modules():
    from shared_temporal.teams_registry import TEAM_TEMPORAL_MODULES

    assert TEAM_TEMPORAL_MODULES.get("job_matching") == "job_matching_team.temporal"


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename can't
    silently break docker-compose."""
    from job_matching_team.temporal import worker

    fn = getattr(worker, "start_job_matching_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_job_matching_temporal_worker_thread() in "
        "job_matching_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """With TEMPORAL_ADDRESS unset, the worker starter returns False (no raise)."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from job_matching_team.temporal.worker import (
        start_job_matching_temporal_worker_thread,
    )

    assert start_job_matching_temporal_worker_thread() is False


def test_worker_start_delegates_when_enabled(monkeypatch):
    """When Temporal is enabled, the starter delegates to start_team_worker with
    the team's workflows/activities/queue."""
    from job_matching_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS, worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker, "start_team_worker", _fake_start)

    assert worker.start_job_matching_temporal_worker_thread() is True
    assert captured == {
        "team": "job_matching",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }


def test_start_job_matching_workflow_delegates_to_shared_bridge(monkeypatch):
    """start_job_matching_workflow delegates to the shared start_workflow_sync
    with the workflow run method, positional (job_id, request), a deterministic
    id, and the team task queue.

    The sync→async plumbing (client-ready wait, closed-loop rejection, coroutine
    marshalling) lives in shared_temporal and is covered by its own tests, so we
    only pin the call contract here rather than re-testing the bridge.
    """
    from job_matching_team.temporal import TASK_QUEUE, JobMatchingWorkflow
    from job_matching_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_job_matching_workflow("job-xyz", {"top_n": 2})

    assert captured["workflow_run"] is JobMatchingWorkflow.run
    assert captured["args"] == ("job-xyz", {"top_n": 2})
    assert captured["workflow_id"] == "job-matching-job-xyz"
    assert captured["task_queue"] == TASK_QUEUE
