"""Startup advisor graph — minimal wrapper.

The startup advisor is already a single conversational Strands Agent.
This graph wraps it for consistent orchestration patterns and enables
future multi-step artifact generation if needed.

Topology::

    advisor (single node, conversational)
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_advisor_graph() -> Graph:
    """Build the startup advisor graph (single-node wrapper)."""
    return build_sequential(
        stages=[
            ("advisor", build_agent(
                name="startup_advisor",
                system_prompt=(
                    "You are an experienced startup advisor specializing in early-stage "
                    "companies. You help founders with strategy, fundraising, product-market "
                    "fit, team building, go-to-market, and operational challenges. "
                    "Provide actionable, specific advice based on the founder's context. "
                    "When generating artifacts (pitch decks, financial models, etc.), "
                    "produce structured output."
                ),
                description="Conversational startup advisor",
            )),
        ],
        graph_id="startup_advisor",
        execution_timeout=300.0,
        node_timeout=180.0,
    )
