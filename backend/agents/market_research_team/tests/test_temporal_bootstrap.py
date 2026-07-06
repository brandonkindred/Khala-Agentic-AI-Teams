"""Regression tests for the market_research Temporal bootstrap.

Guards two failure modes that the wiring was designed to avoid:

1. **Self-bootstrap at import time.** The package ``__init__`` used to call
   ``shared_temporal.start_team_worker(...)`` at module load. The worker
   thread connects the Temporal client asynchronously, so the first
   ``start_market_research_workflow`` call could lose the race and raise
   ``RuntimeError: Temporal client not available``. Boot is now the
   team_service entrypoint's job (or the API lifespan as a backstop).

2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox
   re-imports the workflow module to load ``MarketResearchWorkflow``. The
   previous ``__init__.py`` called ``is_temporal_enabled()`` — which calls
   ``os.getenv("TEMPORAL_ADDRESS")`` — at module level, and the sandbox
   aborted with ``__call__ on os.getenv restricted``. Both the package
   ``__init__`` and the dedicated ``workflows`` module must import clean.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock

import pytest


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared_temporal

    _purge("market_research_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("market_research_team.temporal")
        importlib.import_module("market_research_team.temporal.workflows")
        importlib.import_module("market_research_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the "
            f"first request and a temporalio sandbox os.getenv violation when "
            f"the workflow registers."
        )


def test_workflows_and_package_do_not_call_os_getenv_at_import_time():
    """The workflow module + package __init__ are replayed by the temporalio
    sandbox during workflow registration. Neither may invoke ``os.getenv`` at
    module top level — it has to live inside activity bodies or the worker
    bootstrap."""
    _purge("market_research_team.temporal")
    import os

    importlib.import_module("market_research_team.temporal.workflows")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("market_research_team.temporal")
        assert spy.call_count == 0, (
            f"market_research_team.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — this trips the temporalio "
            f"workflow sandbox during workflow registration."
        )


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename
    can't silently break docker-compose.
    """
    from market_research_team.temporal import worker

    fn = getattr(worker, "start_market_research_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_market_research_temporal_worker_thread() in "
        "market_research_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the backstop
    must return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from market_research_team.temporal.worker import (
        start_market_research_temporal_worker_thread,
    )

    assert start_market_research_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with
    the team's own task queue and returns its result."""
    from market_research_team.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        WORKFLOWS,
    )
    from market_research_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_market_research_temporal_worker_thread() is True
    assert captured == {
        "team": "market_research",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
    assert TASK_QUEUE == "market_research-queue"


def test_start_market_research_workflow_delegates_to_shared_bridge(monkeypatch):
    """The team wrapper forwards to ``shared_temporal.start_workflow_sync`` with
    the market-research workflow id + task queue. The sync→async bridge itself
    (client-ready wait, run_coroutine_threadsafe) is exercised by the two
    ``test_shared_start_workflow_sync_*`` cases below."""
    from market_research_team.temporal import MarketResearchWorkflow
    from market_research_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_market_research_workflow("job-abc", {"product_concept": "x"})

    assert captured["workflow_run"] is MarketResearchWorkflow.run
    assert captured["args"] == ("job-abc", {"product_concept": "x"})
    assert captured["workflow_id"] == "market-research-job-abc"
    assert captured["task_queue"] == "market_research-queue"


def test_shared_start_workflow_sync_raises_when_client_never_connects(monkeypatch):
    """The shared bridge must time out with the original error message — not
    raise immediately and not wait forever — when no worker client connects."""
    from shared_temporal import runner

    monkeypatch.setattr(runner, "get_temporal_client", lambda: None)
    monkeypatch.setattr(runner, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(runner, "CLIENT_READY_POLL_S", 0.01)

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        runner._await_client(timeout_s=0.05)


def test_shared_start_workflow_sync_dispatches_when_client_ready(monkeypatch):
    """Happy path: with a connected client + loop, the bridge starts the
    workflow with the given id/queue/args on the worker loop."""
    from shared_temporal import runner

    fake_client = mock.MagicMock(name="client")
    fake_client.start_workflow.return_value = mock.MagicMock(name="coro")
    fake_loop = mock.MagicMock(name="loop")
    fake_loop.is_closed.return_value = False  # a usable worker loop is open
    monkeypatch.setattr(runner, "get_temporal_client", lambda: fake_client)
    monkeypatch.setattr(runner, "get_temporal_loop", lambda: fake_loop)

    fake_future = mock.MagicMock(name="future")
    fake_future.result.return_value = None
    monkeypatch.setattr(runner.asyncio, "run_coroutine_threadsafe", lambda coro, loop: fake_future)

    runner.start_workflow_sync(object(), "a1", {"k": "v"}, workflow_id="wid", task_queue="q")

    fake_client.start_workflow.assert_called_once()
    _, kwargs = fake_client.start_workflow.call_args
    assert kwargs["id"] == "wid"
    assert kwargs["task_queue"] == "q"
    assert kwargs["args"] == ["a1", {"k": "v"}]
    fake_future.result.assert_called_once()
