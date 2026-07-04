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


def test_start_workflow_waits_for_client_then_raises(monkeypatch):
    """When the worker is genuinely not running, the helper must time out with
    the original error message — not raise immediately and not wait forever."""
    import pytest

    from job_matching_team.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.01)

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        sw._wait_for_client()


def test_start_workflow_returns_client_when_ready(monkeypatch):
    """The happy path returns the connected (client, loop) pair without waiting."""
    from job_matching_team.temporal import start_workflow as sw

    sentinel_client = object()
    sentinel_loop = object()
    monkeypatch.setattr(sw, "get_temporal_client", lambda: sentinel_client)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: sentinel_loop)

    assert sw._wait_for_client() == (sentinel_client, sentinel_loop)


def test_run_async_bridges_into_the_worker_loop(monkeypatch):
    """_run_async must marshal a coroutine onto the worker's event loop thread
    and return its result."""
    import asyncio
    import threading

    from job_matching_team.temporal import start_workflow as sw

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(sw, "_wait_for_client", lambda *a, **k: (object(), loop))

        async def _coro():
            return 42

        assert sw._run_async(_coro()) == 42
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_start_job_matching_workflow_uses_deterministic_id_and_queue(monkeypatch):
    """The workflow is started with a job-scoped id and the team task queue,
    passing (job_id, request) positionally."""
    from unittest.mock import MagicMock

    from job_matching_team.temporal import TASK_QUEUE, JobMatchingWorkflow
    from job_matching_team.temporal import start_workflow as sw

    fake_client = MagicMock(name="TemporalClient")
    monkeypatch.setattr(sw, "_wait_for_client", lambda *a, **k: (fake_client, object()))
    captured: dict = {}
    monkeypatch.setattr(sw, "_run_async", lambda coro: captured.setdefault("coro", coro))

    sw.start_job_matching_workflow("job-xyz", {"top_n": 2})

    fake_client.start_workflow.assert_called_once()
    args, kwargs = fake_client.start_workflow.call_args
    assert args[0] is JobMatchingWorkflow.run
    assert kwargs["args"] == ["job-xyz", {"top_n": 2}]
    assert kwargs["id"] == "job-matching-job-xyz"
    assert kwargs["task_queue"] == TASK_QUEUE
    assert "coro" in captured
