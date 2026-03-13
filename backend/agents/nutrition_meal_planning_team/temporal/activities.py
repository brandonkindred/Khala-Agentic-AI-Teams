"""Temporal activities for the Nutrition & Meal Planning team."""

from __future__ import annotations

import logging
from typing import Any, Dict

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_meal_plan_job")
def run_meal_plan_activity(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Run the meal plan job. Reconstructs MealPlanRequest from request_dict."""
    try:
        from nutrition_meal_planning_team.api.main import _run_meal_plan_job
        from nutrition_meal_planning_team.models import MealPlanRequest
        body = MealPlanRequest(**request_dict)
        _run_meal_plan_job(job_id, body)
    except Exception as e:
        logger.exception("Nutrition meal plan activity failed for job %s", job_id)
        raise
