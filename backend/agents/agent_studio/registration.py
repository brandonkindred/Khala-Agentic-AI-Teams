"""Clone-from-registry and save+register for authored Studio agents.

Saving reuses the **generated-agent runtime**: a saved Studio agent is registered
into the process-wide ``agent_registry`` with the same entrypoint and invoke
schemas that ``agentic_team_provisioning`` stamps onto generated team agents, so
it resolves via ``GET /api/agents/{id}`` and runs via ``POST /api/agents/{id}/invoke``
without a YAML file.

Runtime-binding caveat (inherited from generated agents): the generated runtime
reconstructs the persona from the *invoke request body*, not the stored manifest,
so a saved agent's persisted ``role`` / ``system_prompt`` are advertised but not
yet bound at invoke time. Binding is the same tracked follow-up generated team
agents carry; out of scope for this Stage-1 backend slice.

Registration is in-process only (see :func:`agent_registry.get_registry`).
"""

from __future__ import annotations

import hashlib
import re

from agent_registry.models import AgentManifest, CognitionSpec, IOSchema, SourceInfo

from .models import AgentDefinition

# The registry "team" Studio agents are filed under (must match a TEAM_CONFIGS key).
STUDIO_TEAM = "agent_studio"

# Shared generated-agent runtime — reused so a saved Studio agent is invokable
# exactly like a generated team agent.
_GEN_ENTRYPOINT = "agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
_GEN_INPUT_REF = "agentic_team_provisioning.models:GeneratedAgentInvokeInput"
_GEN_OUTPUT_REF = "agentic_team_provisioning.models:GeneratedAgentInvokeOutput"
_ANATOMY_REF = "backend/agents/agent_provisioning_team/AGENT_ANATOMY.md"

# Tags that describe the registry plumbing rather than the agent itself; stripped
# when projecting a manifest back into an editable definition.
_PLUMBING_TAGS = frozenset({"studio", "generated", "agentic_team_provisioning"})


def studio_agent_id(name: str) -> str:
    """Return a stable, collision-resistant registry id for a Studio agent.

    Preconditions:
        * ``name`` is non-empty.
    Postconditions:
        * Returns ``agent_studio.<slug>-<hash8>``; equal names map to equal ids.
    """
    assert name.strip(), "studio_agent_id: name must be non-empty"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
    # Non-cryptographic: a short, stable suffix that disambiguates equal slugs.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{STUDIO_TEAM}.{slug}-{digest}"


def build_studio_agent_manifest(definition: AgentDefinition) -> AgentManifest:
    """Build a validated, invokable ``agent_registry`` manifest for a definition.

    Preconditions:
        * ``definition.name`` is non-empty.
    Postconditions:
        * Returns a fully validated :class:`AgentManifest` filed under
          ``team == STUDIO_TEAM`` whose ``source.entrypoint`` and invoke schemas
          are the shared generated-agent runtime, and whose ``cognition`` carries
          the ``default_guardrails`` seed pack.
    """
    assert definition.name.strip(), "build_studio_agent_manifest: name must be non-empty"
    manifest = AgentManifest(
        id=studio_agent_id(definition.name),
        team=STUDIO_TEAM,
        name=definition.name,
        summary=definition.role or f"Studio agent {definition.name}",
        description=definition.description,
        tags=sorted({"studio", *definition.tags}),
        inputs=IOSchema(
            schema_ref=_GEN_INPUT_REF,
            description="Roster metadata + user message (shared generated-agent entrypoint).",
        ),
        outputs=IOSchema(schema_ref=_GEN_OUTPUT_REF, description="The agent's response text."),
        cognition=CognitionSpec(rule_packs=["default_guardrails"], tools=list(definition.tools)),
        source=SourceInfo(entrypoint=_GEN_ENTRYPOINT, anatomy_ref=_ANATOMY_REF),
    )
    # Round-trip so the returned object is guaranteed fully validated and serializable.
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))


def clone_from_manifest(manifest: AgentManifest) -> AgentDefinition:
    """Project an existing registry manifest into an editable refine-mode draft.

    The source manifest is never mutated — this returns a *new* definition.

    Postconditions:
        * ``mode == "refine"`` and ``cloned_from == manifest.id``.
    """
    tools = list(manifest.cognition.tools) if manifest.cognition else []
    tags = [t for t in manifest.tags if t not in _PLUMBING_TAGS]
    return AgentDefinition(
        name=f"{manifest.name}.copy",
        role=manifest.summary,
        description=manifest.description,
        tags=tags,
        tools=tools,
        mode="refine",
        cloned_from=manifest.id,
    )
