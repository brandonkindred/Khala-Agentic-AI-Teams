"""Deepthought query graph — lightweight wrapper for Strands observability.

The recursive dynamic agent tree (DeepthoughtAgent) doesn't map cleanly
to a static Graph/Swarm since it spawns N sub-agents per query at runtime
with depth-limited recursion. This graph wraps the execution as a single
node for consistent orchestration patterns and Strands event observability.

For future enhancement: per-depth-level coordination could optionally
use a Swarm for sub-agent collaboration at each recursion level.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_query_graph() -> Graph:
    """Build the Deepthought query graph (single-node wrapper).

    The internal recursive agent tree handles its own decomposition,
    delegation, and synthesis. This graph provides a Strands Graph
    entry point for observability.
    """
    return build_sequential(
        stages=[
            ("deepthought", build_agent(
                name="deepthought_processor",
                system_prompt=(
                    "You are Deepthought, a recursive reasoning agent. For complex queries, "
                    "decompose the problem into sub-problems, analyze each from multiple "
                    "specialist perspectives, and synthesize findings into a comprehensive "
                    "answer. Use the appropriate decomposition strategy: sequential for "
                    "dependent sub-problems, parallel for independent ones, hierarchical "
                    "for multi-level analysis. Return a thorough, well-structured response."
                ),
                agent_key="deepthought",
                description="Recursive multi-perspective query processor",
            )),
        ],
        graph_id="deepthought_query",
        execution_timeout=600.0,
        node_timeout=300.0,
    )
