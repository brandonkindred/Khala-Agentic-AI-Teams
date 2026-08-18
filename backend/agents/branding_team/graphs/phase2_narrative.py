"""Phase 2 — Narrative & Messaging graph (linear specialists).

Six agents run Storyteller → ArchetypeAnalyst → TaglineWriter →
MessageMapper → PersonaBuilder → VoicePrinciplesDrafter.

This is intentionally a Graph rather than a Swarm. Agents built with
``structured_output=`` force Strands' structured-output tool and then
stop the agent loop (``stop_loop=True``), so they never call
``handoff_to_agent``.

Edges are a *single-predecessor chain*. Strands' readiness check treats
multiple incoming edges as OR (any one satisfied predecessor makes the
node ready), so a fan-in would launch every downstream agent as soon as
Storyteller finished. The single-predecessor edge into each node is what
makes Strands auto-populate ``Inputs from previous nodes`` with the
immediate predecessor's typed output; each specialist's own-field
``structured_output`` model (Story 5b Step 1) only ever emits its own new
fields and reads the predecessor's output from there as read-only context.
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

_PHASE2_NODE_ORDER: tuple[str, ...] = (
    "Storyteller",
    "ArchetypeAnalyst",
    "TaglineWriter",
    "MessageMapper",
    "PersonaBuilder",
    "VoicePrinciplesDrafter",
)


def build_phase2_graph() -> Graph:
    """Build the Phase 2 Narrative & Messaging linear Graph.

    Topology::

        Storyteller → ArchetypeAnalyst → TaglineWriter → MessageMapper
            → PersonaBuilder → VoicePrinciplesDrafter

    Each node emits its own disjoint ``structured_output`` fragment; the
    orchestrator merges them into ``NarrativeMessagingOutput``.

    Preconditions:
        None — the builder wires a fixed six-agent factory set and takes no
        arguments.
    Postconditions:
        Returns a built ``Graph`` with ``Storyteller`` as the entry point and a
        single-predecessor chain through to ``VoicePrinciplesDrafter``, so each
        node runs only after its immediate predecessor (never a premature fan-in).
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
    for prior_id, node_id in zip(_PHASE2_NODE_ORDER, _PHASE2_NODE_ORDER[1:]):
        builder.add_edge(nodes[prior_id], nodes[node_id])

    return builder.build()


# Back-compat alias — Phase 2 used to be a Swarm; callers that still import
# the old name get the Graph that preserves structured_output sequencing.
build_phase2_swarm = build_phase2_graph
