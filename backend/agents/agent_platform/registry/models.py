"""Pydantic models for the Agent Registry."""

from __future__ import annotations

from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, Field, field_validator


class IOSchema(BaseModel):
    """Describes an agent's input or output, as either a dotted class ref or inline JSON Schema.

    An agent may advertise its schema two ways:
      * ``schema_ref`` — a dotted ``module.path:ClassName`` import path to a Pydantic
        model, resolved lazily to JSON Schema at request time.
      * ``inline_schema`` — a literal JSON Schema dict carried on the manifest, for
        agents authored without a corresponding Python class (e.g. Agent Studio
        definitions with an authored ``input_schema`` / ``output_schema``).

    Invariants:
        * Both fields are optional. When ``inline_schema`` is present it is
          authoritative and returned verbatim (no import needed); ``schema_ref`` is
          only consulted when ``inline_schema`` is absent. An ``IOSchema`` with
          neither advertises no schema.
    """

    schema_ref: str | None = Field(
        default=None,
        description="Dotted import path in 'module.path:ClassName' form. "
        "Resolved lazily via pydantic.TypeAdapter(cls).json_schema().",
    )
    inline_schema: dict[str, Any] | None = Field(
        default=None,
        description="Literal JSON Schema for an agent authored without a Python model. "
        "Takes precedence over schema_ref when present; returned verbatim by the "
        "schema-resolution endpoints.",
    )
    description: str | None = None

    @field_validator("inline_schema")
    @classmethod
    def _validate_inline_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject an ``inline_schema`` that isn't itself a well-formed JSON Schema.

        Preconditions: none — ``value`` may be ``None``.
        Postconditions: returns ``value`` unchanged when ``None`` or a structurally
            valid JSON Schema document; raises ``ValueError`` (surfaced as a Pydantic
            ``ValidationError``) otherwise. Structural only — this checks that the
            dict is itself well-formed JSON Schema, not that any particular instance
            validates against it.
        """
        if value is None:
            return value
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"inline_schema is not a valid JSON Schema: {exc.message}") from exc
        return value


class InvokeSpec(BaseModel):
    """How to invoke the agent (consumed by Phase 2 — Runner)."""

    kind: Literal["http", "function", "temporal"]
    method: str | None = None
    path: str | None = None
    workflow: str | None = None
    callable_ref: str | None = None
    timeout_seconds: float | None = Field(
        default=None,
        description="Per-agent execution timeout inside the sandbox. "
        "Falls back to AGENT_EXEC_TIMEOUT_S (default 60s).",
    )


class SandboxSpec(BaseModel):
    """Warm-sandbox provisioning hints (consumed by Phase 4)."""

    manifest_path: str | None = "default.yaml"
    access_tier: Literal["minimal", "standard", "elevated", "full"] = "standard"
    env: dict[str, str] = Field(default_factory=dict)
    extra_pip: list[str] = Field(default_factory=list)


class CognitionMemorySpec(BaseModel):
    """Per-agent memory-retention knobs for the Agent Cognition Core.

    Preconditions:
        - ``retention_days_events`` >= 1. A sub-day retention is meaningless for a
          daily-rollup pruner; enforced by the ``ge=1`` field constraint, so a manifest
          declaring ``0`` (or negative) fails validation and is skipped by the loader.
    """

    retention_days_events: int = Field(
        default=90,
        ge=1,
        description="Raw episodic memory events older than this many days are pruned by the "
        "central cognition scheduler, but only after they have been folded into a non-stale "
        "day summary (rollup summaries are retained long-term). Must be >= 1.",
    )


class CognitionKnowledgeGraphSpec(BaseModel):
    """Per-agent knowledge-graph (Neo4j + Graphiti) config.

    Controls the knowledge-graph layer that sits over the rote memory rollups:
    what the sync worker ingests for this agent, and whether reflection grounds its
    rule proposals with graph context. Every new cognition-enabled agent gets a
    knowledge base by default (``enabled=True``); a specific agent opts out by
    setting it false.

    Invariants:
        - Every field carries a safe default, so an omitted block (the common case)
          behaves as the default-on graph.
    """

    enabled: bool = Field(
        default=True,
        description="Attach a Graphiti knowledge graph (group_id = agent_id) to this agent.",
    )
    ingest_events: bool = Field(
        default=True, description="Ingest the agent's raw episodic events into the graph."
    )
    ingest_summaries: bool = Field(
        default=True, description="Ingest the agent's rollup summaries into the graph."
    )
    ground_rule_proposals: bool = Field(
        default=True,
        description="Feed graph context into reflection so learned rule proposals are "
        "better grounded. Never bypasses the human approval gate.",
    )


class CognitionSpec(BaseModel):
    """Declarative per-agent cognition config for the Agent Cognition Core.

    Metadata only — the registry never consumes it. Later phases do: the tools layer
    resolves ``tools``, the invoke proxy honours ``requires_idempotency_key``, the seed-pack
    installer reads ``rule_packs``, the central scheduler prunes memory per
    ``memory.retention_days_events``, and the knowledge-graph sync worker / reflection read
    ``knowledge_graph``.

    Invariants:
        - A manifest may omit the whole block (``AgentManifest.cognition is None``). When the
          block is present every sub-field carries a safe default, so partial blocks always
          validate — mirroring the lazy ``InvokeSpec`` / ``SandboxSpec`` pattern.
    """

    memory: CognitionMemorySpec = Field(default_factory=CognitionMemorySpec)
    tools: list[str] = Field(
        default_factory=list,
        description="Tool ids resolved against LlmToolsService + a caller-supplied integration registry + agent_git_tools.",
    )
    rule_packs: list[str] = Field(
        default_factory=list,
        description="Seed rule-pack names installed on first provision, e.g. 'default_guardrails'.",
    )
    requires_idempotency_key: bool = Field(
        default=False,
        description="When true the agent is side-effecting; invokes without a caller "
        "idempotency key are rejected.",
    )
    knowledge_graph: CognitionKnowledgeGraphSpec = Field(
        default_factory=CognitionKnowledgeGraphSpec,
        description="Knowledge-graph (Neo4j + Graphiti) attachment for this agent.",
    )


class AgentStateSpec(BaseModel):
    """One persisted operating-state persona (planning/executing/researching).

    Metadata only — the registry never consumes it; nothing reads it at invoke
    time. It rides along with the saved manifest so an authored agent's behavioral
    states survive ``register()`` / ``get()`` and are available to clone-back.
    Runtime binding is the same deferred follow-up the persona ``system_prompt``
    already carries.

    ``key`` is a plain ``str`` (not an enum) so the registry never fails validation
    on persisted data; the authoring layer (``agent_studio``) enforces the fixed
    key set at write time.
    """

    key: str
    label: str
    system_prompt: str


class SourceInfo(BaseModel):
    """Traceability — where the agent lives in the codebase."""

    entrypoint: str = Field(
        ...,
        description="Dotted import path in 'module.path:Symbol' form pointing to the "
        "agent's primary class or factory. Not imported at registry load time.",
    )
    anatomy_ref: str | None = Field(
        default=None,
        description="Optional repo-relative path to an anatomy markdown doc for this agent.",
    )


class AgentManifest(BaseModel):
    """One entry per specialist agent, loaded from YAML."""

    schema_version: int = 1
    id: str = Field(..., description="Globally unique dotted identifier, e.g. 'blogging.planner'.")
    team: str = Field(..., description="Team key matching TEAM_CONFIGS in unified_api/config.py.")
    name: str
    summary: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    inputs: IOSchema | None = None
    outputs: IOSchema | None = None
    invoke: InvokeSpec | None = None
    sandbox: SandboxSpec | None = None
    cognition: CognitionSpec | None = None
    states: list[AgentStateSpec] = Field(
        default_factory=list,
        description="Seeded operating-state personas (planning/executing/researching). "
        "Additive metadata — inert until the deferred runtime-binding follow-up; absent "
        "on agents authored before this field existed.",
    )
    source: SourceInfo


class AgentSummary(BaseModel):
    """Light projection used by catalog list endpoints."""

    id: str
    team: str
    name: str
    summary: str
    tags: list[str]
    has_input_schema: bool = False
    has_output_schema: bool = False
    has_invoke: bool = False
    has_sandbox: bool = False
    has_cognition: bool = False
    has_knowledge_graph: bool = False


class AgentDetail(BaseModel):
    """Full detail view, plus any resolved anatomy text if present on disk."""

    manifest: AgentManifest
    anatomy_markdown: str | None = None


class TeamGroup(BaseModel):
    """Team-level grouping for the catalog filter sidebar."""

    team: str
    display_name: str
    agent_count: int
    tags: list[str] = Field(default_factory=list)
