"""Regression tests for the Agent Studio Temporal bootstrap.

Guards the two failure modes the wiring is designed to avoid:

1. **Self-bootstrap at import time.** Importing the package must NOT spin up a worker
   thread (that would race the first request's client-ready wait). Boot is the
   unified-API lifespan's job.
2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox re-imports the
   workflow module + package ``__init__`` during workflow registration; neither may
   call ``os.getenv`` at module top level. That is why ``TASK_QUEUE`` is a literal.
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
    """Loading the package (and its submodules) must NOT spin up a worker thread."""
    import shared.temporal

    _purge("agent_team_studio.agent_studio.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("agent_team_studio.agent_studio.temporal")
        importlib.import_module("agent_team_studio.agent_studio.temporal.workflows")
        importlib.import_module("agent_team_studio.agent_studio.temporal.dispatch")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the first "
            f"request and a temporalio sandbox violation when the workflow registers."
        )


def test_workflows_and_package_do_not_call_os_getenv_at_import_time():
    """The workflow module + package __init__ are replayed by the temporalio sandbox
    during workflow registration. Neither may invoke ``os.getenv`` at module top
    level — it has to live inside activity bodies or the worker bootstrap."""
    _purge("agent_team_studio.agent_studio.temporal")
    import os

    importlib.import_module("agent_team_studio.agent_studio.temporal.workflows")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("agent_team_studio.agent_studio.temporal")
        assert spy.call_count == 0, (
            f"agent_team_studio.agent_studio.temporal.__init__ called os.getenv {spy.call_count} time(s) "
            f"at import — this trips the temporalio workflow sandbox during registration."
        )


def test_worker_module_exposes_lifespan_entrypoint():
    """The unified-API lifespan calls a no-arg
    ``start_agent_studio_temporal_worker_thread``. Keep that contract pinned."""
    from agent_team_studio.agent_studio.temporal import worker

    fn = getattr(worker, "start_agent_studio_temporal_worker_thread", None)
    assert callable(fn), (
        "the unified-API lifespan expects a no-arg "
        "start_agent_studio_temporal_worker_thread() in agent_team_studio.agent_studio.temporal.worker"
    )


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """The no-arg func delegates to ``start_team_worker`` with the team's own task
    queue and returns its result. No ``is_temporal_enabled`` guard — Agent Studio
    assumes Temporal is always configured."""
    from agent_team_studio.agent_studio.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from agent_team_studio.agent_studio.temporal import worker as worker_mod

    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_agent_studio_temporal_worker_thread() is True
    assert captured == {
        "team": "agent_studio",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
    assert TASK_QUEUE == "agent-studio-queue"
