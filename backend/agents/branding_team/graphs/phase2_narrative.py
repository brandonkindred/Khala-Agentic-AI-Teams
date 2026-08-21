"""Phase 2 — Narrative & Messaging graph (pure fan-out).

Six specialist agents produce narrative and messaging artefacts in parallel.
Each is a terminal node — there is no compositor; the orchestrator's Phase-2
merge function assembles their typed fragments into a unified
``NarrativeMessagingOutput`` in Python.
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
    """Build the Phase 2 Narrative & Messaging pure fan-out graph.

    Topology::

        Storyteller
        ArchetypeAnalyst
        TaglineWriter
        MessageMapper
        PersonaBuilder
        VoicePrinciplesDrafter

    All six nodes are both entry points and terminal nodes — they run in
    parallel and have no edges between them. There is no fan-in node: the
    orchestrator's Phase-2 merge function assembles their typed
    ``structured_output`` fragments into a single
    ``NarrativeMessagingOutput`` deterministically in Python.

    Preconditions:
        None — the builder wires a fixed six-agent factory set and takes no
        arguments.
    Postconditions:
        Returns a built ``Graph`` whose six specialist nodes are all both entry
        points and terminal nodes, running in parallel with no edges between them.
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
    for node_id, factory in factories.items():
        builder.add_node(factory(), node_id=node_id)
        builder.set_entry_point(node_id)

    return builder.build()


# Back-compat alias — Phase 2 used to be a Swarm; callers that still import
# the old name get the Graph that preserves structured_output sequencing.
build_phase2_swarm = build_phase2_graph
