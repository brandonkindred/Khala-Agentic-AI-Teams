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
from typing import Any, Callable, Dict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nutrition_meal_planning_team.temporal.constants import TASK_QUEUE

# Meal planning is the longest path (nutrition plan → LLM meal generation →
# guardrail record). The nutrition-plan and regenerate paths are both just
# calculator + single-narrator calls, so they share NUTRITION_PLAN_TIMEOUT.
MEAL_PLAN_TIMEOUT = timedelta(hours=2)
NUTRITION_PLAN_TIMEOUT = timedelta(minutes=30)

# A background thread heartbeats every ``HEARTBEAT_INTERVAL_S`` while an activity
# runs. ``HEARTBEAT_TIMEOUT`` is Temporal's maximum allowed interval *between*
# heartbeats: if the worker dies/hangs and stops heartbeating, Temporal fails the
# activity within this window rather than waiting for ``start_to_close_timeout``
# (up to 2h). Matches how the blogging/coding teams keep their long activities live.
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_TIMEOUT = timedelta(minutes=5)

# The pipelines are long, non-idempotent LLM flows, and the llm_service layer
# already fails over on transient provider errors. A workflow-level retry would
# therefore mostly re-run expensive deterministic failures (and could
# double-write recommendations), so cap at a single attempt: a failure surfaces
# as a failed workflow plus a FAILED job-store row for explicit resubmission
# rather than being auto-retried. Orphaned RUNNING jobs (worker crash mid-run)
# are reconciled to ``interrupted`` by the team_service startup/shutdown
# recovery, not silently re-run.
NO_RETRY = RetryPolicy(maximum_attempts=1)


def _run_activity(job_id: str, core: Callable[..., None], *core_args: Any) -> Dict[str, Any]:
    """Shared activity body: heartbeat while the core runs; own the job-store
    failure contract.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``core`` is one of the ``pipeline.run_*_core`` functions and
          ``core_args`` are its trailing positional arguments.

    Postconditions:
        - On success returns ``{"job_id": job_id}`` (the core wrote COMPLETED).
        - If the job was cancelled, swallows the error and returns
          ``{"job_id": job_id}`` (cancelled is terminal — not retried).
        - On a genuine failure, marks the row FAILED (``ValueError`` →
          ``not_found``) and re-raises so the failure surfaces as a failed
          Temporal workflow (auto-retry bounded by ``NO_RETRY``).
    """
    from nutrition_meal_planning_team.pipeline import mark_job_failed
    from nutrition_meal_planning_team.shared.job_store import is_job_cancelled
    from shared_concurrency import BackgroundHeartbeat

    try:
        # ``copy_context=True`` snapshots the activity context so the daemon
        # thread's ``activity.heartbeat`` reaches this activity execution; a beat
        # error (e.g. outside a live activity in unit tests) is swallowed by the
        # heartbeat driver and never touches the pipeline result.
        with BackgroundHeartbeat(activity.heartbeat, HEARTBEAT_INTERVAL_S, copy_context=True):
            core(job_id, *core_args)
    except Exception as exc:
        activity.logger.exception("Nutrition job %s failed", job_id)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        # Record FAILED best-effort; if the job-store write itself fails, log it
        # but still re-raise the ORIGINAL error so the root cause surfaces.
        try:
            mark_job_failed(job_id, exc)
        except Exception:
            activity.logger.exception("Failed to record job failure for %s", job_id)
        raise
    return {"job_id": job_id}


@activity.defn(name="nutrition_run_plan")
def run_nutrition_plan_activity(job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Run the nutrition-plan job and record job status (see ``_run_activity``).

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``NutritionPlanRequest`` (``body.model_dump()``).

    Postconditions:
        - Per ``_run_activity``: COMPLETED on success, FAILED + re-raise on error
          (``ValueError`` → ``not_found``), swallow on cancel.
    """
    from nutrition_meal_planning_team.pipeline import run_nutrition_plan_core

    return _run_activity(job_id, run_nutrition_plan_core, request)


@activity.defn(name="nutrition_run_regenerate")
def run_nutrition_regenerate_activity(job_id: str, client_id: str) -> Dict[str, Any]:
    """Run the nutrition-plan regenerate job and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``client_id`` identifies a client whose profile exists.

    Postconditions:
        - Per ``_run_activity`` (same job-store contract).
    """
    from nutrition_meal_planning_team.pipeline import run_regenerate_core

    return _run_activity(job_id, run_regenerate_core, client_id)


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
        - Per ``_run_activity`` (same job-store contract).
    """
    from nutrition_meal_planning_team.pipeline import run_meal_plan_core

    return _run_activity(job_id, run_meal_plan_core, request)


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
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
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
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
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
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=NO_RETRY,
        )
