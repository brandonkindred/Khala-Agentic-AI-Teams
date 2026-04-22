"""Pydantic models for the Agent Registry."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IOSchema(BaseModel):
    """Pointer to a Pydantic model that describes an agent's input or output."""

    schema_ref: str | None = Field(
        default=None,
        description="Dotted import path in 'module.path:ClassName' form. "
        "Resolved lazily via pydantic.TypeAdapter(cls).json_schema().",
    )
    description: str | None = None


class InvokeSpec(BaseModel):
    """How to invoke the agent (consumed by Phase 2 — Runner)."""

    kind: Literal["http", "function", "temporal"]
    method: str | None = None
    path: str | None = None
    workflow: str | None = None
    callable_ref: str | None = None


class SandboxSpec(BaseModel):
    """Warm-sandbox provisioning hints (consumed by Phase 4)."""

    manifest_path: str | None = "default.yaml"
    access_tier: Literal["minimal", "standard", "elevated", "full"] = "standard"
    env: dict[str, str] = Field(default_factory=dict)
    extra_pip: list[str] = Field(default_factory=list)


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
