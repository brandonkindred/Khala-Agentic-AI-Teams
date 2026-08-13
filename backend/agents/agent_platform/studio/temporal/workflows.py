"""Temporal workflows + activities for the Agent Studio team.

Kept in its own module (separate from the package ``__init__``) so the temporalio
workflow sandbox can re-import the workflow classes without executing any
non-deterministic top-level code. Top-level imports here are limited to
``datetime`` / ``typing`` / ``temporalio``; the service and the pydantic models are
imported **lazily inside the activity bodies**, so importing this module never pulls
in ``strands``/LLM/Postgres machinery and never calls ``os.getenv``.

Each activity delegates to the existing :class:`~agent_platform.studio.service.AgentStudioService`
method — the same code path the team always used, now reached only through an
activity (no duplicated business logic). The service's error contract
(``ValueError`` / ``LookupError``) is re-shaped at the activity boundary into a typed,
non-retryable :class:`~temporalio.exceptions.ApplicationError` so the dispatch layer
can rebuild the native exception and the route's ``ValueError`` → 400 /
``LookupError`` → 404 mapping survives the Temporal round-trip.

Activities deliberately do NOT call any ``temporalio.activity`` runtime helpers
(``heartbeat``/``logger``) so they remain directly callable in unit tests; they log
through a plain module logger instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)

# Each op is a single, short activity. Cap the run at three minutes — long enough for
# an authoring LLM turn, bounded so a stuck provider surfaces as a failure.
ACTIVITY_TIMEOUT = timedelta(seconds=180)

# maximum_attempts=1: these activities invoke the authoring LLM (start-with-initial-
# message, send-message) and mutate the registry (save). A workflow-level retry would
# double-charge the LLM or re-register; the interactive contract wants a single
# attempt, with any failure surfaced to the caller. Mirrors market_research's cap.
SINGLE_ATTEMPT = RetryPolicy(maximum_attempts=1)


def _to_application_error(exc: Exception) -> ApplicationError:
    """Re-shape a service ``ValueError``/``LookupError`` as a typed ApplicationError.

    The ``type`` marker lets the dispatch layer rebuild the native exception,
    preserving the route's ``ValueError`` → 400 / ``LookupError`` → 404 contract
    through Temporal. ``non_retryable=True`` because these are caller errors (bad input
    / unknown id) — retrying cannot help.

    The marker is the matching **base** contract name, not ``type(exc).__name__``: a
    ``ValueError``/``LookupError`` *subclass* (which the activities' ``except
    (ValueError, LookupError)`` still catches) must map to ``"ValueError"``/
    ``"LookupError"`` so the dispatch layer translates it back to 400/404 rather than
    falling through to a 500 on an unrecognized subclass name.

    Preconditions:
        - ``exc`` is a ``ValueError`` or ``LookupError`` (or subclass) — the activities
          only call this from an ``except (ValueError, LookupError)`` clause.
    Postconditions:
        - Returns an ``ApplicationError`` whose ``type`` is ``"ValueError"`` when
          ``exc`` is a ``ValueError`` (family) else ``"LookupError"``.
    """
    marker = "ValueError" if isinstance(exc, ValueError) else "LookupError"
    return ApplicationError(str(exc), type=marker, non_retryable=True)


# ── Activities ──────────────────────────────────────────────────────────────────


@activity.defn(name="agent_studio_start_conversation")
def start_conversation_activity(
    mode: str, source_agent_id: str | None, initial_message: str | None
) -> dict[str, Any]:
    """Run ``AgentStudioService.start_conversation``; return the state dict.

    Preconditions:
        - Args mirror ``StartConversationRequest`` (validated by the route).
    Postconditions:
        - Returns ``ConversationStateResponse.model_dump()`` on success.
    Raises:
        - ``ApplicationError`` (``type`` = ``"ValueError"``/``"LookupError"``,
          non-retryable) translated from the service's error contract; any other
          exception propagates unchanged.
    """
    from agent_platform.studio.runtime import get_studio_service

    try:
        response = get_studio_service().start_conversation(mode, source_agent_id, initial_message)
    except (ValueError, LookupError) as exc:
        raise _to_application_error(exc) from exc
    return response.model_dump()


@activity.defn(name="agent_studio_send_message")
def send_message_activity(conversation_id: str, message: str) -> dict[str, Any]:
    """Run ``AgentStudioService.send_message``; return the updated state dict.

    Preconditions:
        - ``conversation_id`` and ``message`` mirror the route's inputs.
    Postconditions:
        - Returns ``ConversationStateResponse.model_dump()`` on success.
    Raises:
        - ``ApplicationError`` (typed, non-retryable) for a service
          ``ValueError``/``LookupError``; other exceptions propagate unchanged.
    """
    from agent_platform.studio.runtime import get_studio_service

    try:
        response = get_studio_service().send_message(conversation_id, message)
    except (ValueError, LookupError) as exc:
        raise _to_application_error(exc) from exc
    return response.model_dump()


@activity.defn(name="agent_studio_clone_from_registry")
def clone_from_registry_activity(agent_id: str) -> dict[str, Any]:
    """Run ``AgentStudioService.clone_from_registry``; return the draft dict.

    Preconditions:
        - ``agent_id`` names the source agent to clone.
    Postconditions:
        - Returns ``AgentDefinition.model_dump()`` on success.
    Raises:
        - ``ApplicationError`` (typed, non-retryable) for a service ``LookupError``
          (unknown source id); other exceptions propagate unchanged.
    """
    from agent_platform.studio.runtime import get_studio_service

    try:
        definition = get_studio_service().clone_from_registry(agent_id)
    except (ValueError, LookupError) as exc:
        raise _to_application_error(exc) from exc
    return definition.model_dump()


@activity.defn(name="agent_studio_save_agent")
def save_agent_activity(definition: dict[str, Any]) -> dict[str, Any]:
    """Run ``AgentStudioService.save_agent``; return the registered manifest + flag.

    Preconditions:
        - ``definition`` is an ``AgentDefinition.model_dump()`` dict.
    Postconditions:
        - Returns ``{"manifest": AgentManifest.model_dump(), "created": bool}`` on
          success.
    Raises:
        - ``ApplicationError`` (typed, non-retryable) for a service ``ValueError``
          (definition not ready); other exceptions propagate unchanged.
    """
    from agent_platform.studio.models import AgentDefinition
    from agent_platform.studio.runtime import get_studio_service

    parsed = AgentDefinition.model_validate(definition)
    try:
        manifest, created = get_studio_service().save_agent(parsed)
    except (ValueError, LookupError) as exc:
        raise _to_application_error(exc) from exc
    return {"manifest": manifest.model_dump(), "created": created}


# ── Workflows ───────────────────────────────────────────────────────────────────


@workflow.defn(name="AgentStudioStartConversationWorkflow")
class StartConversationWorkflow:
    @workflow.run
    async def run(
        self, mode: str, source_agent_id: str | None, initial_message: str | None
    ) -> dict[str, Any]:
        """Durable entrypoint: start an authoring conversation.

        Postconditions:
            - Delegates to ``start_conversation_activity`` and returns its
              ``ConversationStateResponse`` dict.
        """
        return await workflow.execute_activity(
            start_conversation_activity,
            args=[mode, source_agent_id, initial_message],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=SINGLE_ATTEMPT,
        )


@workflow.defn(name="AgentStudioSendMessageWorkflow")
class SendMessageWorkflow:
    @workflow.run
    async def run(self, conversation_id: str, message: str) -> dict[str, Any]:
        """Durable entrypoint: run one authoring turn.

        Postconditions:
            - Delegates to ``send_message_activity`` and returns its
              ``ConversationStateResponse`` dict.
        """
        return await workflow.execute_activity(
            send_message_activity,
            args=[conversation_id, message],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=SINGLE_ATTEMPT,
        )


@workflow.defn(name="AgentStudioCloneFromRegistryWorkflow")
class CloneFromRegistryWorkflow:
    @workflow.run
    async def run(self, agent_id: str) -> dict[str, Any]:
        """Durable entrypoint: clone a registered agent into a refine draft.

        Postconditions:
            - Delegates to ``clone_from_registry_activity`` and returns its
              ``AgentDefinition`` dict.
        """
        return await workflow.execute_activity(
            clone_from_registry_activity,
            args=[agent_id],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=SINGLE_ATTEMPT,
        )


@workflow.defn(name="AgentStudioSaveAgentWorkflow")
class SaveAgentWorkflow:
    @workflow.run
    async def run(self, definition: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: save + register a finished definition.

        Postconditions:
            - Delegates to ``save_agent_activity`` and returns its
              ``{"manifest": ..., "created": ...}`` dict.
        """
        return await workflow.execute_activity(
            save_agent_activity,
            args=[definition],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=SINGLE_ATTEMPT,
        )
