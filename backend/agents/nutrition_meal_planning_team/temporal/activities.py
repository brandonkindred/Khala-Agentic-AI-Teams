"""Temporal activities for the Nutrition & Meal Planning team."""

from __future__ import annotations

import logging
from typing import Any, Dict

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_meal_plan_job")
def run_meal_plan_activity(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Run the meal plan job with heartbeating and cancellation support."""
    try:
        from nutrition_meal_planning_team.models import MealPlanRequest
        from nutrition_meal_planning_team.shared.job_store import (
            JOB_STATUS_COMPLETED,
            JOB_STATUS_FAILED,
            JOB_STATUS_RUNNING,
            is_job_cancelled,
            update_job,
        )

        body = MealPlanRequest(**request_dict)
        update_job(job_id, status=JOB_STATUS_RUNNING)
        activity.heartbeat("starting")

        # Check for cancellation before expensive work
        if is_job_cancelled(job_id):
            logger.info("Job %s cancelled before execution", job_id)
            return

        from nutrition_meal_planning_team.api.main import orchestrator

        activity.heartbeat("generating_nutrition_plan")

        profile = orchestrator.get_profile(body.client_id)
        if profile is None:
            update_job(job_id, status=JOB_STATUS_FAILED, error="Profile not found")
            return

        nutrition_plan = orchestrator._get_or_generate_nutrition_plan(profile)
        activity.heartbeat("planning_meals")

        if is_job_cancelled(job_id):
            logger.info("Job %s cancelled after nutrition plan", job_id)
            return

        meal_history = orchestrator.meal_feedback_store.get_meal_history(body.client_id, limit=50)
        suggestions = orchestrator.meal_planning_agent.run(
            profile,
            nutrition_plan,
            meal_history,
            period_days=body.period_days,
            meal_types=body.meal_types,
        )
        activity.heartbeat("recording_suggestions")

        from nutrition_meal_planning_team.guardrail import GUARDRAIL_VERSION, is_guardrail_enabled

        guardrail_on = is_guardrail_enabled()
        pipeline_result = orchestrator._record_suggestions(
            body.client_id, profile, suggestions, guardrail_on=guardrail_on
        )

        from nutrition_meal_planning_team.models import MealPlanResponse

        result = MealPlanResponse(
            client_id=body.client_id,
            suggestions=pipeline_result.recorded,
            dropped=pipeline_result.dropped,
            flags_by_recommendation=pipeline_result.flags_by_recommendation,
            guardrail_version=GUARDRAIL_VERSION if guardrail_on else "",
            restrictions_best_effort=pipeline_result.restrictions_best_effort,
        )
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
        activity.heartbeat("completed")
    except Exception:
        logger.exception("Nutrition meal plan activity failed for job %s", job_id)
        raise
