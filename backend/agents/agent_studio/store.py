"""In-process conversation store for the Agent Studio build assistant.

Authoring conversations are held in memory for the lifetime of the process.
Durable / cross-process persistence is a tracked follow-up — the same caveat the
generated-agent registry carries (``agentic_team_provisioning.manifest_generation``).
Kept deliberately small so it is trivially swappable for a Postgres-backed store
later without touching the service or routes.

The store is **bounded**: it retains at most ``max_conversations`` records and
evicts the oldest (FIFO) on overflow, so an unbounded stream of
``POST /conversations`` calls cannot grow process memory without limit. Eviction
is the in-memory analogue of the TTL the durable store will carry.

Invariants:
    * A ``conversation_id`` returned by :meth:`create` resolves via :meth:`get`
      until it is evicted by the FIFO cap or the process exits; ids are never
      reused.
    * ``len(self._records) <= max_conversations`` holds after every operation.
"""

from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from .models import AgentDefinition, ConversationMessage, StudioMode


def _default_max_conversations() -> int:
    """Resolve the cap from ``AGENT_STUDIO_MAX_CONVERSATIONS`` (defensive parse).

    Postconditions:
        * Returns a positive int; missing/garbage/non-positive values fall back to
          the documented default of 1000.
    """
    try:
        value = int(os.getenv("AGENT_STUDIO_MAX_CONVERSATIONS", ""))
    except (TypeError, ValueError):
        return 1000
    return value if value > 0 else 1000


@dataclass
class ConversationRecord:
    conversation_id: str
    mode: StudioMode
    source_agent_id: str | None
    definition: AgentDefinition
    messages: list[ConversationMessage] = field(default_factory=list)


class AgentStudioConversationStore:
    def __init__(self, max_conversations: int | None = None) -> None:
        """Create a bounded in-memory store.

        Preconditions:
            * ``max_conversations`` is ``None`` (resolve from env) or a positive int.
        """
        resolved = (
            max_conversations if max_conversations is not None else _default_max_conversations()
        )
        assert resolved > 0, "max_conversations must be positive"
        self._max = resolved
        # OrderedDict so the oldest entry is cheap to evict (FIFO).
        self._records: OrderedDict[str, ConversationRecord] = OrderedDict()

    def create(
        self, mode: StudioMode, source_agent_id: str | None, definition: AgentDefinition
    ) -> str:
        """Create a conversation and return its id.

        Postconditions:
            * ``get(returned_id)`` is a fresh record with no messages.
            * At most ``max_conversations`` records remain; on overflow the oldest
              record is evicted and no longer resolves via :meth:`get`.
        """
        conversation_id = str(uuid.uuid4())
        self._records[conversation_id] = ConversationRecord(
            conversation_id=conversation_id,
            mode=mode,
            source_agent_id=source_agent_id,
            definition=definition,
        )
        while len(self._records) > self._max:
            self._records.popitem(last=False)
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
