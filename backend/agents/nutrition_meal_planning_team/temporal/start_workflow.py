"""Start Nutrition & Meal Planning Temporal workflows from synchronous API code.

Thin wrappers over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared_temporal.run_team_job`` here: it
creates its own job row and sets ``status=running`` itself, which would collide
with the API's ``create_job`` and the activity-owned RUNNING/COMPLETED
bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

from nutrition_meal_planning_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    NutritionMealPlanWorkflow,
    NutritionPlanWorkflow,
    NutritionRegenerateWorkflow,
)
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def _start(workflow_run: Any, job_id: str, arg: Any) -> None:
    """Start ``workflow_run`` for ``job_id`` on the nutrition task queue.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``workflow_run`` is a ``@workflow.run`` reference and ``arg`` is its
          second positional argument (serialized request dict or ``client_id``).

    Postconditions:
        - A workflow ``nutrition-meal-planning-<job_id>`` is started (raises
          ``RuntimeError`` if the worker client never becomes available within
          the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        workflow_run,
        job_id,
        arg,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started %s id=%s", workflow_run.__qualname__, workflow_id)


def start_nutrition_plan_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``NutritionPlanWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``NutritionPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - Delegates to ``_start``; see its contract.
    """
    _start(NutritionPlanWorkflow.run, job_id, request)


def start_regenerate_workflow(job_id: str, client_id: str) -> None:
    """Start ``NutritionRegenerateWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``client_id`` identifies a client whose profile exists.

    Postconditions:
        - Delegates to ``_start``; see its contract.
    """
    _start(NutritionRegenerateWorkflow.run, job_id, client_id)


def start_meal_plan_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``NutritionMealPlanWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``MealPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - Delegates to ``_start``; see its contract.
    """
    _start(NutritionMealPlanWorkflow.run, job_id, request)
