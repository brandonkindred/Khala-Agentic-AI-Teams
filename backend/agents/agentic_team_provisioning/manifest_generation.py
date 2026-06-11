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

from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from agentic_team_provisioning.agent_env_provisioning import _slug
from agentic_team_provisioning.models import AgenticTeamAgent

# The registry team key for this service (matches TEAM_CONFIGS in
# unified_api/config.py). This is the manifest ``team`` value — distinct from the
# per-instance team UUID, which only feeds the slugged manifest id.
_TEAM_KEY = "agentic_team_provisioning"

# Where the canonical agent-anatomy contract lives, repo-relative per SourceInfo.
_ANATOMY_REF = "backend/agents/agent_provisioning_team/AGENT_ANATOMY.md"

# The factory that compiles a roster agent into a live strands.Agent.
_ENTRYPOINT = "agentic_team_provisioning.runtime.agent_builder:build_agent"


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
          stable for a given ``(team_id, agent.agent_name)`` pair.
    """
    assert team_id, "build_agent_manifest: team_id must be non-empty"
    assert agent.agent_name, "build_agent_manifest: agent.agent_name must be non-empty"

    manifest_id = f"{_TEAM_KEY}.{_slug(team_id, 12)}.{_slug(agent.agent_name, 40)}"
    summary = agent.role or f"Generated agent {agent.agent_name}"

    manifest = AgentManifest(
        id=manifest_id,
        team=_TEAM_KEY,
        name=agent.agent_name,
        summary=summary,
        tags=["generated", _TEAM_KEY],
        cognition=default_cognition_block(),
        source=SourceInfo(entrypoint=_ENTRYPOINT, anatomy_ref=_ANATOMY_REF),
    )
    # Round-trip through a JSON-safe dump so the returned object is guaranteed
    # to be a fully validated manifest (and safe to serialize over the API).
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))
