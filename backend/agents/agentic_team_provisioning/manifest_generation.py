"""Generate ``agent_registry`` manifests for agents this team produces.

Every agent the agentic-team designer rosters should join the platform with the
batteries-included Agent Cognition Core attached. This module turns a roster
:class:`~agentic_team_provisioning.models.AgenticTeamAgent` into a validated
:class:`~agent_registry.models.AgentManifest` whose ``cognition`` block carries
the core defaults (day-one ``default_guardrails`` seed pack, 90-day episodic
memory, a default-on knowledge graph).

Pure builder: no Postgres, no LLM, no filesystem writes. The API surfaces the
result over a read endpoint; nothing is persisted to the registry's manifest
discovery paths.
"""

from __future__ import annotations

import hashlib
import logging

from agent_registry.models import AgentManifest, CognitionSpec, IOSchema, SourceInfo
from agentic_team_provisioning.agent_env_provisioning import _slug
from agentic_team_provisioning.models import AgenticTeamAgent

logger = logging.getLogger(__name__)

# The registry team key for this service (matches TEAM_CONFIGS in
# unified_api/config.py). This is the manifest ``team`` value — distinct from the
# per-instance team UUID, which only feeds the slugged manifest id.
_TEAM_KEY = "agentic_team_provisioning"

# Where the canonical agent-anatomy contract lives, repo-relative per SourceInfo.
_ANATOMY_REF = "backend/agents/agent_provisioning_team/AGENT_ANATOMY.md"

# The invokable sandbox entrypoint: a single callable that accepts one request
# body (carrying the roster metadata + message), reconstructs the agent, and runs
# it through the cognition-aware wrapper. The dispatch shim calls it as
# ``entrypoint(body)``, so it must take exactly the request body.
_ENTRYPOINT = "agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"

# Dotted refs to the Pydantic models describing the invoke contract (resolved
# lazily by the registry, never imported at load time).
_INPUT_SCHEMA_REF = "agentic_team_provisioning.models:GeneratedAgentInvokeInput"
_OUTPUT_SCHEMA_REF = "agentic_team_provisioning.models:GeneratedAgentInvokeOutput"


def _hash8(s: str) -> str:
    """Short stable hex digest used to keep derived ids injective."""
    return hashlib.sha1(s.encode()).hexdigest()[:8]


def team_id_prefix(team_id: str) -> str:
    """Manifest-id prefix shared by every generated agent of one team.

    Postconditions: returns ``"agentic_team_provisioning.<team-slug>-<hash>."`` —
    the common prefix of every id :func:`manifest_agent_id` produces for
    ``team_id``, used to find this team's generated entries in the registry. The
    ``<hash>`` of the *full* ``team_id`` keeps the prefix injective, so two teams
    whose ids share a normalized 12-char slug never collide (stale cleanup keyed
    on this prefix can't touch another team's manifests).
    """
    return f"{_TEAM_KEY}.{_slug(team_id, 12)}-{_hash8(team_id)}."


def manifest_agent_id(team_id: str, agent_name: str) -> str:
    """Stable, collision-free manifest id for a generated agent.

    Preconditions:
        * ``team_id`` and ``agent_name`` are non-empty.
    Postconditions:
        * Deterministic for a given ``(team_id, agent_name)`` pair, and **never**
          collides for distinct pairs: the team-injective :func:`team_id_prefix`
          plus a short hash of the original strings disambiguates names that share
          a normalized slug (e.g. ``"QA Agent"`` and ``"qa-agent"``, or names
          agreeing on their first 40 slug chars). Always starts with
          :func:`team_id_prefix`.
    """
    pair_hash = _hash8(f"{team_id}\x00{agent_name}")
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
    registries (``LlmToolsService`` + ``IntegrationRegistry`` + ``agent_git_tools``),
    so they are never stamped here — that would only break later tool resolution.
    """
    return CognitionSpec(rule_packs=["default_guardrails"])


def build_agent_manifest(team_id: str, agent: AgenticTeamAgent) -> AgentManifest:
    """Build a validated ``agent_registry`` manifest for one roster agent.

    Preconditions:
        * ``team_id`` is a non-empty string.
        * ``agent.agent_name`` is non-empty (guaranteed by ``AgenticTeamAgent``).
    Postconditions:
        * Returns a fully validated :class:`AgentManifest` whose ``cognition``
          equals :func:`default_cognition_block` (``rule_packs ==
          ["default_guardrails"]``), ``team == "agentic_team_provisioning"``,
          ``source.entrypoint`` points at the roster-agent factory, and ``id`` is
          the stable, collision-free :func:`manifest_agent_id`.
    """
    assert team_id, "build_agent_manifest: team_id must be non-empty"
    assert agent.agent_name, "build_agent_manifest: agent.agent_name must be non-empty"

    manifest_id = manifest_agent_id(team_id, agent.agent_name)
    summary = agent.role or f"Generated agent {agent.agent_name}"

    manifest = AgentManifest(
        id=manifest_id,
        team=_TEAM_KEY,
        name=agent.agent_name,
        summary=summary,
        tags=["generated", _TEAM_KEY],
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


def register_team_manifests(team_id: str, agents: list[AgenticTeamAgent]) -> list[AgentManifest]:
    """Build and install the live registry entries for a team's roster.

    Generated agents are not discovered from disk, so they are registered into the
    process-wide :class:`~agent_registry.loader.AgentRegistry` here — making them
    resolvable by the Agent Console catalog and the ``/api/agents/{id}/invoke``
    route (cognition injection + seed-pack install then happen on first invoke).

    Scope: this is the registry of *this process*. It makes generated agents
    resolvable to consumers that share this process. Other processes load their
    own registries from disk and won't see these entries — the Agent Console /
    invoke route when the team runs behind ``AGENTIC_TEAM_PROVISIONING_SERVICE_URL``,
    and every per-invoke sandbox (its shim re-resolves the manifest from its own
    in-container disk registry). Cross-process visibility (shared persistence /
    sync) is a tracked follow-up beyond this generator-wiring change.

    Preconditions:
        * ``team_id`` is non-empty.
    Postconditions:
        * Returns one validated manifest per roster agent, each registered
          (``get_registry().get(m.id)`` returns it). Acts as a full replace for the
          team: previously-registered generated manifests for ``team_id`` that are
          absent from this roster are unregistered, so removed/renamed agents stop
          appearing in the catalog. In-memory and idempotent. Best-effort — a
          registry failure is logged, never raised, so generation still succeeds.
    """
    manifests = [build_agent_manifest(team_id, a) for a in agents]
    new_ids = {m.id for m in manifests}
    try:
        from agent_registry import get_registry

        registry = get_registry()
        # Drop stale entries from a prior roster (removed/renamed agents) before
        # registering the replacement set. Scope strictly to this team's generated
        # ids (prefix + the "generated" tag) so a hand-authored disk manifest is
        # never touched.
        prefix = team_id_prefix(team_id)
        stale = [
            m.id
            for m in registry.all()
            if m.id.startswith(prefix) and "generated" in m.tags and m.id not in new_ids
        ]
        for agent_id in stale:
            registry.unregister(agent_id)
        for manifest in manifests:
            registry.register(manifest)
    except Exception:
        logger.warning("Could not register generated manifests for team %s", team_id, exc_info=True)
    return manifests
