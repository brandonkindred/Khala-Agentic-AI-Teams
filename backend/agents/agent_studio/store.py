"""In-process conversation store for the Agent Studio build assistant.

Authoring conversations are held in memory for the lifetime of the process.
Durable / cross-process persistence is a tracked follow-up — the same caveat the
generated-agent registry carries (``agentic_team_provisioning.manifest_generation``).
Kept deliberately small so it is trivially swappable for a Postgres-backed store
later without touching the service or routes.

The store is **bounded**: it retains at most ``max_conversations`` records and
evicts the **least-recently-used** on overflow (any access — get / append /
set_definition — refreshes recency via ``OrderedDict.move_to_end``), so an
unbounded stream of ``POST /conversations`` calls cannot grow process memory
without limit, and a conversation that's still in active use is not evicted out
from under a mid-turn request. Eviction is the in-memory analogue of the TTL the
durable store will carry.

The store is **thread-safe**: every mutating/reading method holds a
``threading.Lock``, so it is safe to share one instance across the FastAPI
threadpool (sync handlers run there). The lock makes each individual operation
atomic; serializing a whole multi-call conversation turn (the service's
``_handle_message`` sequence) is a separate concern that arrives with the durable
store and is out of scope here.

Invariants:
    * A ``conversation_id`` returned by :meth:`create` resolves via :meth:`get`
      until it is evicted by the LRU cap or the process exits; ids are never
      reused.
    * ``len(self) <= max_conversations`` holds after every operation.
    * All access to ``self._records`` happens while holding ``self._lock``.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace

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
        if resolved <= 0:
            raise ValueError(f"max_conversations must be positive, got {resolved}")
        self._max = resolved
        # OrderedDict so the oldest entry is cheap to evict (FIFO).
        self._records: OrderedDict[str, ConversationRecord] = OrderedDict()
        # Guards every read/write of ``_records`` (shared across the threadpool).
        self._lock = threading.Lock()

    def create(
        self, mode: StudioMode, source_agent_id: str | None, definition: AgentDefinition
    ) -> str:
        """Create a conversation and return its id.

        Postconditions:
            * ``get(returned_id)`` is a fresh record with no messages.
            * At most ``max_conversations`` records remain; on overflow the
              **least-recently-used** record is evicted (see eviction note).
        """
        conversation_id = str(uuid.uuid4())
        with self._lock:
            self._records[conversation_id] = ConversationRecord(
                conversation_id=conversation_id,
                mode=mode,
                source_agent_id=source_agent_id,
                definition=definition,
            )
            while len(self._records) > self._max:
                self._records.popitem(last=False)  # front == least-recently-used
        return conversation_id

    def get(self, conversation_id: str) -> ConversationRecord | None:
        """Return a snapshot of the record, or ``None`` if the id is unknown.

        Accessing a conversation marks it **most-recently-used** so the LRU cap
        never evicts a conversation that's still in active use (e.g. mid-turn,
        awaiting an LLM response).

        Postconditions:
            * The returned record is an **independent copy** — its own ``messages``
              list and a deep-copied ``definition`` — so callers never hold a
              reference to internal mutable state past the lock; mutating it can't
              race with concurrent ``append_message`` / ``set_definition``.
              Mutations must go through the store's methods. (The ``messages`` list
              is copied; its ``ConversationMessage`` elements are shared but
              **frozen**, so they can't be mutated through the snapshot.)
        """
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                return None
            self._records.move_to_end(conversation_id)
            return replace(
                record,
                messages=list(record.messages),
                definition=record.definition.model_copy(deep=True),
            )

    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        """Append one message (marks the conversation most-recently-used).

        Preconditions:
            * ``conversation_id`` exists.
        Postconditions:
            * Raises :class:`LookupError` (→ 404) if the id is unknown, matching
              the service's error contract, rather than a bare ``KeyError`` (→ 500).
        """
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                raise LookupError(f"Unknown conversation: {conversation_id}")
            record.messages.append(ConversationMessage(role=role, content=content))
            self._records.move_to_end(conversation_id)

    def set_definition(self, conversation_id: str, definition: AgentDefinition) -> None:
        """Replace the in-progress definition (marks the conversation most-recently-used).

        Postconditions:
            * Raises :class:`LookupError` if the id is unknown (see
              :meth:`append_message`).
        """
        with self._lock:
            record = self._records.get(conversation_id)
            if record is None:
                raise LookupError(f"Unknown conversation: {conversation_id}")
            record.definition = definition
            self._records.move_to_end(conversation_id)

    def discard(self, conversation_id: str) -> None:
        """Remove a conversation if present; a no-op when the id is unknown.

        Used to roll back a conversation whose very first turn failed, so a
        partially-started conversation isn't orphaned in the store.

        Postconditions:
            * ``get(conversation_id)`` returns ``None`` afterward.
            * Idempotent: discarding an unknown (or already-discarded) id does
              nothing rather than raising — unlike :meth:`append_message` /
              :meth:`set_definition`, since cleanup must never mask the original
              failure with a second exception.
        """
        with self._lock:
            self._records.pop(conversation_id, None)

    def __len__(self) -> int:
        """Number of live conversations (public read of the cap-bounded size)."""
        with self._lock:
            return len(self._records)
