"""Temporal workflows + activities for the Nutrition & Meal Planning team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow classes without executing
any non-deterministic top-level code (e.g. ``os.getenv``, worker bootstrap).
Co-locating ``start_team_worker``/``is_temporal_enabled`` with the workflow
classes trips the sandbox with ``__call__ on os.getenv restricted`` during
workflow registration.

Each of the team's three async job kinds is one ``@workflow.defn`` class that
dispatches one ``@activity.defn`` function. The activities reuse the shared
pipeline core (``nutrition_meal_planning_team.pipeline`` — a neutral module, so
the worker does not import the FastAPI app) so the job-store bookkeeping
(RUNNING → COMPLETED, cancel guards, FAILED) lives in exactly one place. Status
is written to the durable ``JobServiceClient`` store, so a completed run
survives a worker/process restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nutrition_meal_planning_team.temporal.constants import TASK_QUEUE

# Meal planning is the longest path (nutrition plan → LLM meal generation →
# guardrail record); the nutrition-plan/regenerate paths are calculator +
# single-narrator calls.
MEAL_PLAN_TIMEOUT = timedelta(hours=2)
NUTRITION_PLAN_TIMEOUT = timedelta(minutes=30)

# The pipelines are long, non-idempotent LLM flows, and the llm_service layer
# already fails over on transient provider errors. A workflow-level retry would
# therefore mostly re-run expensive deterministic failures (and could
# double-write recommendations), so cap at a single attempt: a failure surfaces
# as a failed workflow plus a FAILED job-store row for explicit resubmission
# rather than being auto-retried. Orphaned RUNNING jobs (worker crash mid-run)
# are reconciled to ``interrupted`` by the team_service startup/shutdown
# recovery, not silently re-run.
NO_RETRY = RetryPolicy(maximum_attempts=1)


@activity.defn(name="nutrition_run_plan")
def run_nutrition_plan_activity(job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Run the nutrition-plan job and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``NutritionPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - On success the job row ends COMPLETED with the plan result and the
          activity returns ``{"job_id": job_id}``.
        - If the job was cancelled, leaves the row untouched and returns
          ``{"job_id": job_id}`` (cancelled is terminal — not retried).
        - On a genuine failure, marks the row FAILED (``ValueError`` →
          ``not_found``) and re-raises so the failure surfaces as a failed
          Temporal workflow. Auto-retry is bounded by ``NO_RETRY``.
    """
    from nutrition_meal_planning_team.pipeline import mark_job_failed, run_nutrition_plan_core
    from nutrition_meal_planning_team.shared.job_store import is_job_cancelled

    try:
        run_nutrition_plan_core(job_id, request)
    except Exception as exc:
        activity.logger.exception("Nutrition plan job %s failed", job_id)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        mark_job_failed(job_id, exc)
        raise
    return {"job_id": job_id}


@activity.defn(name="nutrition_run_regenerate")
def run_nutrition_regenerate_activity(job_id: str, client_id: str) -> Dict[str, Any]:
    """Run the nutrition-plan regenerate job and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``client_id`` identifies a client whose profile exists.

    Postconditions:
        - Same job-store contract as ``run_nutrition_plan_activity``.
    """
    from nutrition_meal_planning_team.pipeline import mark_job_failed, run_regenerate_core
    from nutrition_meal_planning_team.shared.job_store import is_job_cancelled

    try:
        run_regenerate_core(job_id, client_id)
    except Exception as exc:
        activity.logger.exception("Nutrition regenerate job %s failed", job_id)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        mark_job_failed(job_id, exc)
        raise
    return {"job_id": job_id}


@activity.defn(name="run_meal_plan_job")
def run_meal_plan_activity(job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Run the meal-plan job and record job status.

    The activity name (``run_meal_plan_job``) is kept from the team's original
    single-activity wiring so any in-flight meal-plan workflow history stays
    valid across this migration.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``MealPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - Same job-store contract as ``run_nutrition_plan_activity``.
    """
    from nutrition_meal_planning_team.pipeline import mark_job_failed, run_meal_plan_core
    from nutrition_meal_planning_team.shared.job_store import is_job_cancelled

    try:
        run_meal_plan_core(job_id, request)
    except Exception as exc:
        activity.logger.exception("Nutrition meal plan job %s failed", job_id)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        mark_job_failed(job_id, exc)
        raise
    return {"job_id": job_id}


@workflow.defn(name="NutritionPlanWorkflow")
class NutritionPlanWorkflow:
    """Runs one nutrition-plan job as a single durable activity.

    Invariants:
        - Job-store status bookkeeping (RUNNING → COMPLETED/FAILED) is owned by
          the activity, not the workflow; the workflow only dispatches and
          propagates the activity's failure.
    """

    @workflow.run
    async def run(self, job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """Durable entrypoint: run the nutrition-plan job for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``NutritionPlanRequest``
              (``body.model_dump()``).

        Postconditions:
            - Delegates to ``run_nutrition_plan_activity`` (which owns job-store
              status bookkeeping) and returns its ``{"job_id": job_id}`` result.
        """
        return await workflow.execute_activity(
            run_nutrition_plan_activity,
            args=[job_id, request],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=NUTRITION_PLAN_TIMEOUT,
            retry_policy=NO_RETRY,
        )


@workflow.defn(name="NutritionRegenerateWorkflow")
class NutritionRegenerateWorkflow:
    """Runs one nutrition-plan regenerate job as a single durable activity."""

    @workflow.run
    async def run(self, job_id: str, client_id: str) -> Dict[str, Any]:
        """Durable entrypoint: rebuild the nutrition plan for ``client_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``client_id`` identifies a client whose profile exists.

        Postconditions:
            - Delegates to ``run_nutrition_regenerate_activity`` (which owns
              job-store status bookkeeping) and returns its ``{"job_id": job_id}``
              result.
        """
        return await workflow.execute_activity(
            run_nutrition_regenerate_activity,
            args=[job_id, client_id],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=NUTRITION_PLAN_TIMEOUT,
            retry_policy=NO_RETRY,
        )


@workflow.defn(name="NutritionMealPlanWorkflow")
class NutritionMealPlanWorkflow:
    """Runs one meal-plan job as a single durable activity."""

    @workflow.run
    async def run(self, job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """Durable entrypoint: run the meal-plan job for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``MealPlanRequest`` (``body.model_dump()``).

        Postconditions:
            - Delegates to ``run_meal_plan_activity`` (which owns job-store
              status bookkeeping) and returns its ``{"job_id": job_id}`` result.
        """
        return await workflow.execute_activity(
            run_meal_plan_activity,
            args=[job_id, request],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=MEAL_PLAN_TIMEOUT,
            retry_policy=NO_RETRY,
        )
