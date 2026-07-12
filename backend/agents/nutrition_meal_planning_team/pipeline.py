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
import threading
from typing import Any, Dict, Optional, Union

from nutrition_meal_planning_team.models import MealPlanRequest, NutritionPlanRequest
from nutrition_meal_planning_team.orchestrator.agent import (
    NutritionMealPlanningOrchestrator,
    OperationCancelled,
)
from nutrition_meal_planning_team.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    is_job_cancelled,
    update_job,
)

logger = logging.getLogger(__name__)

_orchestrator: Optional[NutritionMealPlanningOrchestrator] = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> NutritionMealPlanningOrchestrator:
    """Get or create the process-wide orchestrator singleton.

    Lazy (deferred past module import) so the container can start — and every
    route, including ones that don't touch an LLM, can serve — even when no LLM
    provider is configured yet. The Strands model is itself built with
    ``lazy=True``: resolving it (via ``get_strands_model`` -> ``get_client``) is
    what raises ``LLMNotConfiguredError`` when the Postgres provider list is
    empty, and that error should fail the individual request/job that actually
    needs an LLM, not orchestrator construction or process startup.

    Construction is guarded by a lock (double-checked) because the singleton is
    now shared across concurrent callers — API request threads and Temporal
    activity-executor threads — so a first-use race must not build two
    orchestrators.

    Preconditions:
        - None.

    Postconditions:
        - Returns a ``NutritionMealPlanningOrchestrator`` singleton, constructed
          exactly once on first call and reused thereafter (shared by the API
          routes, the thread-dispatch path, and the Temporal activities).
    """
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                from llm_service import get_strands_model

                _orchestrator = NutritionMealPlanningOrchestrator(
                    llm_model=get_strands_model("nutrition_meal_planning", lazy=True)
                )
    return _orchestrator


def mark_job_failed(job_id: str, exc: Exception, *, skip_cancel_check: bool = False) -> None:
    """Record a terminal FAILED status for a job (no-op if it was cancelled).

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``skip_cancel_check``, when True, means the caller has already
          confirmed (via its own guarded check) that the job is not cancelled —
          skips this function's own ``is_job_cancelled`` round trip so a single
          failure path never queries cancellation state twice.

    Postconditions:
        - Unless ``skip_cancel_check`` is set, a cancelled job leaves the row
          untouched (a cancelled run is terminal, not a failure).
        - A ``ValueError`` (e.g. "Profile not found") is recorded with
          ``not_found=True`` so the API surfaces it as a not-found outcome; any
          other exception is recorded as a plain failure with its message.
    """
    if not skip_cancel_check and is_job_cancelled(job_id):
        return
    if isinstance(exc, ValueError):
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc), not_found=True)
    else:
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))


def run_nutrition_plan_core(
    job_id: str, request: Union[NutritionPlanRequest, Dict[str, Any]]
) -> None:
    """Run the nutrition-plan job with cancel guards + RUNNING/COMPLETED bookkeeping.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is either an already-validated ``NutritionPlanRequest``
          (thread-dispatch path, which runs in-process and can pass the model
          directly) or its serialized dict form (Temporal path, crossing the
          workflow/activity boundary as ``body.model_dump()``).

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
    if isinstance(request, dict):
        request = NutritionPlanRequest(**request)
    result = get_orchestrator().get_nutrition_plan(request)
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


def run_meal_plan_core(job_id: str, request: Union[MealPlanRequest, Dict[str, Any]]) -> None:
    """Run the meal-plan job (nutrition plan → meal generation → guardrail record).

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is either an already-validated ``MealPlanRequest``
          (thread-dispatch path) or its serialized dict form (Temporal path,
          ``body.model_dump()``).

    Postconditions:
        - Same RUNNING/COMPLETED + cancel-guard contract as
          ``run_nutrition_plan_core``; delegates the full sequence to the
          orchestrator's public ``get_meal_plan``, passing a ``cancel_check``
          so a job cancelled between nutrition-plan generation and the
          meal-planning LLM call fails fast (returns early, writes nothing)
          instead of still paying for the LLM call.
        - Propagates any other orchestrator exception unchanged.
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    if isinstance(request, dict):
        request = MealPlanRequest(**request)
    try:
        result = get_orchestrator().get_meal_plan(
            request, cancel_check=lambda: is_job_cancelled(job_id)
        )
    except OperationCancelled:
        return
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())


def run_nutrition_plan_background(
    job_id: str, request: Union[NutritionPlanRequest, Dict[str, Any]]
) -> None:
    """Thread-path runner for a nutrition-plan job: run and swallow failures as FAILED.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - On orchestrator failure marks the job FAILED (``ValueError`` maps to
          ``not_found=True``) unless it was cancelled, then returns — a daemon
          thread has no caller to raise to. If recording the failure itself
          raises (e.g. the job service is unreachable), that secondary error is
          logged and swallowed rather than escaping the daemon thread uncaught.
    """
    try:
        run_nutrition_plan_core(job_id, request)
    except Exception as exc:
        logger.exception("Nutrition plan job %s failed", job_id)
        try:
            mark_job_failed(job_id, exc)
        except Exception:
            logger.exception("Failed to record job failure for %s", job_id)


def run_regenerate_background(job_id: str, client_id: str) -> None:
    """Thread-path runner for a regenerate job.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Same failure-swallowing contract as ``run_nutrition_plan_background``.
    """
    try:
        run_regenerate_core(job_id, client_id)
    except Exception as exc:
        logger.exception("Nutrition regenerate job %s failed", job_id)
        try:
            mark_job_failed(job_id, exc)
        except Exception:
            logger.exception("Failed to record job failure for %s", job_id)


def run_meal_plan_background(job_id: str, request: Union[MealPlanRequest, Dict[str, Any]]) -> None:
    """Thread-path runner for a meal-plan job.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Same failure-swallowing contract as ``run_nutrition_plan_background``.
    """
    try:
        run_meal_plan_core(job_id, request)
    except Exception as exc:
        logger.exception("Meal plan job %s failed", job_id)
        try:
            mark_job_failed(job_id, exc)
        except Exception:
            logger.exception("Failed to record job failure for %s", job_id)
