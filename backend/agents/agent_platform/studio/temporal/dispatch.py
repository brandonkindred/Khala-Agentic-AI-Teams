"""In-process dispatch for the Agent Studio authoring CRUD operations.

The synchronous route handlers call these helpers. Each helper delegates to the
matching :class:`~agent_platform.studio.service.AgentStudioService` method on the
process-wide singleton (:func:`agent_platform.studio.runtime.get_studio_service`).
Authoring CRUD (start conversation, send message, clone, save) does **not** start
Temporal workflows: the former 1-activity wrappers are gone, so a configured
Temporal cluster or an in-process ``agent_studio`` worker is never required for
these paths. Native ``ValueError`` / ``LookupError`` from the service propagate
unchanged; the route maps them to 400 / 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_platform.studio.service import AgentStudioService

from agent_platform.registry.models import AgentManifest
from agent_platform.studio.models import AgentDefinition, ConversationStateResponse


def _direct_service() -> "AgentStudioService":
    """Return the process-wide service singleton.

    Imported lazily so tests can monkeypatch
    ``agent_platform.studio.runtime.get_studio_service`` and have this path pick
    up the stand-in, rather than binding a stale reference at import time.

    Preconditions:
        - None.
    Postconditions:
        - Returns the process-wide ``AgentStudioService`` singleton.
    """
    from agent_platform.studio.runtime import get_studio_service

    return get_studio_service()


def start_conversation(
    mode: str, source_agent_id: str | None, initial_message: str | None
) -> ConversationStateResponse:
    """Start an authoring conversation in-process.

    Preconditions:
        - Arguments match ``AgentStudioService.start_conversation``.
    Postconditions:
        - Returns the initial ``ConversationStateResponse``; raises the service's
          native ``ValueError``/``LookupError`` on a bad request / unknown source.
    """
    return _direct_service().start_conversation(mode, source_agent_id, initial_message)


def send_message(conversation_id: str, message: str) -> ConversationStateResponse:
    """Send a message in-process.

    Preconditions:
        - Arguments match ``AgentStudioService.send_message``.
    Postconditions:
        - Returns the updated ``ConversationStateResponse``; raises native
          ``ValueError``/``LookupError`` on invalid input / unknown conversation.
    """
    return _direct_service().send_message(conversation_id, message)


def clone_from_registry(agent_id: str) -> AgentDefinition:
    """Clone a registered agent into a refine-mode draft in-process.

    Preconditions:
        - ``agent_id`` is the registry id to clone.
    Postconditions:
        - Returns the cloned ``AgentDefinition``; raises native ``LookupError``
          when ``agent_id`` names no registered agent.
    """
    return _direct_service().clone_from_registry(agent_id)


def save_agent(definition: AgentDefinition) -> tuple[AgentManifest, bool]:
    """Save + register a definition in-process.

    Mirrors ``AgentStudioService.save_agent``'s return shape so the route stays
    structurally identical.

    Preconditions:
        - ``definition`` is an ``AgentDefinition``.
    Postconditions:
        - Returns ``(AgentManifest, created)``; raises native ``ValueError`` when
          the definition is not ready to save.
    """
    return _direct_service().save_agent(definition)
