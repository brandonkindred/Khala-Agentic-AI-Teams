"""Pydantic models for the Agent Studio Stage-1 build flow.

The central type is :class:`AgentDefinition` — the single-agent definition the
authoring assistant co-authors, the clone endpoint produces, and the save
endpoint consumes. All other models are request/response envelopes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from agent_platform.registry.models import AgentManifest

from ..assistant_kernel import ConversationMessage
from .agent_states import default_agent_states, normalize_agent_states

# The two authoring modes. ``new`` builds from scratch; ``refine`` starts from a
# clone of an existing registry agent (``cloned_from`` records the source id).
StudioMode = Literal["new", "refine"]

# The three operating "states of being" every authored agent is seeded with. The
# key set is fixed (see ``agent_states.STATE_ORDER``); only a state's prompt is
# editable. ``Literal`` here is the authoring-time guard that locks the keys.
AgentStateKey = Literal["planning", "executing", "researching"]


class AgentState(BaseModel):
    """One behavioral operating state seeded onto an authored agent.

    Invariants:
        * ``key`` is one of the three fixed ``AgentStateKey`` literals — the merge
          identity. The model can refine ``system_prompt`` but can never add,
          remove, or rename a state (an invalid key fails validation).
    """

    key: AgentStateKey
    label: str
    system_prompt: str


# A states list that is normalized on assignment to exactly the three fixed keys
# (one each, canonical order). The ``AfterValidator`` runs whenever ``states`` is
# supplied explicitly — by a client POST, the clone path, or the LLM merge — so a
# ``[]`` / partial / duplicate-keyed list can never be persisted. The
# ``default_factory`` already yields a canonical list, so omitting ``states`` is
# unaffected (defaults are not re-validated).
SeededAgentStates = Annotated[list[AgentState], AfterValidator(normalize_agent_states)]


class AgentDefinition(BaseModel):
    """The in-progress definition of one agent being authored.

    Invariants:
        * ``mode == "refine"`` implies ``cloned_from`` is set (enforced by the
          clone path, not the model, so a half-built draft can still round-trip).
    """

    name: str = ""
    role: str = ""
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="Tool ids from GET /api/llm-tools/.")
    system_prompt: str = ""
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    states: SeededAgentStates = Field(
        default_factory=default_agent_states,
        description="The agent's operating states (planning/executing/researching). "
        "Auto-seeded on creation; each state's system_prompt is editable, the key set is fixed "
        "(a supplied list is normalized to exactly the three states).",
    )
    mode: StudioMode = "new"
    cloned_from: str | None = Field(
        default=None, description="Source registry manifest id when mode == 'refine'."
    )

    def missing_required(self) -> list[str]:
        """Return the names of required fields that are still empty.

        Postconditions:
            * Returns a subset of ``["name", "role"]`` in declaration order; empty
              iff the definition is ready to save/test.
        """
        missing: list[str] = []
        if not self.name.strip():
            missing.append("name")
        if not self.role.strip():
            missing.append("role")
        return missing

    @property
    def is_ready(self) -> bool:
        """True iff every required field is present (the Stage-2 gate)."""
        return not self.missing_required()


class StartConversationRequest(BaseModel):
    mode: StudioMode = "new"
    source_agent_id: str | None = Field(
        default=None, description="Required when mode == 'refine' — the agent to clone."
    )
    initial_message: str | None = None


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ConversationStateResponse(BaseModel):
    """The full authoring-conversation state returned after every turn."""

    conversation_id: str
    mode: StudioMode
    messages: list[ConversationMessage] = Field(default_factory=list)
    definition: AgentDefinition
    readiness: list[str] = Field(
        default_factory=list, description="Required fields still missing (empty => ready)."
    )
    suggested_questions: list[str] = Field(default_factory=list)


class SaveAgentRequest(BaseModel):
    """Editable fields accepted by ``POST /agents``.

    Excludes the server-owned ``mode`` / ``cloned_from`` fields carried on
    :class:`AgentDefinition` (they describe authoring provenance and are
    irrelevant to the saved manifest), so the save contract is unambiguous.
    """

    name: str = ""
    role: str = ""
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    states: SeededAgentStates = Field(
        default_factory=default_agent_states,
        description="The agent's operating states. Omitting them seeds the three defaults; a "
        "supplied list is normalized to exactly planning/executing/researching before save.",
    )

    def to_definition(self) -> AgentDefinition:
        """Project into an :class:`AgentDefinition` for the save pipeline.

        Postconditions:
            * Returns a ``new``-mode definition carrying only the editable fields.
        """
        return AgentDefinition(mode="new", **self.model_dump())


class SaveAgentResponse(BaseModel):
    """Result of saving + registering an authored agent."""

    agent_id: str
    manifest: AgentManifest
    created: bool = Field(
        ...,
        description=(
            "True if a new agent was registered; False if an existing agent with the same id "
            "(derived from the name) was updated in place. Lets the UI warn before a same-name overwrite."
        ),
    )


class AgentStudioDraftSummary(BaseModel):
    """Lightweight draft row for list endpoints (no payload).

    Invariants:
        * ``draft_id``, ``name``, and ``updated_at`` are always present on a
          persisted summary; the store owns id + timestamp assignment.
    """

    draft_id: str
    name: str
    updated_at: str = Field(..., description="ISO-8601 timestamp; server-managed.")


class AgentStudioDraft(BaseModel):
    """Full draft record: identity + opaque stage/handoff payload.

    The store persists ``payload`` verbatim and does not interpret stage fields
    (handoff ids, ``stage1AgentDraft``, etc.). Routes may validate shape later.

    Invariants:
        * ``payload`` is always a JSON object (``dict``), never a list/scalar.
    """

    draft_id: str
    name: str
    created_at: str = Field(..., description="ISO-8601 timestamp; server-managed.")
    updated_at: str = Field(..., description="ISO-8601 timestamp; server-managed.")
    payload: dict[str, Any] = Field(default_factory=dict)


class SaveDraftRequest(BaseModel):
    """Create/update body: optional label + opaque stage/handoff payload.

    On ``PUT``, omitted fields (``None``) leave the stored value unchanged; send
    an explicit ``payload`` object (including ``{}``) to replace it.

    Invariants:
        * ``payload``, when provided, is a JSON object (``dict``); the store
          rejects non-dicts with ``ValueError``.
    """

    name: str | None = None
    payload: dict[str, Any] | None = None


class RenameDraftRequest(BaseModel):
    """Rename body for ``PATCH /drafts/{draft_id}``."""

    name: str = Field(..., min_length=1)
