"""Generate ``agent_registry`` manifests for agents this team produces.

Every agent the agentic-team designer rosters should join the platform with the
batteries-included Agent Cognition Core attached. The pure builder helpers in this
module turn a roster :class:`~agent_team_studio.agentic_team_provisioning.models.AgenticTeamAgent`
into a validated :class:`~agent_registry.models.AgentManifest` whose ``cognition``
block carries the core defaults (day-one ``default_guardrails`` seed pack, 90-day
episodic memory, a default-on knowledge graph). Those builders perform no
Postgres, LLM, or filesystem I/O.

:func:`register_team_manifests` is the stateful counterpart: it installs built
manifests into the process-wide registry and, when a Postgres-backed dynamic store
is active, persists them so other workers and sandboxes can resolve them.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

from agent_registry.models import (
    AgentManifest,
    CognitionKnowledgeGraphSpec,
    CognitionMemorySpec,
    CognitionSpec,
    IOSchema,
    SourceInfo,
)
from agent_team_studio.agentic_team_provisioning.agent_env_provisioning import _slug
from agent_team_studio.agentic_team_provisioning.models import SOURCE_GENERATED, AgenticTeamAgent

# The registry team key for this service (matches TEAM_CONFIGS in
# unified_api/config.py). This is the manifest ``team`` value — distinct from the
# per-instance team UUID, which only feeds the slugged manifest id.
_TEAM_KEY = "agentic_team_provisioning"

# Where the canonical agent-anatomy contract lives, repo-relative per SourceInfo.
_ANATOMY_REF = "backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md"

# The invokable sandbox entrypoint: a single callable that accepts one request
# body (carrying the roster metadata + message), reconstructs the agent, and runs
# it through the cognition-aware wrapper. The dispatch shim calls it as
# ``entrypoint(body)``, so it must take exactly the request body.
_ENTRYPOINT = (
    "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
)

# Dotted refs to the Pydantic models describing the invoke contract (resolved
# lazily by the registry, never imported at load time).
_INPUT_SCHEMA_REF = "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeInput"
_OUTPUT_SCHEMA_REF = "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeOutput"


# Hex length of the id-disambiguating digest. 16 hex chars = 64 bits: accidental
# collisions stay negligible far past any realistic team/agent count, and an
# attacker who controls agent names can't cheaply force a within-team clash (an
# 8-hex/32-bit digest was birthday-collidable at ~65k, hence too weak for the
# multi-tenant cleanup prefix). Keep it well short of the full digest so ids stay
# readable in URLs / the catalog.
_HASH_HEX_LEN = 16


def _id_hash(s: str) -> str:
    """Stable, practically collision-resistant hex digest for derived ids."""
    return hashlib.sha256(s.encode()).hexdigest()[:_HASH_HEX_LEN]


def team_id_prefix(team_id: str) -> str:
    """Manifest-id prefix shared by every generated agent of one team.

    Postconditions: returns ``"agentic_team_provisioning.<team-slug>-<hash>."`` —
    the common prefix of every id :func:`manifest_agent_id` produces for
    ``team_id``, used to find this team's generated entries in the registry. The
    ``<hash>`` of the *full* ``team_id`` makes accidental collisions between two
    teams that share a normalized 12-char slug negligible at any realistic team
    count (see ``_HASH_HEX_LEN``), so stale cleanup keyed on this prefix is
    extremely unlikely to touch another team's manifests.
    """
    return f"{_TEAM_KEY}.{_slug(team_id, 12)}-{_id_hash(team_id)}."


def manifest_agent_id(team_id: str, agent_name: str) -> str:
    """Stable, practically unique manifest id for a generated agent.

    Preconditions:
        * ``team_id`` and ``agent_name`` are non-empty.
    Postconditions:
        * Deterministic for a given ``(team_id, agent_name)`` pair. Distinct pairs
          produce distinct ids at any realistic roster size: :func:`team_id_prefix`
          plus a 64-bit (``_HASH_HEX_LEN`` hex) hash of the original strings
          disambiguates names that share a normalized slug (e.g. ``"QA Agent"``
          and ``"qa-agent"``, or names agreeing on their first 40 slug chars).
          Accidental birthday collision on the truncated digest is mathematically
          possible but negligible far past realistic team/agent counts (see
          ``_HASH_HEX_LEN``). Always starts with :func:`team_id_prefix`.
    """
    pair_hash = _id_hash(f"{team_id}\x00{agent_name}")
    return f"{team_id_prefix(team_id)}{_slug(agent_name, 40)}-{pair_hash}"


def default_cognition_block() -> CognitionSpec:
    """Return the batteries-included Agent Cognition Core defaults.

    Preconditions:
        * None.
    Postconditions:
        * Returns a :class:`CognitionSpec` equal to the batteries-included core:
          90-day episodic memory (``memory.retention_days_events == 90``), an
          empty ``tools`` list, ``requires_idempotency_key`` False, a default-on
          knowledge graph, and exactly one seed pack — ``default_guardrails``.

    ``tools`` is deliberately empty: a roster agent's ``tools`` are free-text
    labels ("Git", "Slack API") that do not resolve against the cognition tool
    registries (``LlmToolsService`` + a caller-supplied integration registry + ``agent_git_tools``),
    so they are never stamped here — that would only break later tool resolution.
    """
    return CognitionSpec(
        memory=CognitionMemorySpec(retention_days_events=90),
        tools=[],
        rule_packs=["default_guardrails"],
        requires_idempotency_key=False,
        knowledge_graph=CognitionKnowledgeGraphSpec(),
    )


def build_agent_manifest(
    team_id: str,
    agent_name: str,
    *,
    summary: str | None = None,
    skill_tags: list[str] | None = None,
) -> AgentManifest:
    """Build a validated ``agent_registry`` manifest for one roster agent.

    Preconditions:
        * ``team_id`` is a non-empty string.
        * ``agent_name`` is non-empty.
    Postconditions:
        * Returns a fully validated :class:`AgentManifest` whose ``cognition``
          equals :func:`default_cognition_block` (``rule_packs ==
          ["default_guardrails"]``), ``team == "agentic_team_provisioning"``,
          ``source.entrypoint`` points at the roster-agent factory, and ``id`` is
          the stable, practically unique :func:`manifest_agent_id`.
        * ``tags`` always includes ``"generated"`` and the team key, then any
          non-blank ``skill_tags`` (deduped, order preserved).
    """
    # Explicit validation rather than ``assert`` (which ``python -O`` strips): these
    # are real boundary preconditions — silently skipping them under ``-O`` would
    # build an id from an empty key and could mis-scope the stale-cleanup prefix.
    if not team_id:
        raise ValueError("build_agent_manifest: team_id must be non-empty")
    if not agent_name:
        raise ValueError("build_agent_manifest: agent_name must be non-empty")

    manifest_id = manifest_agent_id(team_id, agent_name)
    resolved_summary = (summary or "").strip() or f"Generated agent {agent_name}"
    tags: list[str] = ["generated", _TEAM_KEY]
    for tag in skill_tags or []:
        cleaned = str(tag).strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)

    manifest = AgentManifest(
        id=manifest_id,
        team=_TEAM_KEY,
        name=agent_name,
        summary=resolved_summary,
        tags=tags,
        inputs=IOSchema(
            schema_ref=_INPUT_SCHEMA_REF,
            description="Roster metadata + user message; a shared entrypoint serves every "
            "generated agent.",
        ),
        outputs=IOSchema(schema_ref=_OUTPUT_SCHEMA_REF, description="The agent's response text."),
        cognition=default_cognition_block(),
        source=SourceInfo(entrypoint=_ENTRYPOINT, anatomy_ref=_ANATOMY_REF),
    )
    # Round-trip through a JSON-safe dump so the returned object is guaranteed
    # to be a fully validated manifest (and safe to serialize over the API).
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))


def skill_tags_from_manifest(manifest: AgentManifest) -> list[str]:
    """Return non-marker tags from a Manifest (persona skill tags).

    Preconditions: ``manifest`` is a validated :class:`AgentManifest`.
    Postconditions: returns tags excluding the ``"generated"`` and team-key markers
        stamped by :func:`build_agent_manifest`, order preserved.
    """
    return [t for t in (manifest.tags or []) if t not in ("generated", _TEAM_KEY)]


def is_generated_manifest(manifest: AgentManifest) -> bool:
    """Whether ``manifest`` is a roster-generated agent (vs. a hand-authored /
    catalog registry agent).

    Generated manifests are ephemeral and roster-owned: :func:`register_team_manifests`
    (un)registers them as a team's roster changes. They must therefore never be added
    to a roster via the from-registry path — doing so creates a registry-source row
    whose ``manifest_id`` the owning team can later unregister, leaving it dangling
    (broken catalog / invoke). The ``"generated"`` tag is the marker
    :func:`build_agent_manifest` stamps and the same one
    :func:`register_team_manifests` keys its stale-cleanup scan on, so classifying on
    it keeps the two paths in agreement.

    Preconditions: ``manifest`` is a validated :class:`AgentManifest`.
    Postconditions: returns ``True`` iff ``manifest`` carries the ``"generated"`` tag.
    """
    return "generated" in manifest.tags


class ManifestRegistrationResult(NamedTuple):
    """Outcome of a successful :func:`register_team_manifests` call.

    Supports both attribute access (``result.manifests``) and positional
    unpacking (``manifests, registered = register_team_manifests(...)``).

    On success ``registered`` is always ``True`` (registry / store failures
    propagate rather than returning ``registered=False``), so chat-save can roll
    back the roster write when registration fails. ``registered`` remains part of
    the result shape for callers that already destructure it.
    """

    manifests: list[AgentManifest]
    registered: bool


def register_team_manifests(
    team_id: str,
    agents: list[AgenticTeamAgent],
    *,
    summaries: dict[str, str] | None = None,
    skill_tags: dict[str, list[str]] | None = None,
    conn: object | None = None,
) -> ManifestRegistrationResult:
    """Build and install the live registry entries for a team's roster.

    Generated agents are not discovered from disk, so they are registered into the
    process-wide :class:`~agent_registry.loader.AgentRegistry` here — making them
    resolvable by the Agent Console catalog and the ``/api/agents/{id}/invoke``
    route (cognition injection + seed-pack install then happen on first invoke).

    Scope: registration always updates *this process's* in-memory registry
    immediately. When a Postgres-backed dynamic store is active (see
    ``AgentRegistry.replace_dynamic_manifests``), the full replacement
    (upserts + stale deletes) commits in **one** Postgres transaction before
    local memory is updated, so other workers and per-invoke sandboxes that
    share that store never observe a partial replace. When ``conn`` is provided
    (chat-save ``on_merged``), those store statements join the caller's open
    roster transaction so both commit or roll back together. When no dynamic
    store is active (Postgres off / sandboxed), registration is local-only and
    other processes won't see these entries until they generate their own.

    Preconditions:
        * ``team_id`` is non-empty.
    Postconditions:
        * Returns a :class:`ManifestRegistrationResult` whose ``manifests`` holds
          one validated manifest per **generated** roster agent (registry-source
          agents are skipped — they're already in the registry) and whose
          ``registered`` is ``True``. Every returned manifest is registered
          (``get_registry().get(m.id)`` returns it). Acts as a full replace for
          the team: previously-registered generated manifests for ``team_id`` that
          are absent from this roster are unregistered, so removed/renamed agents
          stop appearing in the catalog. Idempotent; persisted to the dynamic
          store when one is active, in-memory-only otherwise (see Scope above).
          Registry / store failures propagate — callers that must keep the DB
          roster and registry in sync (e.g. chat-save ``on_merged``) let the
          raise roll back the roster write; import-time retroactive registration
          wraps this call. The prefix scan that computes the stale set uses
          ``require_store=True`` so a failed cross-worker scan cannot silently
          omit another worker's stale ids. Per-agent ``summary`` is taken from
          ``summaries[agent_name]`` when provided, else the existing registry
          manifest's summary when present, else the default from
          :func:`build_agent_manifest`. When ``skill_tags`` includes
          ``agent_name``, those values replace prior skill tags; when the agent
          is absent from ``skill_tags`` (including startup / omitted map),
          non-marker tags on the existing Manifest are preserved — same fallback
          pattern as ``summaries``.
    """
    # Explicit validation rather than ``assert`` (``python -O`` strips asserts): an
    # empty ``team_id`` would compute a degenerate cleanup prefix, so fail loud here
    # in optimized builds too instead of silently scanning the wrong id space.
    if not team_id:
        raise ValueError("register_team_manifests: team_id must be non-empty")
    # Only *generated* roster agents get a team-namespaced wrapper installed here.
    # Registry-source agents (added via Agent Studio's from-registry endpoint)
    # already exist in the registry on their own, so wrapping them would register a
    # duplicate "generated"-tagged entry — e.g. on every restart via the retroactive
    # recovery path, which passes the whole roster. The stale-cleanup below then also
    # drops any such wrapper left behind by an older build.
    agents = [a for a in agents if a.source == SOURCE_GENERATED]
    from agent_registry import get_registry

    registry = get_registry()
    summary_map = summaries or {}
    # ``None`` means "caller omitted skill_tags" — treat like an empty map so per-agent
    # absence falls back to the existing Manifest (startup retroactive register).
    skill_tags_map = skill_tags if skill_tags is not None else {}
    manifests: list[AgentManifest] = []
    for a in agents:
        existing = registry.get(a.manifest_id, conn=conn) if a.manifest_id else None
        summary = summary_map.get(a.agent_name)
        if summary is None and existing is not None:
            summary = existing.summary
        if a.agent_name in skill_tags_map:
            agent_skill_tags: list[str] | None = skill_tags_map[a.agent_name]
        elif existing is not None:
            agent_skill_tags = skill_tags_from_manifest(existing)
        else:
            agent_skill_tags = None
        manifests.append(
            build_agent_manifest(
                team_id,
                a.agent_name,
                summary=summary,
                skill_tags=agent_skill_tags,
            )
        )
    new_ids = {m.id for m in manifests}
    # Snapshot prior team-generated manifests before mutating. Scope strictly to
    # this team's generated ids (prefix + the "generated" tag) so a hand-authored
    # disk manifest is never touched. ``require_store=True`` so a dynamic-store
    # scan failure cannot silently omit another worker's stale ids. This runs
    # under the team lock on the chat-save path, so keeping the scan's allocation
    # small matters.
    prefix = team_id_prefix(team_id)
    prior_generated = {
        m.id: m
        for m in registry.manifests_with_id_prefix(prefix, require_store=True)
        if is_generated_manifest(m)
    }
    stale = [agent_id for agent_id in prior_generated if agent_id not in new_ids]
    # Single atomic replace: shared-store upserts + deletes commit together (or
    # join ``conn`` when provided), then local memory is updated.
    registry.replace_dynamic_manifests(manifests, stale, conn=conn)
    return ManifestRegistrationResult(manifests=manifests, registered=True)
