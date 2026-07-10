"""Regression tests for the SOC2 Temporal bootstrap (shared_temporal pattern).

Guards the two failure modes the shared wiring is designed to avoid:

1. **Self-bootstrap at import time.** The temporal package ``__init__`` must not
   call ``shared_temporal.start_team_worker`` — the worker connects the client
   asynchronously, so a module-level boot would race the first
   ``start_audit_workflow`` call. Boot is the team_service entrypoint's job (or
   the API ``on_startup`` backstop).
2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox re-imports
   the workflow module + package ``__init__`` during workflow registration;
   neither may call ``os.getenv`` at import time.
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

    _purge("soc2_compliance_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("soc2_compliance_team.temporal")
        importlib.import_module("soc2_compliance_team.temporal.workflows")
        importlib.import_module("soc2_compliance_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count})."
        )


def test_workflows_and_package_do_not_call_os_getenv_at_import_time():
    """The workflow module + package __init__ are replayed by the temporalio
    sandbox during workflow registration; neither may invoke ``os.getenv``."""
    _purge("soc2_compliance_team.temporal")
    import os

    importlib.import_module("soc2_compliance_team.temporal.activities")
    importlib.import_module("soc2_compliance_team.temporal.workflows")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("soc2_compliance_team.temporal")
        assert spy.call_count == 0, (
            f"soc2_compliance_team.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — this trips the temporalio sandbox."
        )


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned (docker-compose
    wires ``start_soc2_temporal_worker_thread``)."""
    from soc2_compliance_team.temporal import worker

    fn = getattr(worker, "start_soc2_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_soc2_temporal_worker_thread() in soc2_compliance_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """With TEMPORAL_ADDRESS unset, the backstop returns False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from soc2_compliance_team.temporal.worker import start_soc2_temporal_worker_thread

    assert start_soc2_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with the
    team's own task queue and returns its result."""
    from soc2_compliance_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from soc2_compliance_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_soc2_temporal_worker_thread() is True
    assert captured == {
        "team": "soc2_compliance",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
    assert TASK_QUEUE == "soc2_compliance-queue"


def test_start_audit_workflow_delegates_to_shared_bridge(monkeypatch):
    """The team wrapper forwards to ``shared_temporal.start_workflow_sync`` with
    the SOC2 workflow id + task queue."""
    from soc2_compliance_team.temporal import Soc2AuditWorkflow
    from soc2_compliance_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_audit_workflow("job-abc", "/repo/path")

    assert captured["workflow_run"] is Soc2AuditWorkflow.run
    assert captured["args"] == ("job-abc", "/repo/path")
    assert captured["workflow_id"] == "soc2-audit-job-abc"
    assert captured["task_queue"] == "soc2_compliance-queue"


def test_soc2_registered_in_teams_registry():
    """SOC2 is a first-class shared_temporal team."""
    from shared_temporal.teams_registry import TEAM_TEMPORAL_MODULES

    assert TEAM_TEMPORAL_MODULES.get("soc2_compliance") == "soc2_compliance_team.temporal"
