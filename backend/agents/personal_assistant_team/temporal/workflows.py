"""Temporal workflow for the personal assistant team.

``PaAssistantWorkflow`` drives the whole team as a sequence of activities —
classify intent, run the matching specialist concurrently with the
profile-update extraction, generate the response, finalize — branching on the
classified intent. Job-store bookkeeping lives in the activities (see
``activities.py``); the workflow only orchestrates and, on a genuine activity
failure, records the failure and re-raises so the run surfaces as a failed
workflow *and* a FAILED job row.

Kept separate from the package ``__init__`` and free of top-level
``os.getenv``/heavy imports so the temporalio workflow sandbox can re-import
this module during workflow registration without tripping. Activity imports go
through ``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import is_cancelled_exception

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
#
# Removal criterion: once no open workflow histories predate the decomposition
# deploy (i.e. every in-flight pre-decomposition run has drained), replace this
# whole `if not workflow.patched(...)` block with
# ``workflow.deprecate_patch("pa-decomposed-activities")`` for one release,
# then delete the marker entirely along with ``run_assistant_activity`` and
# the ``LEGACY_TIMEOUT``/``LEGACY_RETRY`` constants below.
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


def _error_message(exc: BaseException) -> str:
    """Best-effort human-readable message from a failed workflow step.

    Preconditions:
        - ``exc`` is the exception caught by ``PaAssistantWorkflow.run``'s
          top-level handler: typically a Temporal ``ActivityError`` (which
          carries a ``.cause`` naming the underlying activity failure), but
          may be any other exception raised directly in workflow-body code.

    Postconditions:
        - Returns ``str(exc.cause)`` when ``exc`` exposes a non-``None``
          ``cause`` attribute (the ``ActivityError`` case), otherwise
          ``str(exc)``. Never raises, even if that ``str()`` call itself
          raises (a badly-behaved ``__str__`` on a third-party exception
          type) — this runs inside an ``except`` handler, so a second
          exception here would replace the one already being reported.
    """
    cause = getattr(exc, "cause", None)
    target = cause if cause is not None else exc
    try:
        return str(target)
    except Exception:
        return f"{type(target).__name__} (error message unavailable)"


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
    ) -> Optional[Dict[str, Any]]:
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
            - On the legacy-drain branch (see ``_DECOMPOSED_PATCH``), returns
              ``None`` — ``run_assistant_activity`` has no return payload,
              matching the pre-decomposition workflow's return value exactly.
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
            # The specialist handler and the profile-update extraction are
            # each their own LLM call, and neither's input depends on the
            # other's output, so they run concurrently rather than back to
            # back — halving this stretch's wall-clock latency, which is paid
            # on every single job. Both activities write their own progress
            # percentage via update_job, so running them concurrently means
            # those writes can land in either order and the job's reported
            # progress can visibly jump backwards for a moment (e.g. 70 then
            # 30). That is a cosmetic, self-correcting UX quirk — the next
            # write always moves progress forward again — and is judged worth
            # the real latency savings for every job.
            action, profile_result = await asyncio.gather(
                workflow.execute_activity(
                    specialist_activity,
                    args=[job_id, user_id, message, context, intent],
                    start_to_close_timeout=STEP_TIMEOUT,
                    retry_policy=LLM_RETRY,
                ),
                workflow.execute_activity(
                    _activities.check_profile_updates_activity,
                    args=[job_id, user_id, message, context],
                    start_to_close_timeout=STEP_TIMEOUT,
                    retry_policy=LLM_RETRY,
                ),
            )
            if action.get("cancelled"):
                return action
            if profile_result is None:
                return {"cancelled": True}

            if action.get("agent") == "orchestrator" and action.get("action") == "error":
                # _run_specialist caught a non-LLM handler exception and
                # synthesized a degraded action. Thread mode's handle_request
                # never populates `results` for the equivalent exception path
                # (the assignment lives inside the try, after the now-unreached
                # success line) — match that here rather than feeding an error
                # dict into the response-generation prompt under the
                # specialist's key.
                results: Dict[str, Any] = {}
            else:
                results = {result_key: action.get("result", {})}

            response = await workflow.execute_activity(
                _activities.generate_response_activity,
                args=[job_id, user_id, message, intent, [action], results, profile_result, context],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
            if response.get("cancelled"):
                return response

            await workflow.execute_activity(
                _activities.finalize_success_activity,
                args=[job_id, response, user_id, message],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
            )
            return response

        except Exception as exc:
            if is_cancelled_exception(exc):
                # Native Temporal-level workflow cancellation (distinct from the
                # PA job-store's own is_job_cancelled flag, which activities
                # poll directly) must propagate and actually cancel the
                # workflow, not be treated as an application failure. A
                # cancelled *activity* surfaces here as an ActivityError
                # wrapping a CancelledError cause, not a bare CancelledError,
                # so a plain `except CancelledError` (which only matches the
                # bare case) would misclassify that as an application failure
                # and incorrectly call fail_job_activity.
                raise
            # A missing LLM provider, a specialist error, or any other failure
            # inside the decomposed flow (including a genuine workflow-body
            # bug, not just an ActivityError) lands here: record the failure on
            # the job row, then re-raise so the workflow itself fails (rather
            # than silently "completing", or — for a non-ActivityError bug —
            # leaving the job stuck at its last RUNNING state forever because
            # fail_job_activity was never called).
            await workflow.execute_activity(
                _activities.fail_job_activity,
                args=[job_id, _error_message(exc)],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
            )
            raise
