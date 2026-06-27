"""Pydantic models for the Agent Studio Stage-1 build flow.

The central type is :class:`AgentDefinition` — the single-agent definition the
authoring assistant co-authors, the clone endpoint produces, and the save
endpoint consumes. All other models are request/response envelopes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_registry.models import AgentManifest

# The two authoring modes. ``new`` builds from scratch; ``refine`` starts from a
# clone of an existing registry agent (``cloned_from`` records the source id).
StudioMode = Literal["new", "refine"]


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


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


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
