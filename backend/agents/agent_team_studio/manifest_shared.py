"""Constants and helpers shared by every package that builds an ``AgentManifest``.

Both ``agent_studio`` (Stage-1 single-agent authoring) and
``agentic_team_provisioning`` (team roster/process authoring) register agents
into the same process-wide ``agent_platform.registry`` through the shared
generated-agent runtime (``runtime.agent_builder:invoke_generated_agent``).
Before this module existed, each package independently hardcoded the same
dotted refs and tag-filtering pattern; this module is their single source so
the two never drift.

Nothing here performs I/O — these are pure constants and pure functions only.
"""

from __future__ import annotations

from agent_platform.registry.models import (
    CognitionKnowledgeGraphSpec,
    CognitionMemorySpec,
    CognitionSpec,
)

# The invokable sandbox entrypoint every generated/authored agent shares: one
# callable that accepts the roster metadata + message, reconstructs the agent,
# and runs it through the cognition-aware wrapper.
GENERATED_AGENT_ENTRYPOINT = (
    "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
)

# Dotted refs to the Pydantic models describing the shared entrypoint's invoke
# contract (resolved lazily by the registry, never imported at manifest-build time).
GENERATED_AGENT_INPUT_REF = (
    "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeInput"
)
GENERATED_AGENT_OUTPUT_REF = (
    "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeOutput"
)

# Where the canonical agent-anatomy contract lives, repo-relative per SourceInfo.
AGENT_ANATOMY_REF = "backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md"

# The day-one Agent Cognition Core seed pack every authored/generated manifest carries.
DEFAULT_RULE_PACKS: tuple[str, ...] = ("default_guardrails",)


def strip_marker_tags(tags: list[str], markers: frozenset[str]) -> list[str]:
    """Return ``tags`` excluding a caller-supplied marker set, order preserved.

    Preconditions:
        * ``tags`` and ``markers`` are non-``None`` (an empty collection is fine).
    Postconditions:
        * Returns a new list containing every entry of ``tags`` not present in
          ``markers``, in the same relative order; ``tags`` is not mutated.
    """
    return [t for t in tags if t not in markers]


def default_cognition_block() -> CognitionSpec:
    """Return the batteries-included Agent Cognition Core defaults.

    Preconditions:
        * None.
    Postconditions:
        * Returns a :class:`CognitionSpec` equal to the batteries-included core:
          90-day episodic memory (``memory.retention_days_events == 90``), an
          empty ``tools`` list, ``requires_idempotency_key`` False, a default-on
          knowledge graph, and exactly one seed pack — ``default_guardrails``.

    ``tools`` is deliberately empty here: it's caller-specific (a roster agent's
    free-text tool labels vs. a Studio agent's resolved tool ids), so every
    caller starts from this block and overrides ``tools`` itself, e.g. via
    ``default_cognition_block().model_copy(update={"tools": [...]})``.
    """
    return CognitionSpec(
        memory=CognitionMemorySpec(retention_days_events=90),
        tools=[],
        rule_packs=list(DEFAULT_RULE_PACKS),
        requires_idempotency_key=False,
        knowledge_graph=CognitionKnowledgeGraphSpec(),
    )
