"""Join-at-read roster persona resolution from AgentManifest (Identity SoT)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_registry import get_registry
from agent_registry.models import AgentManifest


class RosterPersonaView(BaseModel):
    """Non-persisted free-text persona projected from a Manifest.

    Invariants:
        * Never written to ``agentic_team_agents``; roster stores thin refs only.
    """

    role: str = ""
    skills: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    expertise: list[str] = Field(default_factory=list)


def persona_from_manifest(manifest: AgentManifest) -> RosterPersonaView:
    """Map Manifest fields to the free-text persona view used by ``build_agent``.

    Preconditions: ``manifest`` is a validated ``AgentManifest``.
    Postconditions: ``role`` is ``summary`` (or ``name`` if summary blank);
        ``skills`` ← ``tags``; ``tools`` ← ``cognition.tools`` or ``[]``;
        ``expertise`` ← ``[team]`` when team non-empty; ``capabilities`` always ``[]``.
    """
    tools: list[str] = []
    if manifest.cognition and manifest.cognition.tools:
        tools = list(manifest.cognition.tools)
    return RosterPersonaView(
        role=(manifest.summary or manifest.name or "").strip(),
        skills=list(manifest.tags or []),
        capabilities=[],
        tools=tools,
        expertise=[manifest.team] if manifest.team else [],
    )


def resolve_persona(manifest_id: str) -> RosterPersonaView:
    """Load Manifest by id and project persona.

    Preconditions: ``manifest_id`` is a non-empty string.
    Postconditions: returns ``persona_from_manifest`` for the registry entry.
    Raises: ``LookupError`` if the Manifest is not in the registry.
    """
    if not manifest_id or not str(manifest_id).strip():
        raise LookupError("manifest_id must be non-empty")
    manifest = get_registry().get(manifest_id)
    if manifest is None:
        raise LookupError(f"AgentManifest not found: {manifest_id}")
    return persona_from_manifest(manifest)
