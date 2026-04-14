"""Social media marketing campaign graph.

Top-level graph composing the consensus swarm with platform specialists
and experiment design.

Topology::

    consensus_swarm → concept_generation → fan-out(4 platforms) → experiment_design

Platform specialists (LinkedIn, Facebook, Instagram, X) run in parallel.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from shared_graph import build_agent

from .consensus_swarm import build_consensus_swarm


def build_campaign_graph() -> Graph:
    """Build the full campaign planning graph."""
    builder = GraphBuilder()
    builder.set_graph_id("social_media_campaign")
    builder.set_execution_timeout(600.0)
    builder.set_node_timeout(180.0)

    # Phase 1: Consensus swarm for campaign concept
    consensus = builder.add_node(
        build_consensus_swarm(),
        node_id="consensus",
    )
    builder.set_entry_point("consensus")

    # Phase 2: Concept generation from consensus output
    concept_gen = builder.add_node(
        build_agent(
            name="concept_generator",
            system_prompt=(
                "You are a campaign concept finalizer. Take the consensus output and "
                "produce a detailed campaign concept with messaging, visual direction, "
                "tone, and platform adaptation guidelines. Return structured JSON."
            ),
            description="Finalizes campaign concept from consensus",
        ),
        node_id="concept_generation",
    )
    builder.add_edge(consensus, concept_gen)

    # Phase 3: Platform specialists (fan-out)
    platforms = {
        "linkedin": "LinkedIn (B2B focus, thought leadership, professional tone)",
        "facebook": "Facebook (community engagement, storytelling, broad reach)",
        "instagram": "Instagram (visual-first, stories/reels, lifestyle branding)",
        "x_twitter": "X/Twitter (real-time engagement, threads, concise messaging)",
    }

    platform_nodes = []
    for platform_id, platform_desc in platforms.items():
        node = builder.add_node(
            build_agent(
                name=f"{platform_id}_specialist",
                system_prompt=(
                    f"You are a {platform_desc} specialist. Adapt the campaign concept "
                    f"for this platform's best practices, format constraints, and audience. "
                    f"Produce platform-specific content variants, posting schedule, and "
                    f"engagement tactics. Return structured JSON."
                ),
                description=f"Adapts campaign for {platform_id}",
            ),
            node_id=platform_id,
        )
        builder.add_edge(concept_gen, node)
        platform_nodes.append(node)

    # Phase 4: Experiment design (fan-in from all platforms)
    experiment = builder.add_node(
        build_agent(
            name="experiment_designer",
            system_prompt=(
                "You are a marketing experiment designer. Based on all platform-specific "
                "content plans, design A/B tests and measurement frameworks. Define KPIs, "
                "control groups, test duration, and success criteria. Return structured JSON."
            ),
            description="Designs experiments and measurement frameworks",
        ),
        node_id="experiment_design",
    )
    for pn in platform_nodes:
        builder.add_edge(pn, experiment)

    return builder.build()
