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

from agent_registry.models import AgentManifest, AgentStateSpec, CognitionSpec, IOSchema, SourceInfo

from .agent_states import default_agent_states
from .models import AgentDefinition, AgentState

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
    if not name.strip():
        raise ValueError("studio_agent_id: name must be non-empty")
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
        * The definition's operating ``states`` are persisted onto ``manifest.states``
          (inert metadata — see :class:`AgentStateSpec`).
    """
    if not definition.name.strip():
        raise ValueError("build_studio_agent_manifest: name must be non-empty")
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
        states=[
            AgentStateSpec(key=s.key, label=s.label, system_prompt=s.system_prompt)
            for s in definition.states
        ],
        source=SourceInfo(entrypoint=_GEN_ENTRYPOINT, anatomy_ref=_ANATOMY_REF),
    )
    # Round-trip so the returned object is guaranteed fully validated and serializable.
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))


def clone_from_manifest(manifest: AgentManifest) -> AgentDefinition:
    """Project an existing registry manifest into an editable refine-mode draft.

    The source manifest is never mutated — this returns a *new* definition.

    Only the fields the manifest carries are cloned (name → ``<name>.copy``,
    ``summary`` → role, description, tags, cognition tools, operating states).
    ``system_prompt``, ``input_schema``, and ``output_schema`` are **not**
    transferred because the registry manifest does not store them (inputs/outputs
    are dotted ``schema_ref``s, not inline schemas) — consistent with the deferred
    "authored inline schemas" follow-up. The refine conversation re-elicits them.

    The operating ``states`` ARE transferred (the manifest persists them). Cloning
    a legacy manifest saved before states existed (empty ``states``) back-fills the
    three default seeded states, so every refine draft has them.

    Postconditions:
        * ``mode == "refine"`` and ``cloned_from == manifest.id``.
        * The returned definition has exactly three operating states.
    """
    tools = list(manifest.cognition.tools) if manifest.cognition else []
    tags = [t for t in manifest.tags if t not in _PLUMBING_TAGS]
    states = (
        [
            AgentState(key=s.key, label=s.label, system_prompt=s.system_prompt)
            for s in manifest.states
        ]
        if manifest.states
        else default_agent_states()
    )
    # Avoid a confusing "name.copy.copy" when cloning an already-cloned name.
    # Per-team disambiguation (".copy-2", …) is the frontend's job — it knows the
    # team's existing names; the backend only avoids the doubled suffix here.
    name = manifest.name if manifest.name.endswith(".copy") else f"{manifest.name}.copy"
    return AgentDefinition(
        name=name,
        role=manifest.summary,
        description=manifest.description,
        tags=tags,
        tools=tools,
        states=states,
        mode="refine",
        cloned_from=manifest.id,
    )
