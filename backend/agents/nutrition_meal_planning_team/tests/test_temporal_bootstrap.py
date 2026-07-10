"""Bootstrap contract for the nutrition Temporal package.

Guards the invariants the temporalio sandbox and the docker worker hook depend
on: importing ``nutrition_meal_planning_team.temporal`` has no worker
side-effect and binds no bootstrap symbols in ``__init__``; the package exports
the three workflows/activities; and ``start_nutrition_temporal_worker_thread``
is the callable the team_service entrypoint looks up (no-op when Temporal is
disabled, delegating to ``start_team_worker`` when enabled). The ``_startup``
backstop must never raise.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest.mock as mock

import pytest


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_temporal_package_does_not_call_os_getenv_at_import_time():
    """The workflow module + package __init__ are replayed by the temporalio
    sandbox during workflow registration. Neither may invoke ``os.getenv`` at
    module top level (it belongs in activity bodies or the worker bootstrap) —
    the queue name is a hardcoded constant precisely for this reason."""
    _purge("nutrition_meal_planning_team.temporal")
    # Load the workflow module (and temporalio) OUTSIDE the spy; then assert the
    # package __init__ re-import reads no env of its own.
    importlib.import_module("nutrition_meal_planning_team.temporal.workflows")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("nutrition_meal_planning_team.temporal")
        assert spy.call_count == 0


def test_temporal_package_exports_workflows_and_activities():
    mod = importlib.import_module("nutrition_meal_planning_team.temporal")
    assert [w.__name__ for w in mod.WORKFLOWS] == [
        "NutritionPlanWorkflow",
        "NutritionRegenerateWorkflow",
        "NutritionMealPlanWorkflow",
    ]
    assert [a.__name__ for a in mod.ACTIVITIES] == [
        "run_nutrition_plan_activity",
        "run_nutrition_regenerate_activity",
        "run_meal_plan_activity",
    ]
    assert mod.TASK_QUEUE == "nutrition_meal_planning-queue"
    assert mod.WORKFLOW_ID_PREFIX == "nutrition-meal-planning-"


def test_importing_temporal_package_does_not_start_worker():
    """Loading the package/workflows/dispatcher must NOT spin up a worker thread.

    A module-level ``start_team_worker`` bootstrap causes a race on the first
    request and a temporalio sandbox ``os.getenv`` violation at registration.
    """
    import shared_temporal

    _purge("nutrition_meal_planning_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("nutrition_meal_planning_team.temporal")
        importlib.import_module("nutrition_meal_planning_team.temporal.workflows")
        importlib.import_module("nutrition_meal_planning_team.temporal.start_workflow")
        assert patched.call_count == 0


@pytest.mark.parametrize("attr", ["is_temporal_enabled", "start_team_worker"])
def test_temporal_init_has_no_bootstrap_symbols(attr):
    """The package ``__init__`` must not bind worker-bootstrap symbols at module
    scope (they belong in ``worker.py``), so the sandbox re-import stays clean."""
    mod = importlib.import_module("nutrition_meal_planning_team.temporal")
    assert not hasattr(mod, attr)


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename can't
    silently break docker-compose."""
    from nutrition_meal_planning_team.temporal import worker

    fn = getattr(worker, "start_nutrition_temporal_worker_thread", None)
    assert callable(fn)


def test_worker_start_is_noop_when_temporal_disabled(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from nutrition_meal_planning_team.temporal.worker import (
        start_nutrition_temporal_worker_thread,
    )

    assert start_nutrition_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    from nutrition_meal_planning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from nutrition_meal_planning_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_nutrition_temporal_worker_thread() is True
    assert captured == {
        "team": "nutrition_meal_planning",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }
    assert TASK_QUEUE == "nutrition_meal_planning-queue"


def test_startup_backstop_never_raises(monkeypatch):
    """The ``on_startup`` hook must swallow worker-start failures so a boot-time
    Temporal problem cannot abort app startup."""
    from nutrition_meal_planning_team.api import main as api_main

    def _boom():
        raise RuntimeError("worker boot exploded")

    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.worker.start_nutrition_temporal_worker_thread",
        _boom,
    )
    # Must not raise.
    api_main._startup()
