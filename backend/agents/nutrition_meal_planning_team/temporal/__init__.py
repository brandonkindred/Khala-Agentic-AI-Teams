"""Temporal workflows + activities for the Nutrition & Meal Planning team.

The workflow classes and activities live in :mod:`workflows` (sandbox-safe — no
top-level non-deterministic calls). Worker startup lives in :mod:`worker` and is
invoked by the team_service entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC``), with the API lifespan as a standalone-dev
backstop, so the Temporal client is connected before the API serves its first
request. This package ``__init__`` must stay free of import-time side effects
(no worker boot, no ``os.getenv``) — the temporalio sandbox replays it during
workflow registration.
"""

from __future__ import annotations

from nutrition_meal_planning_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from nutrition_meal_planning_team.temporal.workflows import (
    NutritionMealPlanWorkflow,
    NutritionPlanWorkflow,
    NutritionRegenerateWorkflow,
    run_meal_plan_activity,
    run_nutrition_plan_activity,
    run_nutrition_regenerate_activity,
)

WORKFLOWS = [
    NutritionPlanWorkflow,
    NutritionRegenerateWorkflow,
    NutritionMealPlanWorkflow,
]
ACTIVITIES = [
    run_nutrition_plan_activity,
    run_nutrition_regenerate_activity,
    run_meal_plan_activity,
]

__all__ = [
    "ACTIVITIES",
    "NutritionMealPlanWorkflow",
    "NutritionPlanWorkflow",
    "NutritionRegenerateWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "run_meal_plan_activity",
    "run_nutrition_plan_activity",
    "run_nutrition_regenerate_activity",
]
