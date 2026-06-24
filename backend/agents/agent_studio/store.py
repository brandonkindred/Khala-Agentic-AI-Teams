"""In-process conversation store for the Agent Studio build assistant.

Authoring conversations are held in memory for the lifetime of the process.
Durable / cross-process persistence is a tracked follow-up — the same caveat the
generated-agent registry carries (``agentic_team_provisioning.manifest_generation``).
Kept deliberately small so it is trivially swappable for a Postgres-backed store
later without touching the service or routes.

Invariants:
    * A ``conversation_id`` returned by :meth:`create` resolves via :meth:`get`
      until the process exits; ids are never reused.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .models import AgentDefinition, ConversationMessage, StudioMode


@dataclass
class ConversationRecord:
    conversation_id: str
    mode: StudioMode
    source_agent_id: str | None
    definition: AgentDefinition
    messages: list[ConversationMessage] = field(default_factory=list)


class AgentStudioConversationStore:
    def __init__(self) -> None:
        self._records: dict[str, ConversationRecord] = {}

    def create(
        self, mode: StudioMode, source_agent_id: str | None, definition: AgentDefinition
    ) -> str:
        """Create a conversation and return its id.

        Postconditions:
            * ``get(returned_id)`` is a fresh record with no messages.
        """
        conversation_id = str(uuid.uuid4())
        self._records[conversation_id] = ConversationRecord(
            conversation_id=conversation_id,
            mode=mode,
            source_agent_id=source_agent_id,
            definition=definition,
        )
        return conversation_id

    def get(self, conversation_id: str) -> ConversationRecord | None:
        """Return the record, or ``None`` if the id is unknown."""
        return self._records.get(conversation_id)

    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        """Append one message.

        Preconditions:
            * ``conversation_id`` exists (caller validated it via :meth:`get`).
        """
        record = self._records[conversation_id]
        record.messages.append(ConversationMessage(role=role, content=content))

    def set_definition(self, conversation_id: str, definition: AgentDefinition) -> None:
        """Replace the in-progress definition for a conversation."""
        self._records[conversation_id].definition = definition
