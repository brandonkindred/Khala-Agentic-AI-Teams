"""Start Nutrition & Meal Planning Temporal workflows from synchronous API code.

Thin wrappers over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared_temporal.run_team_job`` here: it
creates its own job row and sets ``status=running`` itself, which would collide
with the API's ``create_job`` and the activity-owned RUNNING/COMPLETED
bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from nutrition_meal_planning_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    NutritionMealPlanWorkflow,
    NutritionPlanWorkflow,
    NutritionRegenerateWorkflow,
)
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_nutrition_plan_workflow(job_id: str, request: Dict[str, Any]) -> None:
    """Start ``NutritionPlanWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``NutritionPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - A workflow ``nutrition-meal-planning-<job_id>`` is started on the
          nutrition task queue (raises ``RuntimeError`` if the worker client
          never becomes available within the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        NutritionPlanWorkflow.run,
        job_id,
        request,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started NutritionPlanWorkflow id=%s", workflow_id)


def start_regenerate_workflow(job_id: str, client_id: str) -> None:
    """Start ``NutritionRegenerateWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``client_id`` identifies a client whose profile exists.

    Postconditions:
        - A workflow ``nutrition-meal-planning-<job_id>`` is started on the
          nutrition task queue.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        NutritionRegenerateWorkflow.run,
        job_id,
        client_id,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started NutritionRegenerateWorkflow id=%s", workflow_id)


def start_meal_plan_workflow(job_id: str, request: Dict[str, Any]) -> None:
    """Start ``NutritionMealPlanWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``MealPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - A workflow ``nutrition-meal-planning-<job_id>`` is started on the
          nutrition task queue.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        NutritionMealPlanWorkflow.run,
        job_id,
        request,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started NutritionMealPlanWorkflow id=%s", workflow_id)
