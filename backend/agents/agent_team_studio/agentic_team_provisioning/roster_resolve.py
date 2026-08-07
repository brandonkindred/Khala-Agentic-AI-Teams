"""Join-at-read roster persona resolution from AgentManifest (Identity SoT)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_registry import get_registry
from agent_registry.models import AgentManifest
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
)
from agent_team_studio.agentic_team_provisioning.models import (
    SOURCE_GENERATED,
    SOURCE_REGISTRY,
    AgenticTeamAgent,
)

_THIN_ROSTER_KEYS = frozenset({"agent_name", "source", "manifest_id"})


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


def _raw_has_fat_keys(raw: dict) -> bool:
    """True when ``raw`` carries legacy persona fields beyond the thin ref shape."""
    return any(k not in _THIN_ROSTER_KEYS for k in raw)


def _thin_agent(*, agent_name: str, source: str, manifest_id: str) -> AgenticTeamAgent:
    """Build a thin roster ref without requiring legacy fat fields on the model."""
    return AgenticTeamAgent.model_construct(
        agent_name=agent_name,
        source=source,
        manifest_id=manifest_id,
    )


def migrate_roster_row(team_id: str, raw: dict) -> tuple[AgenticTeamAgent, bool]:
    """Eagerly migrate one legacy fat roster row to a thin ref + stamped ``manifest_id``.

    Preconditions:
        * ``team_id`` is a non-empty string.
        * ``raw`` contains ``agent_name``.
        * When ``source == "registry"``, ``manifest_id`` must be present and non-empty.
    Postconditions:
        * Returns ``(agent, changed)`` where ``agent`` carries only
          ``agent_name``, ``source``, and ``manifest_id`` (via ``model_construct``).
        * ``changed`` is ``True`` when persistence should rewrite the row: newly
          stamped ``manifest_id``, or fat keys still present in ``raw``.
        * ``changed`` is ``False`` when ``raw`` is already thin-only with
          ``manifest_id`` set.
        * For ``source == "generated"`` without ``manifest_id``, registers a
          manifest when the registry lacks the stable id.
    Raises:
        * ``ValueError`` when ``source == "registry"`` and ``manifest_id`` is missing.
    """
    if not team_id or not str(team_id).strip():
        raise ValueError("migrate_roster_row: team_id must be non-empty")
    agent_name = raw.get("agent_name")
    if not agent_name or not str(agent_name).strip():
        raise ValueError("migrate_roster_row: agent_name must be non-empty")

    source = raw.get("source") or SOURCE_GENERATED
    manifest_id = raw.get("manifest_id")
    has_manifest_id = manifest_id is not None and str(manifest_id).strip()

    if source == SOURCE_REGISTRY and not has_manifest_id:
        raise ValueError("registry roster row requires manifest_id")

    if has_manifest_id:
        return (
            _thin_agent(agent_name=agent_name, source=source, manifest_id=str(manifest_id)),
            _raw_has_fat_keys(raw),
        )

    mid = manifest_agent_id(team_id, agent_name)
    registry = get_registry()
    if registry.get(mid) is None:
        summary = raw.get("role") or None
        manifest = build_agent_manifest(team_id, agent_name, summary=summary)
        registry.register(manifest)

    return _thin_agent(agent_name=agent_name, source=source, manifest_id=mid), True
