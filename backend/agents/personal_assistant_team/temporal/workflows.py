"""Temporal workflow for the personal assistant team.

``PaAssistantWorkflow`` drives the whole team as a sequence of activities —
classify intent, run the matching specialist, apply profile updates, generate
the response, finalize — branching on the classified intent. Job-store
bookkeeping lives in the activities (see ``activities.py``); the workflow only
orchestrates and, on a genuine activity failure, records the failure and
re-raises so the run surfaces as a failed workflow *and* a FAILED job row.

Kept separate from the package ``__init__`` and free of top-level
``os.getenv``/heavy imports so the temporalio workflow sandbox can re-import
this module during workflow registration without tripping. Activity imports go
through ``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from personal_assistant_team.temporal import activities as _activities

    # ``constants`` is a literal (no os.getenv), so importing it here is
    # sandbox-safe; only the legacy-drain branch below references TASK_QUEUE.
    from personal_assistant_team.temporal.constants import TASK_QUEUE

# ``workflow.patched`` id gating the decomposed activity flow. Executions
# started before the decomposition (single ``run_pa_assistant`` activity) have
# no marker in their history, so ``patched`` returns False for them and they
# replay the legacy branch deterministically; new executions run the decomposed
# flow. Never rename this id.
_DECOMPOSED_PATCH = "pa-decomposed-activities"

# Per-step timeout. Each step is at most one LLM round-trip; 30 min is generous.
STEP_TIMEOUT = timedelta(minutes=30)

# Legacy single-activity scheduling options, matching the pre-decomposition
# workflow so old histories replay/drain against unchanged behavior.
LEGACY_TIMEOUT = timedelta(hours=2)
LEGACY_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)

# LLM-driven steps are non-idempotent and the llm_service layer already fails
# over on transient provider errors (429s), so a workflow-level retry would
# mostly re-run expensive deterministic failures. Cap at a single attempt: a
# failure surfaces as a failed workflow + FAILED job row for explicit resubmit
# (same reasoning as market_research's single-attempt policy).
LLM_RETRY = RetryPolicy(maximum_attempts=1)

# Job-store bookkeeping steps are cheap and idempotent, so a small retry rides
# out transient job-service blips.
BOOKKEEPING_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)

# intent.primary -> (specialist activity, results key). Unknown intents fall
# back to the general handler under the "general" key (matches the thread path).
_SPECIALIST_ACTIVITIES = {
    "email": (_activities.handle_email_activity, "email"),
    "calendar": (_activities.handle_calendar_activity, "calendar"),
    "tasks": (_activities.handle_tasks_activity, "tasks"),
    "deals": (_activities.handle_deals_activity, "deals"),
    "reservations": (_activities.handle_reservations_activity, "reservations"),
    "documentation": (_activities.handle_documentation_activity, "documentation"),
    "profile": (_activities.handle_profile_activity, "profile"),
    "general": (_activities.handle_general_activity, "general"),
}


def _error_message(exc: ActivityError) -> str:
    """Best-effort human-readable message from a failed activity.

    Preconditions:
        - ``exc`` is a Temporal ``ActivityError`` raised by ``execute_activity``.

    Postconditions:
        - Returns ``str(exc.cause)`` when a cause is present (the underlying
          failure), otherwise ``str(exc)``. Never raises.
    """
    return str(exc.cause) if exc.cause is not None else str(exc)


@workflow.defn(name="PaAssistantWorkflow")
class PaAssistantWorkflow:
    """Durable orchestration of one personal-assistant job across activities."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        user_id: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run one assistant job end to end.

        Preconditions:
            - ``job_id`` refers to a job already created in the PA job store
              (the API endpoint calls ``create_job`` before dispatch).

        Postconditions:
            - On success: intent classification -> matching specialist ->
              profile-update -> response generation -> finalize all run, the
              job row ends COMPLETED, and the serialized ``OrchestratorResponse``
              is returned.
            - If a step reports cancellation, returns the cancellation sentinel
              without marking the job failed (the cancel guard already set the
              row to CANCELLED).
            - On a genuine activity failure, marks the job FAILED and re-raises.
        """
        context = context or {}

        if not workflow.patched(_DECOMPOSED_PATCH):
            # Executions started before the decomposition replay the original
            # single-activity path so their history stays deterministic.
            return await workflow.execute_activity(
                _activities.run_assistant_activity,
                args=[job_id, user_id, message, context],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=LEGACY_TIMEOUT,
                retry_policy=LEGACY_RETRY,
            )

        try:
            intent = await workflow.execute_activity(
                _activities.classify_intent_activity,
                args=[job_id, message, context],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
            if intent.get("cancelled"):
                return intent

            primary = intent.get("primary", "general")
            specialist_activity, result_key = _SPECIALIST_ACTIVITIES.get(
                primary, (_activities.handle_general_activity, "general")
            )
            action = await workflow.execute_activity(
                specialist_activity,
                args=[job_id, user_id, message, context, intent],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
            if action.get("cancelled"):
                return action

            results = {result_key: action.get("result", {})}

            # Apply high-confidence profile preferences before generating the
            # response, so the response reflects them (matches the thread path).
            # This step makes an LLM call AND mutates the profile store, so it is
            # non-idempotent — cap it at one attempt like the other LLM steps
            # rather than retrying (which would re-bill the LLM and re-apply
            # preferences).
            await workflow.execute_activity(
                _activities.check_profile_updates_activity,
                args=[job_id, user_id, message],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=LLM_RETRY,
            )

            response = await workflow.execute_activity(
                _activities.generate_response_activity,
                args=[job_id, user_id, message, intent, [action], results],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=LLM_RETRY,
            )

            await workflow.execute_activity(
                _activities.finalize_success_activity,
                args=[job_id, response, user_id, message],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
            )
            return response

        except ActivityError as exc:
            # A missing LLM provider or any specialist error lands here: record
            # the failure on the job row, then re-raise so the workflow itself
            # fails (rather than silently "completing").
            await workflow.execute_activity(
                _activities.fail_job_activity,
                args=[job_id, _error_message(exc)],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
            )
            raise
