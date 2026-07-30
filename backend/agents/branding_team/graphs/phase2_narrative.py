"""Phase 2 — Narrative & Messaging graph (sequential specialists).

Six agents run Storyteller → ArchetypeAnalyst → TaglineWriter →
MessageMapper → PersonaBuilder → VoicePrinciplesDrafter.

This is intentionally a Graph rather than a Swarm. Agents built with
``structured_output=`` force Strands' structured-output tool and then
stop the agent loop (``stop_loop=True``), so they never call
``handoff_to_agent``. A Swarm therefore completes after the entry node and
drops every downstream fragment. Graph edges sequence the specialists
without relying on tool-based handoffs.

Each non-entry node has edges from *every* upstream specialist, not just
its immediate predecessor. Strands' ``_build_node_input`` only includes
results from directly incoming edges, so a single-predecessor chain would
drop earlier fragments (e.g. TaglineWriter would see ArchetypeAnalyst but
not Storyteller) even though those prompts depend on the full narrative.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    make_archetype_analyst,
    make_message_mapper,
    make_persona_builder,
    make_storyteller,
    make_tagline_writer,
    make_voice_principles_drafter,
)

# Execution order = merge order. Downstream nodes fan in from every prior id.
_PHASE2_NODE_ORDER: tuple[str, ...] = (
    "Storyteller",
    "ArchetypeAnalyst",
    "TaglineWriter",
    "MessageMapper",
    "PersonaBuilder",
    "VoicePrinciplesDrafter",
)


def build_phase2_graph() -> Graph:
    """Build the Phase 2 Narrative & Messaging cumulative fan-in graph.

    Topology (each node depends on all earlier specialists)::

        Storyteller ─────────────────────────────────────┐
             │                                           │
             ├──────────────▶ ArchetypeAnalyst ──────────┤
             │                     │                     │
             ├─────────────────────┼──▶ TaglineWriter ───┤
             │                     │          │          │
             ├─────────────────────┼──────────┼──▶ MessageMapper ─┐
             │                     │          │          │         │
             └─────────────────────┴──────────┴──────────┴──▶ … ──▶ VoicePrinciplesDrafter

    Each node emits its own ``structured_output`` fragment; the orchestrator
    merges them into ``NarrativeMessagingOutput``.

    Returns:
        A configured ``Graph`` instance ready for invocation.
    """
    builder = GraphBuilder()

    factories = {
        "Storyteller": make_storyteller,
        "ArchetypeAnalyst": make_archetype_analyst,
        "TaglineWriter": make_tagline_writer,
        "MessageMapper": make_message_mapper,
        "PersonaBuilder": make_persona_builder,
        "VoicePrinciplesDrafter": make_voice_principles_drafter,
    }
    nodes = {
        node_id: builder.add_node(factory(), node_id=node_id)
        for node_id, factory in factories.items()
    }

    builder.set_entry_point("Storyteller")
    # Cumulative fan-in: node i waits on — and receives input from — every
    # node j < i, so Strands' dependency-only input builder still sees the
    # full prior narrative (brand story, archetypes, tagline, …).
    for i, node_id in enumerate(_PHASE2_NODE_ORDER):
        if i == 0:
            continue
        for prior_id in _PHASE2_NODE_ORDER[:i]:
            builder.add_edge(nodes[prior_id], nodes[node_id])

    return builder.build()


# Back-compat alias — Phase 2 used to be a Swarm; callers that still import
# the old name get the Graph that preserves structured_output sequencing.
build_phase2_swarm = build_phase2_graph
