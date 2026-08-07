"""Join-at-read roster persona resolution from AgentManifest (Identity SoT)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_registry.models import AgentManifest
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
    skill_tags_from_manifest,
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


# Soft-enrich fallback when a roster ref's manifest_id no longer resolves (read paths only).
EMPTY_ROSTER_PERSONA = RosterPersonaView()


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
    summary = (manifest.summary or "").strip()
    name = (manifest.name or "").strip()
    return RosterPersonaView(
        role=summary or name,
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
    from agent_registry import get_registry

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


def _skill_tags_from_fat_raw(raw: dict) -> list[str]:
    """Extract deduped non-blank skill strings from a legacy fat roster dict."""
    skills = raw.get("skills") or []
    if not isinstance(skills, list):
        return []
    tags: list[str] = []
    for skill in skills:
        cleaned = str(skill).strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def _ensure_generated_manifest_from_fat(
    team_id: str,
    agent_name: str,
    manifest_id: str,
    raw: dict,
    registry,
) -> None:
    """Register or update a generated Manifest from legacy fat persona fields.

    Preconditions:
        * ``team_id`` / ``agent_name`` / ``manifest_id`` are non-empty.
        * ``registry`` supports ``get`` / ``register``.
    Postconditions:
        * Ensures a Manifest at ``manifest_id``. Fat ``role`` → summary;
          fat ``skills`` → skill tags (merged with any existing non-marker tags).
        * Does not clear existing skill tags when fat ``skills`` is empty/absent.
    """
    fat_skills = _skill_tags_from_fat_raw(raw)
    role = raw.get("role")
    role_summary = str(role).strip() if role is not None and str(role).strip() else None
    existing = registry.get(manifest_id)
    if existing is None:
        registry.register(
            build_agent_manifest(
                team_id,
                agent_name,
                summary=role_summary,
                skill_tags=fat_skills or None,
            )
        )
        return

    merged_skills = skill_tags_from_manifest(existing)
    for tag in fat_skills:
        if tag not in merged_skills:
            merged_skills.append(tag)
    summary = role_summary if role_summary is not None else existing.summary
    if merged_skills == skill_tags_from_manifest(existing) and summary == existing.summary:
        return
    registry.register(
        build_agent_manifest(
            team_id,
            agent_name,
            summary=summary,
            skill_tags=merged_skills,
        )
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
        * For ``source == "generated"``, ensures a Manifest exists and copies
          legacy fat ``role`` / ``skills`` onto summary / tags when present
          (merging skill tags with any already on the Manifest).
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
        mid = str(manifest_id)
        # Generated rows may still carry denormalized fat skills alongside a stamped
        # id — fold them into the Manifest before stripping the roster JSON.
        if source == SOURCE_GENERATED and _raw_has_fat_keys(raw):
            from agent_registry import get_registry

            _ensure_generated_manifest_from_fat(team_id, agent_name, mid, raw, get_registry())
        return (
            _thin_agent(agent_name=agent_name, source=source, manifest_id=mid),
            _raw_has_fat_keys(raw),
        )

    mid = manifest_agent_id(team_id, agent_name)
    from agent_registry import get_registry

    _ensure_generated_manifest_from_fat(team_id, agent_name, mid, raw, get_registry())
    return _thin_agent(agent_name=agent_name, source=source, manifest_id=mid), True


def coerce_roster_agent(team_id: str, raw: dict) -> AgenticTeamAgent:
    """Normalize a legacy or thin roster dict into a validated thin ``AgenticTeamAgent``.

    Used by Temporal activities (workflow history may still carry fat rows) and any
    path that must accept pre-thin JSON without going through the Postgres loaders.

    Preconditions:
        * ``team_id`` is a non-empty string.
        * ``raw`` is a mapping with ``agent_name`` (same rules as
          :func:`migrate_roster_row`).
    Postconditions:
        * Returns a validated thin :class:`AgenticTeamAgent` (``agent_name``,
          ``source``, ``manifest_id`` only). May register a generated Manifest when
          migrating a fat generated row that lacked ``manifest_id``.
    """
    agent, _changed = migrate_roster_row(team_id, raw)
    return AgenticTeamAgent.model_validate(agent.model_dump(mode="json"))
