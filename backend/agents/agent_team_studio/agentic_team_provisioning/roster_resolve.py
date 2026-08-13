"""Join-at-read roster persona resolution from AgentManifest (Identity SoT)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_platform.registry.models import AgentManifest
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
        ``skills`` ← non-marker Manifest tags (excludes ``"generated"`` / team-key
        stamps — same filter as :func:`skill_tags_from_manifest`);
        ``tools`` ← ``cognition.tools`` or ``[]``;
        ``expertise`` ← ``[team]`` when team non-empty; ``capabilities`` always ``[]``.
    """
    tools: list[str] = []
    if manifest.cognition and manifest.cognition.tools:
        tools = list(manifest.cognition.tools)
    summary = (manifest.summary or "").strip()
    name = (manifest.name or "").strip()
    return RosterPersonaView(
        role=summary or name,
        skills=skill_tags_from_manifest(manifest),
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
    from agent_platform.registry import get_registry

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


def _string_tags_from_fat_raw(raw: dict, key: str) -> list[str]:
    """Extract deduped non-blank strings from a legacy fat roster list field."""
    values = raw.get(key) or []
    if not isinstance(values, list):
        return []
    tags: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def persona_tags_from_fat_raw(raw: dict) -> list[str]:
    """Fold free-text persona lists into Manifest skill tags.

    Used by legacy migrate and LLM roster save. ``skills``, ``tools``,
    ``capabilities``, and ``expertise`` have no separate Manifest homes for
    free-text labels (``cognition.tools`` is reserved for resolvable tool ids).
    Merge them into tags so labels are not dropped when persisting thin refs.

    Preconditions: ``raw`` is a mapping (may omit any of the list keys).
    Postconditions: returns deduped non-blank tags in first-seen order across
        skills → tools → capabilities → expertise; empty when none are present.
    """
    tags: list[str] = []
    for key in ("skills", "tools", "capabilities", "expertise"):
        for tag in _string_tags_from_fat_raw(raw, key):
            if tag not in tags:
                tags.append(tag)
    return tags


def llm_persona_lists_explicitly_empty(raw: dict) -> bool:
    """Return True when the LLM roster dict explicitly clears persona lists.

    Distinguishes ``"skills": []`` (clear) from an omitted key or a
    whitespace-only list (preserve prior Manifest tags). Malformed non-list
    values are treated as absent.

    Preconditions: ``raw`` is a mapping.
    Postconditions: True iff at least one of ``skills`` / ``tools`` /
        ``capabilities`` / ``expertise`` is present as a list, every such
        present list is literally empty (``[]``), and
        :func:`persona_tags_from_fat_raw` yields no non-blank tags.
    """
    present_lists: list[list] = []
    for key in ("skills", "tools", "capabilities", "expertise"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list):
            continue
        present_lists.append(value)
    if not present_lists:
        return False
    return all(len(v) == 0 for v in present_lists) and not persona_tags_from_fat_raw(raw)


# Back-compat alias for callers/tests that imported the private name.
_persona_tags_from_fat_raw = persona_tags_from_fat_raw
_llm_persona_lists_explicitly_empty = llm_persona_lists_explicitly_empty


def _ensure_generated_manifest_from_fat(
    team_id: str,
    agent_name: str,
    manifest_id: str,
    raw: dict,
    registry,
    *,
    conn: object | None = None,
) -> None:
    """Register or update a generated Manifest from legacy fat persona fields.

    Preconditions:
        * ``team_id`` / ``agent_name`` / ``manifest_id`` are non-empty.
        * ``registry`` supports ``get`` / ``register`` (with ``require_persist``).
    Postconditions:
        * Ensures a Manifest at ``manifest_id``. Fat ``role`` → summary;
          fat ``skills`` / ``tools`` / ``capabilities`` / ``expertise`` → skill
          tags (merged with any existing non-marker tags).
        * Does not clear existing skill tags when those fat lists are empty/absent.
        * Registration uses ``require_persist=True`` so a dynamic-store upsert
          failure raises and callers can leave the fat roster row unstripped.
        * When ``conn`` is provided, registry get/register join that connection
          (no nested pool checkout while the caller holds a roster ``get_conn()``).
    """
    fat_skills = persona_tags_from_fat_raw(raw)
    role = raw.get("role")
    role_summary = str(role).strip() if role is not None and str(role).strip() else None
    existing = registry.get(manifest_id, conn=conn)
    if existing is None:
        registry.register(
            build_agent_manifest(
                team_id,
                agent_name,
                summary=role_summary,
                skill_tags=fat_skills or None,
            ),
            require_persist=True,
            conn=conn,
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
        ),
        require_persist=True,
        conn=conn,
    )


def migrate_roster_row(
    team_id: str, raw: dict, *, conn: object | None = None
) -> tuple[AgenticTeamAgent, bool]:
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
          legacy fat ``role`` onto summary and fat ``skills`` / ``tools`` /
          ``capabilities`` / ``expertise`` onto skill tags when present
          (merging with any already on the Manifest). Manifest
          registration is fail-closed (``require_persist=True``) so a store
          upsert failure aborts before callers strip the fat roster row.
        * When ``conn`` is provided, Manifest get/register join that connection.
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
            from agent_platform.registry import get_registry

            _ensure_generated_manifest_from_fat(
                team_id, agent_name, mid, raw, get_registry(), conn=conn
            )
        return (
            _thin_agent(agent_name=agent_name, source=source, manifest_id=mid),
            _raw_has_fat_keys(raw),
        )

    mid = manifest_agent_id(team_id, agent_name)
    from agent_platform.registry import get_registry

    _ensure_generated_manifest_from_fat(
        team_id, agent_name, mid, raw, get_registry(), conn=conn
    )
    return _thin_agent(agent_name=agent_name, source=source, manifest_id=mid), True


def coerce_roster_agent(team_id: str, raw: dict, *, conn: object | None = None) -> AgenticTeamAgent:
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
    agent, _changed = migrate_roster_row(team_id, raw, conn=conn)
    return AgenticTeamAgent.model_validate(agent.model_dump(mode="json"))
