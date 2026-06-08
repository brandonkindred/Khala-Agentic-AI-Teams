"""Graph-backed context retrieval — the read counterpart of the sync worker.

:func:`build_graph_context` is the async analogue of
:func:`agent_cognition.memory.retrieval.build_memory_digest`: where the memory
digest stitches the rote rollup summaries, this runs a Graphiti hybrid search
(semantic + keyword + graph distance, recency-aware) scoped to the agent's
``group_id`` and renders the most relevant *related* facts into a block.

It is hot-path-friendly: it makes no LLM call (the result set is already bounded
by ``num_results``) and it never raises — a disabled graph, empty query, empty
result, or any Graphiti error yields ``""`` so a graph hiccup can never fail an
agent invoke.
"""

from __future__ import annotations

import logging

from agent_cognition.runtime_config import read_positive_int
from shared_neo4j import get_graphiti, is_neo4j_enabled, timed_graph_op

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 10


def _search_top_k() -> int:
    """Max graph facts retrieved per query (env ``AGENT_COGNITION_GRAPH_SEARCH_TOP_K``)."""
    return read_positive_int("AGENT_COGNITION_GRAPH_SEARCH_TOP_K", _DEFAULT_TOP_K)


@timed_graph_op("build_graph_context")
async def build_graph_context(agent_id: str, query: str) -> str:
    """Build the knowledge-graph context block for an agent + query.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns ``""`` when the graph layer is disabled, the query is blank, the
          search returns nothing, or the search errors — never raises, so the
          caller can compose it with the memory digest without special-casing
          graph availability.
        * Otherwise returns a ``## Knowledge graph`` block of the top related
          facts (scoped to ``group_ids=[agent_id]``).
    """
    assert agent_id, "build_graph_context: agent_id must be non-empty"

    if not is_neo4j_enabled() or not query or not query.strip():
        return ""

    try:
        results = await get_graphiti().search(
            query=query, group_ids=[agent_id], num_results=_search_top_k()
        )
    except Exception:
        logger.warning("build_graph_context: graph search failed; returning empty", exc_info=True)
        return ""

    facts = [fact for r in results if (fact := getattr(r, "fact", None))]
    if not facts:
        return ""

    return "## Knowledge graph\n" + "\n".join(f"- {fact}" for fact in facts)
