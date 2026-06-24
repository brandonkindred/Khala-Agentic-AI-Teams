"""Orchestration for the Agent Studio Stage-1 build flow.

Wires the authoring assistant, the conversation store, and clone/save+register
into the operations the routes expose. All registry access goes through an
injected getter so tests can supply an isolated registry.

Error contract (translated to HTTP by the routes):
    * :class:`ValueError`  -> bad request (missing/invalid input).
    * :class:`LookupError` -> not found (unknown conversation or source agent).
"""

from __future__ import annotations

from typing import Callable

from agent_registry.models import AgentManifest

from .assistant import DEFAULT_SUGGESTIONS, GREETING, AgentDesignerAgent
from .models import AgentDefinition, ConversationStateResponse, StudioMode
from .registration import build_studio_agent_manifest, clone_from_manifest
from .store import AgentStudioConversationStore

# A getter returning an object with ``.get(id)`` and ``.register(manifest)`` —
# i.e. the process-wide ``AgentRegistry``.
RegistryGetter = Callable[[], object]


def _default_registry_getter() -> object:  # pragma: no cover - thin import wrapper
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
        self._assistant = assistant or AgentDesignerAgent()
        self._store = store or AgentStudioConversationStore()
        self._get_registry = registry_getter or _default_registry_getter

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
        Postconditions:
            * Returns the initial conversation state. In ``refine`` mode the
              definition is pre-seeded from a clone of the source manifest.
        """
        if mode == "refine":
            if not source_agent_id:
                raise ValueError("source_agent_id is required when mode == 'refine'")
            manifest = self._get_registry().get(source_agent_id)  # type: ignore[attr-defined]
            if manifest is None:
                raise LookupError(f"Unknown source agent: {source_agent_id}")
            definition = clone_from_manifest(manifest)
        else:
            definition = AgentDefinition(mode="new")

        conversation_id = self._store.create(mode, source_agent_id, definition)
        if initial_message:
            return self._handle_message(conversation_id, initial_message)

        self._store.append_message(conversation_id, "assistant", GREETING[mode])
        return self._state(conversation_id, suggestions=list(DEFAULT_SUGGESTIONS))

    def send_message(self, conversation_id: str, message: str) -> ConversationStateResponse:
        """Send a user message and return the updated conversation state.

        Preconditions:
            * ``conversation_id`` names an existing conversation.
        """
        if self._store.get(conversation_id) is None:
            raise LookupError(f"Unknown conversation: {conversation_id}")
        return self._handle_message(conversation_id, message)

    def _handle_message(self, conversation_id: str, message: str) -> ConversationStateResponse:
        record = self._store.get(conversation_id)
        assert record is not None  # callers validate first
        history = [(m.role, m.content) for m in record.messages]
        self._store.append_message(conversation_id, "user", message)

        reply, updated, suggestions = self._assistant.respond(history, record.definition, message)
        self._store.append_message(conversation_id, "assistant", reply)
        if updated is not None:
            self._store.set_definition(conversation_id, updated)
        return self._state(conversation_id, suggestions=suggestions)

    # ── Clone / Save ───────────────────────────────────────────────────────────

    def clone_from_registry(self, agent_id: str) -> AgentDefinition:
        """Return a refine-mode draft cloned from a registered agent.

        Preconditions:
            * ``agent_id`` names a registered agent.
        """
        manifest = self._get_registry().get(agent_id)  # type: ignore[attr-defined]
        if manifest is None:
            raise LookupError(f"Unknown source agent: {agent_id}")
        return clone_from_manifest(manifest)

    def save_agent(self, definition: AgentDefinition) -> AgentManifest:
        """Build, register, and return the manifest for a finished definition.

        Preconditions:
            * ``definition`` is ready (``definition.missing_required()`` is empty).
        Postconditions:
            * The manifest is registered (resolvable via ``get(manifest.id)``) and
              invokable via the shared generated-agent runtime.
        """
        missing = definition.missing_required()
        if missing:
            raise ValueError(
                f"Agent is not ready to save — missing required fields: {', '.join(missing)}"
            )
        manifest = build_studio_agent_manifest(definition)
        self._get_registry().register(manifest)  # type: ignore[attr-defined]
        return manifest

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _state(self, conversation_id: str, *, suggestions: list[str]) -> ConversationStateResponse:
        record = self._store.get(conversation_id)
        assert record is not None
        return ConversationStateResponse(
            conversation_id=conversation_id,
            mode=record.mode,
            messages=list(record.messages),
            definition=record.definition,
            readiness=record.definition.missing_required(),
            suggested_questions=suggestions,
        )
