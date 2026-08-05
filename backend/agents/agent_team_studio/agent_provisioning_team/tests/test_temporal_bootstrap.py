"""Regression tests for the Agent Provisioning Temporal bootstrap wiring.

Guards the failure modes the shared-infra wiring was designed to avoid:

1. **Self-bootstrap at import time.** Importing the ``temporal`` package must NOT
   spin up a worker thread — unified-api imports this package via Agent Console
   sandbox dispatch and must not inherit a full provisioning worker. Boot is the
   team_service entrypoint's job (or ``start_all_team_workers``).
2. **The team_service entrypoint contract.** ``docker-compose`` looks up
   ``TEAM_TEMPORAL_WORKER_FUNC`` on ``TEAM_TEMPORAL_WORKER_MODULE``; keep that
   ``start_agent_provisioning_temporal_worker_thread`` symbol pinned so a rename
   can't silently break the container wiring. Standalone ``uvicorn`` runs use
   the same helper via ``api.main``'s lifespan backstop.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock

import pytest

_TEMPORAL_PREFIX = "agent_team_studio.agent_provisioning_team.temporal"


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _isolate_temporal_modules():
    """Snapshot and restore the ``agent_team_studio.agent_provisioning_team.temporal*`` modules.

    These tests deliberately ``_purge`` + re-import the package to observe
    import-time behavior. Snapshotting/restoring here means that manipulation can
    neither inherit a partially-imported state from an earlier test nor leak one
    into a later test, even in a shared or parallel session.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == _TEMPORAL_PREFIX or name.startswith(_TEMPORAL_PREFIX + ".")
    }
    try:
        yield
    finally:
        _purge(_TEMPORAL_PREFIX)
        sys.modules.update(saved)


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package (and its workflow/dispatch modules) must NOT spin up a
    worker thread — that would start a provisioning worker inside unified-api."""
    import shared.temporal

    _purge("agent_team_studio.agent_provisioning_team.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("agent_team_studio.agent_provisioning_team.temporal")
        importlib.import_module("agent_team_studio.agent_provisioning_team.temporal.workflows")
        importlib.import_module("agent_team_studio.agent_provisioning_team.temporal.start_workflow")
        importlib.import_module(
            "agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch"
        )
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}); importing from unified-api "
            f"must not start the provisioning worker."
        )


def test_temporal_package_exports_pattern_a_contract():
    """The package must export WORKFLOWS/ACTIVITIES/TASK_QUEUE and the explicit
    start helper — packaging stays side-effect-free, boot is deliberate."""
    _purge("agent_team_studio.agent_provisioning_team.temporal")
    from agent_team_studio.agent_provisioning_team.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        WORKFLOWS,
        start_agent_provisioning_temporal_worker_thread,
    )

    assert [w.__name__ for w in WORKFLOWS] == [
        "AgentProvisioningWorkflow",
        "AgentDeprovisioningWorkflow",
    ]
    assert all(callable(a) for a in ACTIVITIES)
    assert isinstance(TASK_QUEUE, str) and TASK_QUEUE
    assert callable(start_agent_provisioning_temporal_worker_thread)


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py resolves TEAM_TEMPORAL_WORKER_FUNC on
    TEAM_TEMPORAL_WORKER_MODULE; keep the contract pinned."""
    from agent_team_studio.agent_provisioning_team.temporal import worker

    fn = getattr(worker, "start_agent_provisioning_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_agent_provisioning_temporal_worker_thread() in "
        "agent_team_studio.agent_provisioning_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """With TEMPORAL_ADDRESS unset the bootstrap must return False, not raise."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from agent_team_studio.agent_provisioning_team.temporal.worker import (
        start_agent_provisioning_temporal_worker_thread,
    )

    assert start_agent_provisioning_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with the
    team's own task queue + WORKFLOWS/ACTIVITIES and returns its result."""
    import shared.temporal
    from agent_team_studio.agent_provisioning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from agent_team_studio.agent_provisioning_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    # start_team_worker is imported inside the function from shared.temporal.
    monkeypatch.setattr(shared.temporal, "start_team_worker", _fake_start)

    assert worker_mod.start_agent_provisioning_temporal_worker_thread() is True
    assert captured == {
        "team": "agent_provisioning",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }


def test_registered_in_shared_registry():
    """agent_provisioning is registered so ``start_all_team_workers`` can boot it
    without relying on import-time side effects."""
    from shared.temporal.teams_registry import TEAM_TEMPORAL_MODULES

    assert (
        TEAM_TEMPORAL_MODULES["agent_provisioning"]
        == "agent_team_studio.agent_provisioning_team.temporal"
    )
