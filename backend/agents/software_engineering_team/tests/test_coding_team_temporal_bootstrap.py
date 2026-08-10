"""Regression tests for the coding_team Temporal bootstrap.

The coding_team package defines ``CodingTeamWorkflow`` in
``coding_team/temporal/__init__.py``, so the temporalio workflow sandbox
re-imports that module during workflow registration. Two invariants must hold
or the worker breaks the same way the other teams' bootstraps once did:

1. **No import-time worker self-boot.** Importing ``coding_team.temporal`` must
   NOT call ``start_team_worker`` — a worker thread connects its Temporal client
   asynchronously, so the first ``start_coding_team_workflow`` would lose the
   race. Boot is the team_service entrypoint's job via ``temporal.worker``.

2. **No ``os.getenv`` at package import.** The previous ``__init__`` called
   ``is_temporal_enabled()`` (→ ``os.getenv("TEMPORAL_ADDRESS")``) at module
   level; the temporalio sandbox aborts with ``__call__ on os.getenv restricted``
   when it re-imports the workflow module.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock

import pytest

_NS = "software_engineering_team.temporal.coding_team_workflow"


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _restore_temporal_modules():
    """These tests purge ``coding_team.temporal`` from sys.modules to force a
    clean re-import. Snapshot and restore it so the mutation can't leak a
    half-imported module into sibling tests (e.g. the route dispatch test that
    imports the same submodules at request time)."""
    saved = {
        name: mod for name, mod in sys.modules.items() if name == _NS or name.startswith(_NS + ".")
    }
    try:
        yield
    finally:
        _purge(_NS)
        sys.modules.update(saved)


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared.temporal

    _purge("software_engineering_team.temporal.coding_team_workflow")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("software_engineering_team.temporal.coding_team_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the "
            f"first request and a temporalio sandbox os.getenv violation when "
            f"the workflow registers."
        )


def test_temporal_package_does_not_call_os_getenv_at_import_time():
    """The package __init__ defines CodingTeamWorkflow and is what the sandbox
    replays — it must not call os.getenv at import."""
    _purge("software_engineering_team.temporal.coding_team_workflow")
    import os

    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("software_engineering_team.temporal.coding_team_workflow")
        assert spy.call_count == 0, (
            f"software_engineering_team.temporal.coding_team_workflow.__init__ called os.getenv {spy.call_count} "
            f"time(s) at import — this trips the temporalio workflow sandbox "
            f"during workflow registration."
        )


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename
    can't silently break docker-compose."""
    from software_engineering_team.temporal import coding_team_worker as worker

    fn = getattr(worker, "start_coding_team_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_coding_team_temporal_worker_thread() in software_engineering_team.temporal.coding_team_worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the worker
    boot must return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from software_engineering_team.temporal.coding_team_worker import (
        start_coding_team_temporal_worker_thread,
    )

    assert start_coding_team_temporal_worker_thread() is False


def test_worker_registers_grooming_workflows_and_activities(monkeypatch):
    """Issue grooming's WORKFLOWS/ACTIVITIES must be merged onto the
    coding-team worker alongside CodingTeamWorkflow's own, per
    ``coding_team_worker.py``'s module docstring."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    import shared.temporal
    from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE
    from software_engineering_team.temporal.coding_team_worker import (
        start_coding_team_temporal_worker_thread,
    )
    from software_engineering_team.temporal.coding_team_workflow import (
        ACTIVITIES as CODING_TEAM_ACTIVITIES,
    )
    from software_engineering_team.temporal.coding_team_workflow import (
        WORKFLOWS as CODING_TEAM_WORKFLOWS,
    )
    from software_engineering_team.temporal.issue_grooming_workflow import (
        ACTIVITIES as GROOMING_ACTIVITIES,
    )
    from software_engineering_team.temporal.issue_grooming_workflow import (
        WORKFLOWS as GROOMING_WORKFLOWS,
    )

    with mock.patch.object(shared.temporal, "start_team_worker", return_value=True) as patched:
        result = start_coding_team_temporal_worker_thread()

    assert result is True
    patched.assert_called_once()
    args, kwargs = patched.call_args
    team, workflows, activities = args
    assert team == "coding_team"
    assert kwargs.get("task_queue") == TASK_QUEUE
    assert workflows == CODING_TEAM_WORKFLOWS + GROOMING_WORKFLOWS
    assert activities == CODING_TEAM_ACTIVITIES + GROOMING_ACTIVITIES
    assert set(GROOMING_WORKFLOWS).issubset(set(workflows))
    assert set(GROOMING_ACTIVITIES).issubset(set(activities))
