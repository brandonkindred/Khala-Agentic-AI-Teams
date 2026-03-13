"""Temporal workflows for the Nutrition & Meal Planning team."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nutrition_meal_planning_team.temporal import activities as _activities
    from nutrition_meal_planning_team.temporal.constants import TASK_QUEUE

MEAL_PLAN_TIMEOUT = timedelta(hours=2)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="NutritionMealPlanWorkflow")
class NutritionMealPlanWorkflow:
    """Runs one meal plan job as an activity."""

    @workflow.run
    async def run(self, job_id: str, request_dict: Dict[str, Any]) -> None:
        await workflow.execute_activity(
            _activities.run_meal_plan_activity,
            args=[job_id, request_dict],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=MEAL_PLAN_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
