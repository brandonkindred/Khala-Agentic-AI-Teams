"""Temporal activities for the personal assistant team.

Each unit of the orchestrator's work is exposed as its own ``@activity.defn``
so ``PaAssistantWorkflow`` can drive the whole team through Temporal:

    classify intent
      -> one specialist handler (email/calendar/tasks/deals/reservations/
         documentation/profile/general)
      -> apply profile updates
      -> generate the natural-language response
      -> finalize (or fail) the job

Every activity is a *thin wrapper* that reuses the existing
``PersonalAssistantOrchestrator`` (via ``core.get_orchestrator``) and the PA
job store. Job-store bookkeeping (RUNNING/COMPLETED/FAILED + cancel guard)
lives here in the activities, never in the workflow, so status survives a
worker restart.

Import hygiene: this module keeps its top level free of heavy / environment
imports. ``temporal.__init__`` imports these functions to build ``ACTIVITIES``,
and the temporalio workflow sandbox replays that package during workflow
registration — so orchestrator/store imports are done lazily inside each
activity body (mirrors ``market_research_team.temporal.workflows``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from temporalio import activity

logger = logging.getLogger(__name__)

# Cancellation sentinel returned by intent/specialist activities when the job
# has been cancelled, so the workflow can short-circuit without marking the run
# failed (mirrors the thread-path early-return in ``api.main._run_assistant_job``).
_CANCELLED: Dict[str, Any] = {"cancelled": True}

# intent.primary -> (orchestrator handler method, progress status text). Matches
# the routing + progress messages of the pre-existing thread path
# (``orchestrator.agent.PersonalAssistantOrchestrator.handle_request``).
_SPECIALIST_STATUS: Dict[str, str] = {
    "email": "Handling email request...",
    "calendar": "Checking your calendar...",
    "tasks": "Managing your tasks...",
    "deals": "Searching for deals...",
    "reservations": "Processing reservation request...",
    "documentation": "Generating documentation...",
    "profile": "Updating your profile...",
    "general": "Processing your request...",
}


def _run_specialist(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
    method_name: str,
    status_text: str,
) -> Dict[str, Any]:
    """Run one orchestrator specialist handler and return its ``AgentAction``.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).
        - ``method_name`` names an existing ``_handle_*`` method on the
          orchestrator.

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the handler's result, or the
          ``{"cancelled": True}`` sentinel if the job was cancelled first.
        - Advances the job to progress 30 with ``status_text`` when it runs.
    """
    from ..core import get_orchestrator
    from ..orchestrator.models import Intent, OrchestratorRequest
    from ..shared.pa_job_store import is_job_cancelled, update_job

    if is_job_cancelled(job_id):
        return dict(_CANCELLED)
    update_job(job_id, status_text=status_text, progress=30)
    orchestrator = get_orchestrator()
    request = OrchestratorRequest(user_id=user_id, message=message, context=context or {})
    handler = getattr(orchestrator, method_name)
    action = handler(request, Intent(**intent))
    return action.model_dump()


@activity.defn(name="pa_classify_intent")
def classify_intent_activity(
    job_id: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify the user message into an ``Intent``.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.

    Postconditions:
        - Marks the job RUNNING and returns ``Intent.model_dump()``, or the
          cancellation sentinel if the job was cancelled.
        - Re-raises ``LLMNotConfiguredError`` (a missing provider must fail the
          run, not default to ``general``).
    """
    from ..core import get_orchestrator
    from ..shared.pa_job_store import PA_JOB_STATUS_RUNNING, is_job_cancelled, update_job

    # Check the cancel guard BEFORE stamping RUNNING so a cancellation that
    # landed between create_job and this first activity is honored rather than
    # clobbered back to RUNNING.
    if is_job_cancelled(job_id):
        return dict(_CANCELLED)
    update_job(
        job_id,
        status=PA_JOB_STATUS_RUNNING,
        status_text="Classifying intent...",
        progress=5,
    )

    intent = get_orchestrator().classify_intent(message)
    logger.info("Classified intent: %s (confidence: %.2f)", intent.primary, intent.confidence)
    update_job(
        job_id,
        status_text=f"Processing {intent.primary} request...",
        progress=15,
        request_type=intent.primary,
    )
    return intent.model_dump()


@activity.defn(name="pa_handle_email")
def handle_email_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the email specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id, user_id, message, context, intent, "_handle_email", _SPECIALIST_STATUS["email"]
    )


@activity.defn(name="pa_handle_calendar")
def handle_calendar_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the calendar specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id,
        user_id,
        message,
        context,
        intent,
        "_handle_calendar",
        _SPECIALIST_STATUS["calendar"],
    )


@activity.defn(name="pa_handle_tasks")
def handle_tasks_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the tasks specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id, user_id, message, context, intent, "_handle_tasks", _SPECIALIST_STATUS["tasks"]
    )


@activity.defn(name="pa_handle_deals")
def handle_deals_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the deal-finder specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id, user_id, message, context, intent, "_handle_deals", _SPECIALIST_STATUS["deals"]
    )


@activity.defn(name="pa_handle_reservations")
def handle_reservations_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the reservation specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id,
        user_id,
        message,
        context,
        intent,
        "_handle_reservations",
        _SPECIALIST_STATUS["reservations"],
    )


@activity.defn(name="pa_handle_documentation")
def handle_documentation_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the documentation specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id,
        user_id,
        message,
        context,
        intent,
        "_handle_documentation",
        _SPECIALIST_STATUS["documentation"],
    )


@activity.defn(name="pa_handle_profile")
def handle_profile_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the profile specialist. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id, user_id, message, context, intent, "_handle_profile", _SPECIALIST_STATUS["profile"]
    )


@activity.defn(name="pa_handle_general")
def handle_general_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the general/fallback handler. See :func:`_run_specialist` for the contract."""
    return _run_specialist(
        job_id, user_id, message, context, intent, "_handle_general", _SPECIALIST_STATUS["general"]
    )


@activity.defn(name="pa_check_profile_updates")
def check_profile_updates_activity(job_id: str, user_id: str, message: str) -> List[Dict[str, Any]]:
    """Extract and apply high-confidence profile preferences from the message.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.

    Postconditions:
        - Applies any high-confidence preferences (side effect on the profile
          store) and returns the list of applied preference dicts.
        - Advances the job to progress 70.
    """
    from ..core import get_orchestrator
    from ..orchestrator.models import OrchestratorRequest
    from ..shared.pa_job_store import update_job

    update_job(job_id, status_text="Checking for profile updates...", progress=70)
    request = OrchestratorRequest(user_id=user_id, message=message, context={})
    return get_orchestrator()._check_for_profile_updates(request)


@activity.defn(name="pa_generate_response")
def generate_response_activity(
    job_id: str,
    user_id: str,
    message: str,
    intent: Dict[str, Any],
    actions: List[Dict[str, Any]],
    results: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate the natural-language response from the specialist actions.

    Preconditions:
        - ``intent`` is a serialized ``Intent``; ``actions`` are serialized
          ``AgentAction`` dicts; ``results`` maps an intent key to its result.

    Postconditions:
        - Returns ``OrchestratorResponse.model_dump()``.
        - Advances the job to progress 85.
        - Re-raises ``LLMNotConfiguredError`` (missing provider fails the run).
    """
    from ..core import get_orchestrator
    from ..orchestrator.models import AgentAction, Intent, OrchestratorRequest
    from ..shared.pa_job_store import update_job

    update_job(job_id, status_text="Generating response...", progress=85)
    request = OrchestratorRequest(user_id=user_id, message=message, context={})
    action_objs = [AgentAction(**a) for a in actions]
    response = get_orchestrator()._generate_response(
        request, Intent(**intent), action_objs, results
    )
    return response.model_dump()


@activity.defn(name="pa_finalize_success")
def finalize_success_activity(
    job_id: str, response: Dict[str, Any], user_id: str, message: str
) -> None:
    """Mark the job completed with the response and fire the Slack notification.

    Preconditions:
        - ``response`` is a serialized ``OrchestratorResponse``.

    Postconditions:
        - Marks the job COMPLETED at progress 100 with the response stored in the
          same ``AssistantResponse`` shape as the thread path — UNLESS the job was
          cancelled first, in which case it is left untouched (mirrors the final
          cancel guard in ``api.main._run_assistant_job``).
        - Sends the Slack notification at most once: it is skipped when the job is
          already COMPLETED, so an activity retry (e.g. a worker crash before
          Temporal recorded completion) cannot double-notify.
    """
    from ..models import AssistantResponse
    from ..shared.pa_job_store import (
        PA_JOB_STATUS_COMPLETED,
        get_job,
        is_job_cancelled,
        update_job,
    )

    # Do not complete a job the user cancelled after the last cancel-checked step.
    if is_job_cancelled(job_id):
        return
    existing = get_job(job_id)
    already_completed = existing is not None and existing.get("status") == PA_JOB_STATUS_COMPLETED

    assistant_response = AssistantResponse(
        request_id=job_id,
        message=response.get("message", "I've processed your request."),
        actions_taken=response.get("actions_taken", []),
        data=response.get("data", {}),
        follow_up_suggestions=response.get("follow_up_suggestions", []),
    )
    update_job(
        job_id,
        status=PA_JOB_STATUS_COMPLETED,
        progress=100,
        status_text="Request completed successfully",
        response=assistant_response.model_dump(),
    )
    # Slack delivery is not idempotent; only notify on the first completion.
    if not already_completed:
        _notify_slack(user_id, message, assistant_response)


@activity.defn(name="pa_fail_job")
def fail_job_activity(job_id: str, error: str) -> None:
    """Mark the job failed with the given error message.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.

    Postconditions:
        - The job store row ends FAILED with ``error`` recorded, UNLESS the job
          was already cancelled (a user cancellation is not overwritten).
    """
    from ..shared.pa_job_store import PA_JOB_STATUS_FAILED, is_job_cancelled, update_job

    # A user cancellation takes precedence over a downstream error.
    if is_job_cancelled(job_id):
        return
    update_job(
        job_id,
        status=PA_JOB_STATUS_FAILED,
        status_text=f"Error: {error}",
        error=error,
    )


@activity.defn(name="run_pa_assistant")
def run_assistant_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Legacy single-activity job runner (pre-decomposition).

    Retained ONLY so ``PaAssistantWorkflow`` executions that were started before
    the activity decomposition can replay/drain deterministically — the workflow
    gates the decomposed path behind ``workflow.patched`` and schedules this
    activity for un-patched (old) histories. New executions never schedule it.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.

    Postconditions:
        - Runs the whole orchestrator job via the thread-path runner and re-raises
          on failure (job-store bookkeeping is owned by ``_run_assistant_job``).
    """
    try:
        from personal_assistant_team.api.main import _run_assistant_job

        _run_assistant_job(job_id, user_id, message, context or {})
    except Exception:
        logger.exception("PA assistant activity failed for job %s", job_id)
        raise


def _notify_slack(user_id: str, message: str, response: Any) -> None:
    """Best-effort Slack notification for a completed PA response.

    Preconditions:
        - ``response`` exposes ``message``/``actions_taken``/
          ``follow_up_suggestions`` (an ``AssistantResponse``).

    Postconditions:
        - Sends the notification if the notifier is importable; swallows any
          import or delivery error so it never fails the job.
    """
    try:
        from unified_api.slack_notifier import notify_pa_response
    except ImportError:
        return
    try:
        notify_pa_response(
            user_id,
            message,
            response.message,
            response.actions_taken,
            response.follow_up_suggestions,
        )
    except Exception:  # pragma: no cover - notification is best-effort
        logger.warning("Slack notification failed for PA job", exc_info=True)
