"""Regression tests for the Planning Temporal bootstrap wiring.

Guards the failure modes the shared-infra wiring was designed to avoid:

1. **Self-bootstrap at import time.** Importing the ``temporal`` package must NOT
   spin up a worker thread — the worker connects its client asynchronously, so a
   module-level boot would race the first ``start_planning_workflow`` and raise
   ``RuntimeError: Temporal client not available``. Boot is the team_service
   entrypoint's job (or the API lifespan backstop).

2. **The team_service entrypoint contract.** ``docker-compose`` looks up
   ``TEAM_TEMPORAL_WORKER_FUNC`` on ``TEAM_TEMPORAL_WORKER_MODULE``; keep that
   ``start_planning_temporal_worker_thread`` symbol pinned so a rename can't
   silently break the container wiring.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package (and its workflow/dispatch modules) must NOT spin up a
    worker thread — that would race the first request."""
    import shared_temporal

    _purge("planning_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("planning_team.temporal")
        importlib.import_module("planning_team.temporal.workflows")
        importlib.import_module("planning_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}); this races the first request."
        )


def test_temporal_package_exports_pattern_a_contract():
    """The package must export the WORKFLOWS/ACTIVITIES contract the shared worker
    registers — one workflow and one activity per pipeline phase (+ finalize)."""
    _purge("planning_team.temporal")
    from planning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOW_ID_PREFIX, WORKFLOWS

    assert [w.__name__ for w in WORKFLOWS] == ["PlanningWorkflow"]
    assert len(ACTIVITIES) == 8  # intake, discovery, requirements, market research,
    # synthesis, document production, sub-agent provisioning, finalize
    assert all(callable(a) for a in ACTIVITIES)
    assert TASK_QUEUE == "planning"
    assert WORKFLOW_ID_PREFIX == "planning-"


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py resolves TEAM_TEMPORAL_WORKER_FUNC on
    TEAM_TEMPORAL_WORKER_MODULE; keep the contract pinned."""
    from planning_team.temporal import worker

    fn = getattr(worker, "start_planning_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_planning_temporal_worker_thread() in planning_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """With TEMPORAL_ADDRESS unset the backstop must return False, not raise."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from planning_team.temporal.worker import start_planning_temporal_worker_thread

    assert start_planning_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with the
    team's own task queue + WORKFLOWS/ACTIVITIES and returns its result."""
    from planning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from planning_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_planning_temporal_worker_thread() is True
    assert captured == {
        "team": "planning",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
