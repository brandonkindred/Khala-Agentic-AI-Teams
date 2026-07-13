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
import threading
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
        - Returns ``AgentAction.model_dump(mode="json")`` for the handler's
          result (JSON mode: a handler's ``result`` may embed non-JSON-native
          Pydantic values, e.g. an ``HttpUrl``, which the Temporal payload
          converter cannot encode as-is), or the ``{"cancelled": True}``
          sentinel if the job was cancelled first.
        - A non-``LLMNotConfiguredError`` exception from the handler CALL ITSELF
          is caught and turned into an ``orchestrator:error`` ``AgentAction``
          (success=False) so the job still completes with a degraded response —
          matching the thread path (``handle_request``) instead of failing the
          whole job. A malformed ``intent`` dict (a postcondition violation of
          ``classify_intent_activity``) is NOT part of that handled surface: it
          fails loudly instead of being misreported as a specialist error.
        - Advances the job to progress 30 with ``status_text`` when it runs.
    """
    from llm_service import LLMNotConfiguredError

    from ..core import get_orchestrator
    from ..orchestrator.models import AgentAction, Intent, OrchestratorRequest
    from ..shared.pa_job_store import is_job_cancelled, update_job

    if is_job_cancelled(job_id):
        return dict(_CANCELLED)
    update_job(job_id, status_text=status_text, progress=30)
    orchestrator = get_orchestrator()
    request = OrchestratorRequest(user_id=user_id, message=message, context=context or {})
    handler = getattr(orchestrator, method_name)
    # Reconstructing Intent is a contract check, not specialist work: a bad
    # ``intent`` dict must raise loudly rather than be caught below and
    # misreported as a specialist backend hiccup.
    typed_intent = Intent(**intent)
    try:
        action = handler(request, typed_intent)
    except LLMNotConfiguredError:
        # A missing provider is a configuration failure: fail the run so the UI
        # routes the operator to /llm-config (same as handle_request).
        raise
    except Exception as e:
        # A specialist backend hiccup is handled exactly as the thread path's
        # handle_request handles it — record an error action and let the job
        # continue to a degraded response rather than failing it outright.
        logger.error("Specialist %s failed: %s", method_name, e)
        action = AgentAction(
            agent="orchestrator", action="error", result={"error": str(e)}, success=False
        )
    # mode="json": `action.result` is a `Dict[str, Any]` that specialist
    # handlers populate with arbitrary nested Pydantic model dumps — e.g. the
    # deal-finder's `DealMatch.url` is an `HttpUrl`. A plain `model_dump()`
    # leaves those as non-JSON-native objects (an `HttpUrl`, not a `str`),
    # which the shared Temporal payload converter cannot encode, failing this
    # activity at the boundary. `mode="json"` recursively normalizes them
    # (Pydantic's json-mode serializer converts known non-JSON-native types —
    # HttpUrl, UUID, datetime, etc. — even when nested under an Any-typed
    # field) before this ever reaches the wire.
    return action.model_dump(mode="json")


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
        - Marks the job RUNNING and returns ``Intent.model_dump(mode="json")``,
          or the cancellation sentinel if the job was cancelled.
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
    # mode="json": see the matching note on _run_specialist's return — cheap
    # defense against any future non-JSON-native value landing in `entities`.
    return intent.model_dump(mode="json")


@activity.defn(name="pa_handle_email")
def handle_email_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the email specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the email agent's result,
          or the ``{"cancelled": True}`` sentinel if the job was cancelled
          first, or a degraded ``orchestrator:error`` action on a non-LLM
          handler exception — see :func:`_run_specialist` for the full
          contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Handling email
          request...".
    """
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
    """Run the calendar specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the calendar agent's
          result, or the ``{"cancelled": True}`` sentinel if the job was
          cancelled first, or a degraded ``orchestrator:error`` action on a
          non-LLM handler exception — see :func:`_run_specialist` for the
          full contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Checking your
          calendar...".
    """
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
    """Run the tasks specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the tasks agent's result,
          or the ``{"cancelled": True}`` sentinel if the job was cancelled
          first, or a degraded ``orchestrator:error`` action on a non-LLM
          handler exception — see :func:`_run_specialist` for the full
          contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Managing your
          tasks...".
    """
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
    """Run the deal-finder specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the deal-finder agent's
          result, or the ``{"cancelled": True}`` sentinel if the job was
          cancelled first, or a degraded ``orchestrator:error`` action on a
          non-LLM handler exception — see :func:`_run_specialist` for the
          full contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Searching for
          deals...".
    """
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
    """Run the reservation specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the reservation agent's
          result, or the ``{"cancelled": True}`` sentinel if the job was
          cancelled first, or a degraded ``orchestrator:error`` action on a
          non-LLM handler exception — see :func:`_run_specialist` for the
          full contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Processing
          reservation request...".
    """
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
    """Run the documentation specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the documentation agent's
          result, or the ``{"cancelled": True}`` sentinel if the job was
          cancelled first, or a degraded ``orchestrator:error`` action on a
          non-LLM handler exception — see :func:`_run_specialist` for the
          full contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Generating
          documentation...".
    """
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
    """Run the profile specialist.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the profile agent's
          result, or the ``{"cancelled": True}`` sentinel if the job was
          cancelled first, or a degraded ``orchestrator:error`` action on a
          non-LLM handler exception — see :func:`_run_specialist` for the
          full contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Updating your
          profile...".
    """
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
    """Run the general/fallback handler.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.
        - ``intent`` is a serialized ``Intent`` (``Intent.model_dump()``).

    Postconditions:
        - Returns ``AgentAction.model_dump()`` for the general handler's
          result, or the ``{"cancelled": True}`` sentinel if the job was
          cancelled first, or a degraded ``orchestrator:error`` action on a
          non-LLM handler exception — see :func:`_run_specialist` for the
          full contract shared by all specialist activities.
        - Advances the job to progress 30 with status_text "Processing your
          request...".
    """
    return _run_specialist(
        job_id, user_id, message, context, intent, "_handle_general", _SPECIALIST_STATUS["general"]
    )


@activity.defn(name="pa_check_profile_updates")
def check_profile_updates_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Extract and apply high-confidence profile preferences from the message.

    Preconditions:
        - ``job_id`` refers to a job already created in the PA job store.

    Postconditions:
        - Returns ``None`` (a cancellation signal the workflow short-circuits on)
          WITHOUT running the extraction LLM call, applying preferences, or
          touching progress, when the job was cancelled first.
        - Otherwise applies any high-confidence preferences (side effect on the
          profile store), returns the applied list, and advances progress to 70.
    """
    from ..core import get_orchestrator
    from ..orchestrator.models import OrchestratorRequest
    from ..shared.pa_job_store import is_job_cancelled, update_job

    # Honor a cancellation that landed after the specialist step: don't run the
    # (billed) extraction LLM call or mutate the profile store.
    if is_job_cancelled(job_id):
        return None
    update_job(job_id, status_text="Checking for profile updates...", progress=70)
    request = OrchestratorRequest(user_id=user_id, message=message, context=context or {})
    return get_orchestrator()._check_for_profile_updates(request)


@activity.defn(name="pa_generate_response")
def generate_response_activity(
    job_id: str,
    user_id: str,
    message: str,
    intent: Dict[str, Any],
    actions: List[Dict[str, Any]],
    results: Dict[str, Any],
    profile_updates: Optional[List[Dict[str, Any]]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate the natural-language response from the specialist actions.

    Preconditions:
        - ``intent`` is a serialized ``Intent``; ``actions`` are serialized
          ``AgentAction`` dicts; ``results`` maps an intent key to its result;
          ``profile_updates`` is the list returned by
          ``check_profile_updates_activity`` (or ``None``/empty if none).

    Postconditions:
        - Returns the ``{"cancelled": True}`` sentinel (the workflow then skips
          finalize) WITHOUT running the response LLM call, when the job was
          cancelled first.
        - Otherwise returns ``OrchestratorResponse.model_dump(mode="json")``
          with ``profile_updates`` set to the given list (matching the thread
          path's ``response.profile_updates = profile_updates`` assignment).
          Leaves progress at 85 ("Generating response..."); the next write is
          ``finalize_success_activity``'s own COMPLETED/100 write, which
          follows immediately in the workflow — an intermediate
          progress-100 write here would only be overwritten a moment later,
          so it is skipped as a redundant job-store round-trip.
        - Re-raises ``LLMNotConfiguredError`` (missing provider fails the run).
    """
    from ..core import get_orchestrator
    from ..orchestrator.models import AgentAction, Intent, OrchestratorRequest
    from ..shared.pa_job_store import is_job_cancelled, update_job

    if is_job_cancelled(job_id):
        return dict(_CANCELLED)
    update_job(job_id, status_text="Generating response...", progress=85)
    request = OrchestratorRequest(user_id=user_id, message=message, context=context or {})
    action_objs = [AgentAction(**a) for a in actions]
    response = get_orchestrator()._generate_response(
        request, Intent(**intent), action_objs, results
    )
    response.profile_updates = profile_updates or []
    # mode="json": see the matching note on _run_specialist's return —
    # `response.data` is `results`, which already crossed the wire once as
    # part of a specialist activity's return value, but this keeps the
    # activity boundary safe regardless of that upstream detail.
    return response.model_dump(mode="json")


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
          Temporal recorded completion) cannot double-notify. Delivery is fired
          on a daemon thread (matching ``api.main._run_assistant_job``'s
          equivalent notification) rather than awaited inline, so a slow or
          hanging Slack call cannot hold this activity - and by extension one
          of the worker's few activity-executor slots - open past the point
          where the job is already durably marked COMPLETED.
    """
    from ..models import AssistantResponse
    from ..shared.pa_job_store import (
        PA_JOB_STATUS_CANCELLED,
        PA_JOB_STATUS_COMPLETED,
        get_job,
        update_job,
    )

    # Single read: derive both the cancel-guard and the already-completed
    # check from one job-store snapshot instead of two separate reads
    # (is_job_cancelled makes its own internal get_job call).
    existing = get_job(job_id)
    if existing is not None and existing.get("status") == PA_JOB_STATUS_CANCELLED:
        return
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
    # Fire-and-forget on a daemon thread so this activity isn't held open by
    # Slack's network round-trip after the job is already durably COMPLETED.
    if not already_completed:
        threading.Thread(
            target=_notify_slack,
            args=(user_id, message, assistant_response),
            daemon=True,
        ).start()


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

    ``api.main`` import safety: this is the ONE place in ``temporal/`` that
    imports the FastAPI app module, and only lazily, inside this dormant
    legacy-only activity BODY — never at package import time, so it does not
    threaten the worker's own startup path (which only imports this module,
    not calls it; ``test_temporal_bootstrap.py`` pins the package's
    import-time contract more generally). It is safe in the deployment this
    runs in because the PA Temporal worker
    is always started from ``TEAM_TEMPORAL_WORKER_FUNC`` inside the SAME
    process as ``api.main`` (docker-compose's pa-service boots the worker
    before uvicorn accepts requests — see ``worker.py``'s docstring), so
    ``api.main`` is already imported and cached in ``sys.modules`` by the time
    any pre-decomposition history could reach this activity; this re-import
    is a no-op module-cache hit, not a fresh FastAPI app construction. This
    activity — and the coupling — is deleted entirely once no in-flight
    execution predates the decomposition (see ``_DECOMPOSED_PATCH``'s removal
    criterion in ``workflows.py``), so it is not worth extracting
    ``_run_assistant_job`` into a shared module for what is temporary,
    already-safe-in-practice code.
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
