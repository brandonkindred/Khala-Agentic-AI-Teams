"""Tests for the nutrition Temporal start-workflow bridges + client shim.

Each ``start_*_workflow`` wrapper must forward to ``shared_temporal.start_workflow_sync``
with the workflow's ``run`` reference, positional args, the deterministic
workflow id, and the team task queue. The shared bridge itself (client-ready
wait, run_coroutine_threadsafe) is covered by ``shared_temporal``'s own tests.
"""

from __future__ import annotations

import importlib

from nutrition_meal_planning_team.temporal import (
    NutritionMealPlanWorkflow,
    NutritionPlanWorkflow,
    NutritionRegenerateWorkflow,
)
from nutrition_meal_planning_team.temporal import start_workflow as sw


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run,
            args=args,
            workflow_id=workflow_id,
            task_queue=task_queue,
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)
    return captured


def test_start_nutrition_plan_workflow_delegates(monkeypatch):
    captured = _capture(monkeypatch)

    sw.start_nutrition_plan_workflow("job-abc", {"client_id": "c"})

    assert captured["workflow_run"] is NutritionPlanWorkflow.run
    assert captured["args"] == ("job-abc", {"client_id": "c"})
    assert captured["workflow_id"] == "nutrition-meal-planning-job-abc"
    assert captured["task_queue"] == "nutrition-meal-planning"


def test_start_regenerate_workflow_delegates(monkeypatch):
    captured = _capture(monkeypatch)

    sw.start_regenerate_workflow("job-def", "client-7")

    assert captured["workflow_run"] is NutritionRegenerateWorkflow.run
    assert captured["args"] == ("job-def", "client-7")
    assert captured["workflow_id"] == "nutrition-meal-planning-job-def"
    assert captured["task_queue"] == "nutrition-meal-planning"


def test_start_meal_plan_workflow_delegates(monkeypatch):
    captured = _capture(monkeypatch)

    sw.start_meal_plan_workflow("job-ghi", {"client_id": "c", "period_days": 7})

    assert captured["workflow_run"] is NutritionMealPlanWorkflow.run
    assert captured["args"] == ("job-ghi", {"client_id": "c", "period_days": 7})
    assert captured["workflow_id"] == "nutrition-meal-planning-job-ghi"
    assert captured["task_queue"] == "nutrition-meal-planning"


def test_client_shim_reexports_shared_temporal_helpers():
    """The back-compat client shim re-exports the shared connection helpers."""
    import shared_temporal.client as shared_client

    client = importlib.import_module("nutrition_meal_planning_team.temporal.client")

    assert client.is_temporal_enabled is shared_client.is_temporal_enabled
    assert client.get_temporal_client is shared_client.get_temporal_client
    assert client.get_temporal_loop is shared_client.get_temporal_loop
