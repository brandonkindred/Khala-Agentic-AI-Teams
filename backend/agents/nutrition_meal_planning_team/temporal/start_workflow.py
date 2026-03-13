"""Start Nutrition Meal Planning Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from nutrition_meal_planning_team.temporal.client import get_temporal_client, get_temporal_loop, is_temporal_enabled
from nutrition_meal_planning_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from nutrition_meal_planning_team.temporal.workflows import NutritionMealPlanWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the Nutrition worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_meal_plan_workflow(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Start NutritionMealPlanWorkflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            NutritionMealPlanWorkflow.run,
            args=[job_id, request_dict],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started NutritionMealPlanWorkflow id=%s", workflow_id)
