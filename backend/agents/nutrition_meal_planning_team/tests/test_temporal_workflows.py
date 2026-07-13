"""Tests for the nutrition Temporal workflows.

Each ``@workflow.defn`` class is driven directly with ``workflow.execute_activity``
monkeypatched — no Temporal server, no sandbox. Asserts each workflow forwards
(job_id, <arg>) to its activity on the team task queue with the expected timeout
and a single-attempt retry policy.
"""

from __future__ import annotations

import asyncio

import pytest

from nutrition_meal_planning_team.temporal import workflows as wf


def _drive(monkeypatch, workflow_obj, *args):
    """Run ``workflow_obj.run(*args)`` capturing the execute_activity call."""
    captured: dict = {}

    async def _fake_execute_activity(fn, *_a, **kwargs):
        captured["fn"] = fn
        captured["args"] = kwargs.get("args")
        captured["task_queue"] = kwargs.get("task_queue")
        captured["timeout"] = kwargs.get("start_to_close_timeout")
        captured["heartbeat_timeout"] = kwargs.get("heartbeat_timeout")
        captured["retry_policy"] = kwargs.get("retry_policy")
        return {"job_id": args[0]}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)
    out = asyncio.run(workflow_obj.run(*args))
    return out, captured


@pytest.mark.parametrize(
    "workflow_cls, activity_fn, second_arg, timeout",
    [
        (
            wf.NutritionPlanWorkflow,
            wf.run_nutrition_plan_activity,
            {"client_id": "c"},
            wf.NUTRITION_PLAN_TIMEOUT,
        ),
        (
            wf.NutritionRegenerateWorkflow,
            wf.run_nutrition_regenerate_activity,
            "client-9",
            wf.NUTRITION_PLAN_TIMEOUT,
        ),
        (
            wf.NutritionMealPlanWorkflow,
            wf.run_meal_plan_activity,
            {"client_id": "c"},
            wf.MEAL_PLAN_TIMEOUT,
        ),
    ],
)
def test_workflow_run_delegates_to_activity(
    monkeypatch, workflow_cls, activity_fn, second_arg, timeout
):
    out, captured = _drive(monkeypatch, workflow_cls(), "job-1", second_arg)

    assert out == {"job_id": "job-1"}
    assert captured["fn"] is activity_fn
    assert captured["args"] == ["job-1", second_arg]
    assert captured["task_queue"] == wf.TASK_QUEUE
    assert captured["timeout"] == timeout
    # A heartbeat_timeout lets Temporal detect a dead/hung worker within the
    # heartbeat window rather than only at the (up-to-2h) start_to_close_timeout.
    assert captured["heartbeat_timeout"] == wf.HEARTBEAT_TIMEOUT
    # Non-idempotent LLM pipeline: retries are capped at a single attempt.
    assert captured["retry_policy"].maximum_attempts == 1
