"""Phase 2 — Narrative & Messaging graph (sequential specialists).

Six agents run in a fixed pipeline: Storyteller → ArchetypeAnalyst →
TaglineWriter → MessageMapper → PersonaBuilder → VoicePrinciplesDrafter.

This is intentionally a Graph rather than a Swarm. Agents built with
``structured_output=`` force Strands' structured-output tool and then
stop the agent loop (``stop_loop=True``), so they never call
``handoff_to_agent``. A Swarm therefore completes after the entry node and
drops every downstream fragment. Graph edges sequence the specialists
without relying on tool-based handoffs.
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


def build_phase2_graph() -> Graph:
    """Build the Phase 2 Narrative & Messaging sequential graph.

    Topology::

        Storyteller → ArchetypeAnalyst → TaglineWriter → MessageMapper
            → PersonaBuilder → VoicePrinciplesDrafter

    Each node emits its own ``structured_output`` fragment; the orchestrator
    merges them into ``NarrativeMessagingOutput``.

    Returns:
        A configured ``Graph`` instance ready for invocation.
    """
    builder = GraphBuilder()

    storyteller = builder.add_node(make_storyteller(), node_id="Storyteller")
    archetype = builder.add_node(make_archetype_analyst(), node_id="ArchetypeAnalyst")
    tagline = builder.add_node(make_tagline_writer(), node_id="TaglineWriter")
    message = builder.add_node(make_message_mapper(), node_id="MessageMapper")
    persona = builder.add_node(make_persona_builder(), node_id="PersonaBuilder")
    voice = builder.add_node(make_voice_principles_drafter(), node_id="VoicePrinciplesDrafter")

    builder.set_entry_point("Storyteller")
    builder.add_edge(storyteller, archetype)
    builder.add_edge(archetype, tagline)
    builder.add_edge(tagline, message)
    builder.add_edge(message, persona)
    builder.add_edge(persona, voice)

    return builder.build()


# Back-compat alias — Phase 2 used to be a Swarm; callers that still import
# the old name get the Graph that preserves structured_output sequencing.
build_phase2_swarm = build_phase2_graph
