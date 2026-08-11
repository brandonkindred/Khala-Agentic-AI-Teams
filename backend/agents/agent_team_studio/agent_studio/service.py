"""Orchestration for the Agent Studio Stage-1 build flow.

Wires the authoring assistant, the conversation store, and clone/save+register
into the operations the routes expose. All registry access goes through an
injected getter so tests can supply an isolated registry.

Error contract (translated to HTTP by the routes):
    * :class:`ValueError`  -> bad request (missing/invalid input).
    * :class:`LookupError` -> not found (unknown conversation or source agent).
"""

from __future__ import annotations

from typing import Callable, Protocol

from agent_registry.models import AgentManifest

from .assistant import DEFAULT_SUGGESTIONS, GREETING, AgentDesignerAgent
from .models import AgentDefinition, ConversationStateResponse, StudioMode
from .registration import build_studio_agent_manifest, clone_from_manifest
from .store import AgentStudioConversationStore


class RegistryLike(Protocol):
    """The slice of ``agent_registry.AgentRegistry`` this service depends on."""

    def get(self, agent_id: str) -> AgentManifest | None: ...

    def register(self, manifest: AgentManifest) -> None: ...


# A getter returning the process-wide ``AgentRegistry`` (or a test double).
RegistryGetter = Callable[[], RegistryLike]


def _default_registry_getter() -> RegistryLike:  # pragma: no cover - thin import wrapper
    from agent_registry import get_registry

    return get_registry()


class AgentStudioService:
    def __init__(
        self,
        *,
        assistant: AgentDesignerAgent | None = None,
        store: AgentStudioConversationStore | None = None,
        registry_getter: RegistryGetter | None = None,
    ) -> None:
        # Use explicit None checks, not ``or`` — an empty store is falsy now that
        # the store defines ``__len__``, so ``store or ...`` would discard a
        # passed-in empty store.
        self._assistant = assistant if assistant is not None else AgentDesignerAgent()
        self._store = store if store is not None else AgentStudioConversationStore()
        self._get_registry = (
            registry_getter if registry_getter is not None else _default_registry_getter
        )

    # ── Conversations ──────────────────────────────────────────────────────────

    def start_conversation(
        self,
        mode: StudioMode,
        source_agent_id: str | None,
        initial_message: str | None,
    ) -> ConversationStateResponse:
        """Start an authoring conversation in ``new`` or ``refine`` mode.

        Preconditions:
            * ``mode == "refine"`` requires ``source_agent_id`` to name a
              registered agent.
            * ``mode == "new"`` must **not** carry a ``source_agent_id`` (a new
              build has no source; passing one is a client error, not silently
              ignored).
        Postconditions:
            * Returns the initial conversation state. In ``refine`` mode the
              definition is pre-seeded from a clone of the source manifest.
            * If the first turn fails (an ``initial_message`` whose assistant call
              raises), the just-created conversation is discarded before the error
              propagates, so a failed start leaves no orphaned empty record —
              matching ``send_message``'s consistent-state-on-failure property.
        """
        if mode == "refine":
            if not source_agent_id:
                raise ValueError("source_agent_id is required when mode == 'refine'")
            manifest = self._get_registry().get(source_agent_id)
            if manifest is None:
                raise LookupError(f"Unknown source agent: {source_agent_id}")
            definition = clone_from_manifest(manifest)
        else:
            if source_agent_id:
                raise ValueError("source_agent_id must not be provided when mode == 'new'")
            definition = AgentDefinition(mode="new")

        conversation_id = self._store.create(mode, source_agent_id, definition)
        if initial_message:
            try:
                return self._handle_message(conversation_id, initial_message)
            except Exception:
                # Roll back the just-created conversation so a failed first turn
                # doesn't leak an orphaned empty record, then re-raise unchanged.
                self._store.discard(conversation_id)
                raise

        self._store.append_message(conversation_id, "assistant", GREETING[mode])
        return self._state(conversation_id, suggestions=list(DEFAULT_SUGGESTIONS))

    def send_message(self, conversation_id: str, message: str) -> ConversationStateResponse:
        """Send a user message and return the updated conversation state.

        Preconditions:
            * ``conversation_id`` names an existing conversation — otherwise the
              turn raises :class:`LookupError` (→ 404); the check lives in
              ``store.turn`` so it's atomic with taking the turn lock rather than a
              separate round trip that could race.
        """
        return self._handle_message(conversation_id, message)

    def _handle_message(self, conversation_id: str, message: str) -> ConversationStateResponse:
        """Run one assistant turn: read state, call the LLM, persist user + reply.

        The whole turn (read history+draft → LLM → append → set_draft)
        runs inside ``store.turn(...)``, which serializes it against a concurrent
        send on the *same* conversation — a per-conversation lock in-memory, or a
        ``SELECT … FOR UPDATE`` row lock with the durable store. A second concurrent
        send blocks until this turn commits, then reads fresh state, so there is no
        lost definition update or out-of-order transcript. Across the 4 uvicorn
        workers the durable store makes this coherent (the in-memory store applies
        within a single worker).

        The assistant is called *before* any write, so if it raises, the
        conversation isn't left with a dangling user message and no reply — the turn
        rolls back / no-ops (consistent state on failure / retry).
        """
        with self._store.turn(conversation_id) as turn:
            reply, updated, suggestions = self._assistant.respond(turn.history, turn.draft, message)
            turn.append_message("user", message)
            turn.append_message("assistant", reply)
            if updated is not None:
                turn.set_draft(updated)
        return self._state(conversation_id, suggestions=suggestions)

    # ── Clone / Save ───────────────────────────────────────────────────────────

    def clone_from_registry(self, agent_id: str) -> AgentDefinition:
        """Return a refine-mode draft cloned from a registered agent.

        Preconditions:
            * ``agent_id`` names a registered agent.
        """
        manifest = self._get_registry().get(agent_id)
        if manifest is None:
            raise LookupError(f"Unknown source agent: {agent_id}")
        return clone_from_manifest(manifest)

    def save_agent(self, definition: AgentDefinition) -> tuple[AgentManifest, bool]:
        """Build, register, and return the manifest for a finished definition.

        The registry id is derived from ``definition.name`` (see
        :func:`registration.studio_agent_id`), so the agent **name is its
        identity**: re-saving a definition with the same name updates that agent
        in place rather than creating a second entry. To keep that overwrite from
        being silent, the returned ``created`` flag tells the caller whether a new
        agent was registered (``True``) or an existing same-id one was replaced
        (``False``) — the UI surfaces this so a same-name save is never a silent
        clobber.

        Concurrency: the get-then-register below is **not** atomic, so under two
        concurrent saves of the same name the ``created`` flags can both read
        ``True``. This is benign — ``created`` is an advisory UI hint (warn before a
        same-name overwrite), not a correctness guarantee, and ``register`` is
        idempotent by id so the final stored manifest is consistent regardless. A
        truly atomic flag needs the shared ``agent_registry`` to return creation
        status from ``register`` itself; that's a registry-level change, out of scope
        for this Stage-1 slice and in the same deferred concurrency class as the
        per-conversation turn serialization.

        Preconditions:
            * ``definition`` is ready (``definition.missing_required()`` is empty).
        Postconditions:
            * The manifest is registered (resolvable via ``get(manifest.id)``).
              Returns ``(manifest, created)`` where ``created`` is ``True`` iff no
              agent with ``manifest.id`` existed before this call (subject to the
              concurrency caveat above).
        """
        missing = definition.missing_required()
        if missing:
            raise ValueError(
                f"Agent is not ready to save — missing required fields: {', '.join(missing)}"
            )
        manifest = build_studio_agent_manifest(definition)
        registry = self._get_registry()
        created = registry.get(manifest.id) is None
        registry.register(manifest)
        return manifest, created

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _state(self, conversation_id: str, *, suggestions: list[str]) -> ConversationStateResponse:
        record = self._store.get(conversation_id)
        if record is None:  # defensive guard; callers always create/validate first
            raise RuntimeError("Conversation record unexpectedly missing")  # pragma: no cover
        return ConversationStateResponse(
            conversation_id=conversation_id,
            mode=record.mode,
            messages=list(record.messages),
            definition=record.definition,
            readiness=record.definition.missing_required(),
            suggested_questions=suggestions,
        )
