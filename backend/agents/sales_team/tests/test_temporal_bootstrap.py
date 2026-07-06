"""Regression tests for the sales_team Temporal bootstrap.

Guards two failure modes the wiring was designed to avoid:

1. **Self-bootstrap at import time.** The package ``__init__`` used to call
   ``shared_temporal.start_team_worker(...)`` at module load. The worker
   thread connects the Temporal client asynchronously, so the first
   ``start_sales_workflow`` call could lose the race and raise
   ``RuntimeError: Temporal client not available``. Boot is now the
   team_service entrypoint's job (or the API lifespan as a backstop).

2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox
   re-imports the workflow module to load ``SalesWorkflow``. The previous
   ``__init__.py`` called ``is_temporal_enabled()`` — which calls
   ``os.getenv("TEMPORAL_ADDRESS")`` — at module level, and the sandbox
   would abort with ``__call__ on os.getenv restricted``. Both the package
   ``__init__`` and the dedicated ``workflows`` module must import clean.
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
    """Loading the package must NOT spin up a worker thread."""
    import shared_temporal

    _purge("sales_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("sales_team.temporal")
        importlib.import_module("sales_team.temporal.workflows")
        importlib.import_module("sales_team.temporal.start_workflow")
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
    _purge("sales_team.temporal")
    import os

    importlib.import_module("sales_team.temporal.workflows")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("sales_team.temporal")
        assert spy.call_count == 0, (
            f"sales_team.temporal.__init__ called os.getenv {spy.call_count} "
            f"time(s) at import — this trips the temporalio workflow sandbox "
            f"during workflow registration."
        )


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename
    can't silently break docker-compose.
    """
    from sales_team.temporal import worker

    fn = getattr(worker, "start_sales_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_sales_temporal_worker_thread() in sales_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the backstop
    must return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from sales_team.temporal.worker import start_sales_temporal_worker_thread

    assert start_sales_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with
    the team's own task queue and returns its result."""
    from sales_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from sales_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_sales_temporal_worker_thread() is True
    assert captured == {
        "team": "sales",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
    assert TASK_QUEUE == "sales-queue"


def test_start_sales_workflow_delegates_to_shared_bridge(monkeypatch):
    """The team wrapper forwards to ``shared_temporal.start_workflow_sync`` with
    the sales workflow id + task queue."""
    from sales_team.temporal import SalesWorkflow
    from sales_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_sales_workflow("job-abc", {"product_name": "x"})

    assert captured["workflow_run"] is SalesWorkflow.run
    assert captured["args"] == ("job-abc", {"product_name": "x"})
    assert captured["workflow_id"] == "sales-job-abc"
    assert captured["task_queue"] == "sales-queue"
