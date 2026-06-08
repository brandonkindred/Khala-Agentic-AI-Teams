"""Graph-backed context retrieval — the read counterpart of the sync worker.

:func:`build_graph_context` is the async analogue of
:func:`agent_cognition.memory.retrieval.build_memory_digest`: where the memory
digest stitches the rote rollup summaries, this runs a Graphiti hybrid search
(semantic + keyword + graph distance, recency-aware) scoped to the agent's
``group_id`` and renders the most relevant *related* facts into a bounded block.
It is injected alongside the memory digest at invoke time and reused by the
rule-reflection grounding.

Budget contract mirrors ``build_memory_digest`` (token→char at 4×, hard-capped),
but it is hot-path-friendly: it never makes an LLM compaction call (the result set
is already bounded by ``num_results``) and it never raises — a disabled graph,
empty query, empty result, or any Graphiti error yields ``""`` so a graph hiccup
can never fail an agent invoke.
"""

from __future__ import annotations

import logging
import os

from shared_neo4j import get_graphiti, is_neo4j_enabled

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4
_DEFAULT_TOP_K = 10


def _search_top_k() -> int:
    """Max graph facts retrieved per query (env ``AGENT_COGNITION_GRAPH_SEARCH_TOP_K``)."""
    raw = os.getenv("AGENT_COGNITION_GRAPH_SEARCH_TOP_K")
    if raw is None:
        return _DEFAULT_TOP_K
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TOP_K
    return value if value >= 1 else _DEFAULT_TOP_K


async def build_graph_context(agent_id: str, query: str, token_budget: int) -> str:
    """Build the bounded knowledge-graph context block for an agent + query.

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``token_budget >= 0``.
    Postconditions:
        * Returns ``""`` when the graph layer is disabled, the budget is 0, the
          query is blank, the search returns nothing, or the search errors — never
          raises, so the caller can compose it with the memory digest without
          special-casing graph availability.
        * Otherwise returns a ``## Knowledge graph`` block of the top related
          facts (scoped to ``group_ids=[agent_id]``), hard-capped to
          ``token_budget * 4`` characters.
    """
    assert agent_id, "build_graph_context: agent_id must be non-empty"
    assert token_budget >= 0, "build_graph_context: token_budget must be non-negative"

    if token_budget == 0 or not is_neo4j_enabled() or not query or not query.strip():
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

    block = "## Knowledge graph\n" + "\n".join(f"- {fact}" for fact in facts)
    char_budget = token_budget * _CHARS_PER_TOKEN
    if len(block) > char_budget:
        block = block[:char_budget]
    return block
