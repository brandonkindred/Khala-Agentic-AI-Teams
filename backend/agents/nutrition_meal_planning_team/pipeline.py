"""Neutral pipeline core for the Nutrition & Meal Planning team.

No FastAPI, no Temporal imports — holds the lazy orchestrator singleton plus the
cancel-guarded RUNNING → COMPLETED job-store bookkeeping shared by the HTTP
thread-dispatch path (``api.main``) and the Temporal activities
(``temporal.workflows``). Keeping it here lets the durable Temporal worker run
the pipeline without importing the web app, and keeps the status-write order and
cancel/failure semantics in exactly one place for every async job kind
(nutrition plan, regenerate, meal plan).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from nutrition_meal_planning_team.models import MealPlanRequest, NutritionPlanRequest
from nutrition_meal_planning_team.orchestrator.agent import NutritionMealPlanningOrchestrator
from nutrition_meal_planning_team.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    is_job_cancelled,
    update_job,
)

logger = logging.getLogger(__name__)

_orchestrator: Optional[NutritionMealPlanningOrchestrator] = None


def get_orchestrator() -> NutritionMealPlanningOrchestrator:
    """Get or create the process-wide orchestrator singleton.

    Lazy (deferred past module import) so the container can start — and every
    route, including ones that don't touch an LLM, can serve — even when no LLM
    provider is configured yet. The Strands model is itself built with
    ``lazy=True``: resolving it (via ``get_strands_model`` -> ``get_client``) is
    what raises ``LLMNotConfiguredError`` when the Postgres provider list is
    empty, and that error should fail the individual request/job that actually
    needs an LLM, not orchestrator construction or process startup.

    Preconditions:
        - None.

    Postconditions:
        - Returns a ``NutritionMealPlanningOrchestrator`` singleton, constructed
          on first call and reused thereafter (shared by the API routes, the
          thread-dispatch path, and the Temporal activities).
    """
    global _orchestrator
    if _orchestrator is None:
        from llm_service import get_strands_model

        _orchestrator = NutritionMealPlanningOrchestrator(
            llm_model=get_strands_model("nutrition_meal_planning", lazy=True)
        )
    return _orchestrator


def mark_job_failed(job_id: str, exc: Exception) -> None:
    """Record a terminal FAILED status for a job (no-op if it was cancelled).

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - If the job is cancelled, leaves the row untouched (a cancelled run is
          terminal, not a failure).
        - A ``ValueError`` (e.g. "Profile not found") is recorded with
          ``not_found=True`` so the API surfaces it as a not-found outcome; any
          other exception is recorded as a plain failure with its message.
    """
    if is_job_cancelled(job_id):
        return
    if isinstance(exc, ValueError):
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc), not_found=True)
    else:
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))


def run_nutrition_plan_core(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Run the nutrition-plan job with cancel guards + RUNNING/COMPLETED bookkeeping.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request_dict`` is a serialized ``NutritionPlanRequest``
          (``body.model_dump()``).

    Postconditions:
        - Writes RUNNING then COMPLETED (with the plan result) on success;
          writes nothing and returns early if the job is cancelled before or
          after the run.
        - Propagates any orchestrator exception unchanged — the caller owns the
          failure policy (swallow-as-FAILED for threads, re-raise for the
          Temporal activity).
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    result = get_orchestrator().get_nutrition_plan(NutritionPlanRequest(**request_dict))
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())


def run_regenerate_core(job_id: str, client_id: str) -> None:
    """Run the nutrition-plan regenerate job (forced cache miss + rebuild).

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``client_id`` identifies a client whose profile exists.

    Postconditions:
        - Same RUNNING/COMPLETED + cancel-guard contract as
          ``run_nutrition_plan_core``; propagates orchestrator exceptions
          unchanged.
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    result = get_orchestrator().regenerate_nutrition_plan(client_id)
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())


def run_meal_plan_core(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Run the meal-plan job (nutrition plan → meal generation → guardrail record).

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request_dict`` is a serialized ``MealPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - Same RUNNING/COMPLETED + cancel-guard contract as
          ``run_nutrition_plan_core``; delegates the full sequence to the
          orchestrator's public ``get_meal_plan`` and propagates its exceptions
          unchanged.
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    result = get_orchestrator().get_meal_plan(MealPlanRequest(**request_dict))
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())


def run_nutrition_plan_background(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Thread-path runner for a nutrition-plan job: run and swallow failures as FAILED.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - On orchestrator failure marks the job FAILED (``ValueError`` maps to
          ``not_found=True``) unless it was cancelled, then returns — a daemon
          thread has no caller to raise to.
    """
    try:
        run_nutrition_plan_core(job_id, request_dict)
    except Exception as exc:
        logger.exception("Nutrition plan job %s failed", job_id)
        mark_job_failed(job_id, exc)


def run_regenerate_background(job_id: str, client_id: str) -> None:
    """Thread-path runner for a regenerate job (see ``run_nutrition_plan_background``)."""
    try:
        run_regenerate_core(job_id, client_id)
    except Exception as exc:
        logger.exception("Nutrition regenerate job %s failed", job_id)
        mark_job_failed(job_id, exc)


def run_meal_plan_background(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Thread-path runner for a meal-plan job (see ``run_nutrition_plan_background``)."""
    try:
        run_meal_plan_core(job_id, request_dict)
    except Exception as exc:
        logger.exception("Meal plan job %s failed", job_id)
        mark_job_failed(job_id, exc)
