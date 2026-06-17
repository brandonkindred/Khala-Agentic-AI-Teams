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


_GRAPH_HEADER = "## Knowledge graph"


def render_graph_block(facts: list[str], *, header: str = _GRAPH_HEADER) -> str:
    """Render related graph ``facts`` as a bounded markdown block, or ``""``.

    Preconditions:
        * ``facts`` is the (possibly empty) list returned by
          :func:`search_graph_facts`; ``header`` is the block's section title.
    Postconditions:
        * Returns ``""`` for an empty ``facts`` list; otherwise ``header`` followed
          by one ``- <fact>`` line per fact. The sole renderer of the block, so a
          caller wanting a different section title (reflection grounding) passes
          ``header`` rather than rewriting the rendered string.
    """
    if not facts:
        return ""
    return f"{header}\n" + "\n".join(f"- {fact}" for fact in facts)


async def search_graph_facts(agent_id: str, query: str) -> list[str]:
    """Return the related graph facts for an agent + query (the structured search).

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns ``[]`` when the graph layer is disabled, the query is blank, the
          search returns nothing, or the search errors — never raises, so callers
          can render or count without special-casing graph availability.
        * Otherwise returns the top related facts (scoped to
          ``group_ids=[agent_id]``), most relevant first.
    """
    assert agent_id, "search_graph_facts: agent_id must be non-empty"

    if not is_neo4j_enabled() or not query or not query.strip():
        return []

    try:
        results = await get_graphiti().search(
            query=query, group_ids=[agent_id], num_results=_search_top_k()
        )
    except Exception:
        logger.warning("search_graph_facts: graph search failed; returning empty", exc_info=True)
        return []

    return [fact for r in results if (fact := getattr(r, "fact", None))]


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
    return render_graph_block(await search_graph_facts(agent_id, query))
