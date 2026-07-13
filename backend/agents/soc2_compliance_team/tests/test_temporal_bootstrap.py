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

import contextlib
import importlib
import sys
import unittest.mock as mock

import pytest


@contextlib.contextmanager
def _purged(prefix: str):
    """Temporarily evict modules under ``prefix`` from ``sys.modules``, then
    restore the exact pre-purge module objects — and the parent-package
    attributes Python's import machinery points at them — afterward.

    A bare delete-with-no-restore would leave the *reimported* copies in
    ``sys.modules`` for the rest of the test session: any code importing
    ``soc2_compliance_team.temporal`` (or a submodule) after this test would
    get a second, distinct set of module/class objects — e.g. a second
    ``Soc2AuditWorkflow`` class distinct from the one other test modules
    already hold a reference to — which can produce confusing identity/type
    mismatches in unrelated tests depending on collection or xdist ordering.

    Restoring the ``sys.modules`` dict entries alone is not sufficient either:
    importing a submodule also sets it as an attribute on its parent package
    (``parent.child = submodule``), and a later ``import_module`` call on an
    already-cached name short-circuits without re-running that assignment. So
    a stale parent attribute set during this context (pointing at a module
    object that ``sys.modules`` no longer considers current) can survive the
    dict restore — e.g. breaking a later ``monkeypatch.setattr("a.b.c", ...)``
    attribute-chain resolution, which walks parent attributes, not
    ``sys.modules``, to get from ``a.b`` to ``c``.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
        sys.modules.update(saved)
        for name, mod in saved.items():
            parent_name, _, leaf = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, leaf, mod)


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared_temporal

    with _purged("soc2_compliance_team.temporal"):
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
    import os

    with _purged("soc2_compliance_team.temporal"):
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
    team's own task queue, enough activity slots for the 5-way audit fan-out,
    and returns its result."""
    from soc2_compliance_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from soc2_compliance_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue, max_concurrent_activities):
        captured.update(
            team=team,
            workflows=workflows,
            activities=activities,
            task_queue=task_queue,
            max_concurrent_activities=max_concurrent_activities,
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_soc2_temporal_worker_thread() is True
    assert captured == {
        "team": "soc2_compliance",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
        "max_concurrent_activities": worker_mod.MAX_CONCURRENT_ACTIVITIES,
    }
    assert TASK_QUEUE == "soc2_compliance-queue"
    # Must cover all 5 concurrently fanned-out criterion activities.
    assert worker_mod.MAX_CONCURRENT_ACTIVITIES >= 5


def test_start_audit_workflow_delegates_to_shared_bridge(monkeypatch, tmp_path):
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

    repo_path = str(tmp_path)
    sw.start_audit_workflow("job-abc", repo_path)

    assert captured["workflow_run"] is Soc2AuditWorkflow.run
    assert captured["args"] == ("job-abc", repo_path)
    assert captured["workflow_id"] == "soc2-audit-job-abc"
    assert captured["task_queue"] == "soc2_compliance-queue"


def test_start_audit_workflow_rejects_empty_job_id(tmp_path):
    from soc2_compliance_team.temporal import start_workflow as sw

    with pytest.raises(ValueError, match="job_id"):
        sw.start_audit_workflow("", str(tmp_path))


def test_start_audit_workflow_rejects_nonexistent_repo_path():
    from soc2_compliance_team.temporal import start_workflow as sw

    with pytest.raises(ValueError, match="repo_path"):
        sw.start_audit_workflow("job-abc", "/nonexistent/path/soc2-xyz")


def test_soc2_registered_in_teams_registry():
    """SOC2 is a first-class shared_temporal team."""
    from shared_temporal.teams_registry import TEAM_TEMPORAL_MODULES

    assert TEAM_TEMPORAL_MODULES.get("soc2_compliance") == "soc2_compliance_team.temporal"
