"""Regression tests for the agentic_team_provisioning Temporal bootstrap.

Guards two failure modes the wiring was designed to avoid:

1. **Self-bootstrap at import time.** The package ``__init__`` used to call
   ``shared.temporal.start_team_worker(...)`` at module load. The worker thread
   connects the Temporal client asynchronously, so the first ``start_...workflow`` call
   could lose the race and raise ``RuntimeError: Temporal client not available``. Boot
   is now the team_service entrypoint's job (or the API lifespan as a backstop).

2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox re-imports the
   workflow module to load the workflow class. The previous ``__init__.py`` called
   ``is_temporal_enabled()`` — which calls ``os.getenv("TEMPORAL_ADDRESS")`` — at module
   level, and the sandbox would abort with ``__call__ on os.getenv restricted``. Both the
   package ``__init__`` and the dedicated ``workflows`` module must import clean.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock


def _purge(prefix: str) -> None:
    """Drop ``prefix`` and its submodules from ``sys.modules`` so the next
    ``import_module`` re-executes them from scratch.

    Side-effect warning: this mutates the process-wide module cache. These tests
    deliberately re-import ``agent_team_studio.agentic_team_provisioning.temporal`` to observe its
    import-time behavior, which is only meaningful on a fresh import. Every test here
    re-imports the names it needs inside its own body, so the purge is self-contained
    and order-independent. Mirrors the identical helper in ``sales_team``'s bootstrap
    tests.
    """
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared.temporal

    _purge("agent_team_studio.agentic_team_provisioning.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("agent_team_studio.agentic_team_provisioning.temporal")
        importlib.import_module("agent_team_studio.agentic_team_provisioning.temporal.workflows")
        importlib.import_module(
            "agent_team_studio.agentic_team_provisioning.temporal.start_workflow"
        )
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the first "
            f"request and a temporalio sandbox os.getenv violation on registration."
        )


def test_workflows_and_package_do_not_call_os_getenv_at_import_time():
    """The workflow module + package __init__ are replayed by the temporalio sandbox
    during workflow registration. Neither may invoke ``os.getenv`` at module top level —
    it has to live inside activity bodies or the worker bootstrap."""
    _purge("agent_team_studio.agentic_team_provisioning.temporal")
    import os

    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("agent_team_studio.agentic_team_provisioning.temporal.workflows")
        assert spy.call_count == 0, (
            f"agent_team_studio.agentic_team_provisioning.temporal.workflows called os.getenv "
            f"{spy.call_count} time(s) at import — trips the temporalio workflow sandbox."
        )
        spy.reset_mock()
        importlib.import_module("agent_team_studio.agentic_team_provisioning.temporal")
        assert spy.call_count == 0, (
            f"agent_team_studio.agentic_team_provisioning.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — trips the temporalio workflow sandbox."
        )


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename can't silently
    break docker-compose.
    """
    from agent_team_studio.agentic_team_provisioning.temporal import worker

    fn = getattr(worker, "start_agentic_team_provisioning_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_agentic_team_provisioning_temporal_worker_thread() in "
        "agent_team_studio.agentic_team_provisioning.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the backstop must
    return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from agent_team_studio.agentic_team_provisioning.temporal.worker import (
        start_agentic_team_provisioning_temporal_worker_thread,
    )

    assert start_agentic_team_provisioning_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with the team's
    own task queue and returns its result."""
    from agent_team_studio.agentic_team_provisioning.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        WORKFLOWS,
    )
    from agent_team_studio.agentic_team_provisioning.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_agentic_team_provisioning_temporal_worker_thread() is True
    assert captured == {
        "team": "agentic_team_provisioning",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
    assert TASK_QUEUE == "agentic_team_provisioning-queue"


def test_start_workflow_delegates_to_shared_bridge(monkeypatch):
    """The team wrapper forwards to ``shared.temporal.start_workflow_sync`` with the
    agentic pipeline workflow id + task queue and the correct arg order."""
    from agent_team_studio.agentic_team_provisioning.temporal import AgenticPipelineWorkflow
    from agent_team_studio.agentic_team_provisioning.temporal import start_workflow as sw

    monkeypatch.setenv("AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S", "1234")
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_agentic_pipeline_workflow(
        "run-abc", [{"agent_name": "a"}], {"process_id": "p", "steps": []}, "seed"
    )

    assert captured["workflow_run"] is AgenticPipelineWorkflow.run
    assert captured["args"] == (
        "run-abc",
        [{"agent_name": "a"}],
        {"process_id": "p", "steps": []},
        "seed",
        1234,  # wait_timeout_s passed as an arg, resolved from env in the API process
    )
    assert captured["workflow_id"] == "agentic-pipeline-run-abc"
    assert captured["task_queue"] == "agentic_team_provisioning-queue"


def test_activities_carry_temporal_activity_definitions():
    """The workflow-driver tests call activities directly, so a dropped ``@activity.defn``
    would go unnoticed until a real worker failed to register. Assert every exported
    activity is a registered Temporal activity with the expected name."""
    from agent_team_studio.agentic_team_provisioning.temporal import ACTIVITIES

    names = set()
    for fn in ACTIVITIES:
        defn = getattr(fn, "__temporal_activity_definition", None)
        assert defn is not None, f"{fn.__name__} is missing the @activity.defn decorator"
        names.add(defn.name)

    assert names == {
        "agentic_pipeline_advance_step",
        "agentic_pipeline_run_step",
        "agentic_pipeline_wait_setup",
        "agentic_pipeline_wait_finalize",
        "agentic_pipeline_complete",
        "agentic_pipeline_cancel_reconcile",
        "agentic_pipeline_fail",
    }


def test_workflow_carries_temporal_workflow_definition():
    """A dropped ``@workflow.defn`` / ``@workflow.run`` / ``@workflow.signal`` would only
    surface when a worker registers the workflow. Assert the Temporal definition exposes
    the run method and the ``submit_input`` signal used by the resume path."""
    from temporalio import workflow

    from agent_team_studio.agentic_team_provisioning.temporal import (
        WORKFLOWS,
        AgenticPipelineWorkflow,
    )

    assert WORKFLOWS == [AgenticPipelineWorkflow]
    defn = workflow._Definition.from_class(AgenticPipelineWorkflow)
    assert defn is not None, "AgenticPipelineWorkflow is missing the @workflow.defn decorator"
    assert defn.name == "AgenticPipelineWorkflow"
    assert defn.run_fn.__name__ == "run"
    assert "submit_input" in defn.signals
