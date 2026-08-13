"""Dual-path dispatch for the Agent Studio authoring CRUD operations.

The synchronous route handlers call these helpers. When Temporal is configured
(``is_temporal_enabled()``) *and* a live in-process Agent Studio worker is polling
``agent-studio-queue``, each helper starts the matching workflow and blocks for
its result on the worker's event loop (via ``shared.temporal.execute_workflow_sync``),
then rebuilds the Pydantic response the route returns. When Temporal is not
configured, or the Studio worker is disabled/absent, each helper instead calls the corresponding
:class:`~agent_platform.studio.service.AgentStudioService` method directly,
in-process, via the same process-wide singleton
(:func:`agent_platform.studio.runtime.get_studio_service`) the Temporal
activities delegate to — so both paths share one conversation store and identical
business logic. The branch is decided per call, so routes stay unaware of which mode
ran.

Error contract: on the Temporal path, an activity re-shapes the service's
``ValueError``/``LookupError`` as a typed ``ApplicationError`` (see
:mod:`agent_platform.studio.temporal.workflows`); Temporal surfaces that as a
``WorkflowFailureError`` whose cause chain carries the marker.
:func:`_translate_workflow_failure` walks that chain and re-raises the *native*
``ValueError``/``LookupError`` so the route's untouched ``ValueError`` → 400 /
``LookupError`` → 404 mapping still applies. A failure with no such marker is
re-raised as-is (→ 500). On the direct path there is no round-trip to reshape through:
the service already raises those native exceptions directly, so they propagate
unchanged and the same route mapping applies without any translation step.
"""

from __future__ import annotations

import concurrent.futures
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_platform.studio.service import AgentStudioService

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import WorkflowAlreadyStartedError

from agent_platform.registry.models import AgentManifest
from agent_platform.studio.models import AgentDefinition, ConversationStateResponse
from agent_platform.studio.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX_CLONE,
    WORKFLOW_ID_PREFIX_MSG,
    WORKFLOW_ID_PREFIX_SAVE,
    WORKFLOW_ID_PREFIX_START,
    CloneFromRegistryWorkflow,
    SaveAgentWorkflow,
    SendMessageWorkflow,
    StartConversationWorkflow,
)
from shared.temporal import execute_workflow_sync

logger = logging.getLogger(__name__)

# Bounded walk so a cyclic/adversarial cause chain can never loop forever — the same
# bounded-walk pattern the codebase's other Temporal error translators use.
_MAX_CAUSE_DEPTH = 12

# The ApplicationError.type markers the activities stamp, mapped to the native
# exception the route's HTTP handler expects.
_MARKER_EXCEPTIONS: dict[str, type[Exception]] = {
    "ValueError": ValueError,
    "LookupError": LookupError,
}


def _translate_workflow_failure(exc: WorkflowFailureError) -> None:
    """Re-raise the native domain exception a workflow failure carries, if any.

    Walks the standard exception chain (``__cause__`` / ``__context__``) for an
    ``ApplicationError`` whose ``type`` marker names a contract exception
    (``ValueError``/``LookupError``) and re-raises that native exception, so the
    route's HTTP mapping is preserved through Temporal. Temporal surfaces the marker
    either at the top of the chain (the activity raised it directly) or nested under
    an ``ActivityError``; the bounded walk handles both. (Temporal's
    ``FailureError.cause`` is defined as an alias of ``__cause__``, so the standard
    attributes cover both temporalio and plain exceptions.)

    Preconditions:
        - ``exc`` is a ``WorkflowFailureError``.
    Postconditions:
        - Raises the native ``ValueError``/``LookupError`` if a marker is found;
          otherwise returns (the caller re-raises the original failure → 500).
          Never raises anything other than the mapped native exception; bounded and
          cycle-safe.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    depth = 0
    while node is not None and id(node) not in seen and depth < _MAX_CAUSE_DEPTH:
        seen.add(id(node))
        depth += 1
        marker = getattr(node, "type", None)
        native = _MARKER_EXCEPTIONS.get(marker) if isinstance(marker, str) else None
        if native is not None:
            raise native(str(node)) from exc
        node = node.__cause__ or node.__context__


def _temporal_enabled() -> bool:
    """True when authoring CRUD should dispatch to Temporal.

    Requires both a configured Temporal cluster and a live in-process
    ``agent_studio`` worker. When the worker is disabled or absent, CRUD uses
    direct in-process ``AgentStudioService`` instead — other teams may still
    use Temporal in the same process.

    Postconditions:
        - Returns True iff Temporal is configured and ``is_team_worker_alive``
          reports a live ``agent_studio`` worker. Never raises.
    """
    from shared.temporal.client import is_temporal_enabled
    from shared.temporal.worker import is_team_worker_alive

    return is_temporal_enabled() and is_team_worker_alive("agent_studio")


def _direct_service() -> "AgentStudioService":
    """Return the process-wide service singleton for the direct dispatch path.

    Imported lazily (matching the activities' own lazy import of the same getter) so
    tests can monkeypatch ``agent_platform.studio.runtime.get_studio_service``
    and have both the Temporal activities and this direct path pick up the same
    stand-in, rather than binding a stale reference at this module's import time.

    Postconditions:
        - Returns the process-wide ``AgentStudioService`` singleton.
    """
    from agent_platform.studio.runtime import get_studio_service

    return get_studio_service()


def _execute(workflow_run: Callable[..., Any], *args: Any, workflow_id: str) -> Any:
    """Run a workflow to completion, translating a failed run's domain error.

    Preconditions:
        - ``workflow_id`` is unique per call. The public helpers below enforce this by
          minting a fresh ``uuid.uuid4().hex`` per call — there is no runtime assertion,
          so a future caller that reuses a still-live id gets the
          ``WorkflowAlreadyStartedError`` → ``RuntimeError`` path below.
    Postconditions:
        - Returns the workflow result on success. On ``WorkflowFailureError`` first
          re-raises a native ``ValueError``/``LookupError`` when the failure carries
          that marker, else re-raises the original ``WorkflowFailureError``.
        - If the precondition is violated and ``workflow_id`` collides with a still-live
          workflow, Temporal raises ``WorkflowAlreadyStartedError``; that is re-raised as
          a ``RuntimeError`` naming the offending id rather than surfacing the raw
          Temporal error (which the route would not map, yielding an opaque 500).
        - If the workflow does not finish within the dispatch timeout,
          ``execute_workflow_sync`` raises ``concurrent.futures.TimeoutError``; that is
          re-raised as a ``RuntimeError`` naming the workflow, again to avoid an opaque
          500 with an unhelpful message.
    """
    try:
        return execute_workflow_sync(
            workflow_run, *args, workflow_id=workflow_id, task_queue=TASK_QUEUE
        )
    except concurrent.futures.TimeoutError as exc:
        raise RuntimeError(
            f"Agent Studio workflow {workflow_id} did not complete within the dispatch timeout"
        ) from exc
    except WorkflowAlreadyStartedError as exc:
        # Unreachable in normal operation — every dispatch mints a fresh uuid — but
        # execute_workflow_sync documents id-uniqueness as a caller precondition, so a
        # violation surfaces as a clear error instead of an opaque 500.
        raise RuntimeError(
            f"Agent Studio dispatch minted a duplicate live workflow id: {workflow_id}"
        ) from exc
    except WorkflowFailureError as exc:
        _translate_workflow_failure(exc)
        raise


def start_conversation(
    mode: str, source_agent_id: str | None, initial_message: str | None
) -> ConversationStateResponse:
    """Start an authoring conversation, via Temporal when enabled else in-process.

    Postconditions:
        - Returns the initial ``ConversationStateResponse``; raises the service's
          native ``ValueError``/``LookupError`` on a bad request / unknown source, in
          both dispatch modes. The direct path calls
          ``AgentStudioService.start_conversation`` and lets its exceptions propagate
          unchanged — there is no Temporal round-trip to reshape them through.
    """
    if not _temporal_enabled():
        return _direct_service().start_conversation(mode, source_agent_id, initial_message)
    out = _execute(
        StartConversationWorkflow.run,
        mode,
        source_agent_id,
        initial_message,
        workflow_id=f"{WORKFLOW_ID_PREFIX_START}{uuid.uuid4().hex}",
    )
    return ConversationStateResponse.model_validate(out)


def send_message(conversation_id: str, message: str) -> ConversationStateResponse:
    """Send a message, via Temporal when enabled else in-process.

    Postconditions:
        - Returns the updated ``ConversationStateResponse``; raises native
          ``ValueError``/``LookupError`` on invalid input / unknown conversation, in
          both dispatch modes.
    """
    if not _temporal_enabled():
        return _direct_service().send_message(conversation_id, message)
    out = _execute(
        SendMessageWorkflow.run,
        conversation_id,
        message,
        workflow_id=f"{WORKFLOW_ID_PREFIX_MSG}{conversation_id}-{uuid.uuid4().hex}",
    )
    return ConversationStateResponse.model_validate(out)


def clone_from_registry(agent_id: str) -> AgentDefinition:
    """Clone a registered agent into a refine-mode draft, via Temporal when enabled
    else in-process.

    Postconditions:
        - Returns the cloned ``AgentDefinition``; raises native ``LookupError`` when
          ``agent_id`` names no registered agent, in both dispatch modes.
    """
    if not _temporal_enabled():
        return _direct_service().clone_from_registry(agent_id)
    out = _execute(
        CloneFromRegistryWorkflow.run,
        agent_id,
        workflow_id=f"{WORKFLOW_ID_PREFIX_CLONE}{agent_id}-{uuid.uuid4().hex}",
    )
    return AgentDefinition.model_validate(out)


def save_agent(definition: AgentDefinition) -> tuple[AgentManifest, bool]:
    """Save + register a definition, via Temporal when enabled else in-process.

    Mirrors ``AgentStudioService.save_agent``'s return shape so the route stays
    structurally identical regardless of dispatch mode.

    Postconditions:
        - Returns ``(AgentManifest, created)``; raises native ``ValueError`` when the
          definition is not ready to save, in both dispatch modes.
    """
    if not _temporal_enabled():
        return _direct_service().save_agent(definition)
    out = _execute(
        SaveAgentWorkflow.run,
        definition.model_dump(),
        workflow_id=f"{WORKFLOW_ID_PREFIX_SAVE}{uuid.uuid4().hex}",
    )
    return AgentManifest.model_validate(out["manifest"]), out["created"]
