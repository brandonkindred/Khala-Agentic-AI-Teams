"""Invoke-time cognition facade — assembles the ``CognitionContext`` for a request.

The lean ``CognitiveContext`` the invoke proxy folds onto an agent's request: the
agent's active rules plus a combined memory block (the rote rollup digest stitched
together with the recency-ranked knowledge-graph context). The proxy wraps the
body with this via :func:`agent_cognition.tools.envelope.wrap_request`; the sandbox
shim renders the advisory rules and brokers tool calls against the enforced ones.

Async because it composes a synchronous Postgres read (the digest + rules) with the
asynchronous Graphiti search. Both store reads run in ``asyncio.to_thread`` so the
proxy's event loop is never blocked; the graph search is awaited directly and is
already best-effort (returns ``""`` on any failure), so building the context never
raises on a graph hiccup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from agent_cognition.graph.retrieval import build_graph_context
from agent_cognition.memory.retrieval import build_memory_digest
from agent_cognition.models import CognitionContext, RuleStatus
from agent_cognition.rules import store as rules_store
from agent_cognition.runtime_config import read_positive_int

logger = logging.getLogger(__name__)

_DEFAULT_DIGEST_TOKENS = 1024


def invoke_digest_token_budget() -> int:
    """Token budget for each memory block (env ``AGENT_COGNITION_INVOKE_DIGEST_TOKENS``)."""
    return read_positive_int("AGENT_COGNITION_INVOKE_DIGEST_TOKENS", _DEFAULT_DIGEST_TOKENS)


def extract_query_text(body: Any) -> str:
    """Best-effort natural-language query text from an invoke body for graph search.

    Postconditions:
        * A string for the graph search: the body itself when it is a string, else
          the concatenation of its top-level string values when it is a mapping,
          else ``""``. Never raises.
    """
    if isinstance(body, str):
        return body
    if isinstance(body, Mapping):
        parts = [v for v in body.values() if isinstance(v, str) and v.strip()]
        return " ".join(parts)
    return ""


async def build_cognition_context(
    agent_id: str, *, query: str, token_budget: int | None = None
) -> CognitionContext:
    """Assemble the ``CognitionContext`` injected on an agent invoke.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * ``rules`` are the agent's currently *active* rules (advisory + enforced);
          ``memory_digest`` is the rollup digest and the knowledge-graph context
          joined by a blank line (each omitted when empty). Either may be empty —
          a brand-new agent yields an empty-but-valid context. Never raises on a
          graph failure (the graph block degrades to empty).
    """
    assert agent_id, "build_cognition_context: agent_id must be non-empty"
    budget = token_budget if token_budget is not None else invoke_digest_token_budget()

    # The three reads are independent — fetch them concurrently so a
    # cognition-enabled invoke pays the latency of the slowest, not their sum.
    rules, digest, graph = await asyncio.gather(
        asyncio.to_thread(rules_store.list_rules, agent_id, status=RuleStatus.ACTIVE),
        asyncio.to_thread(build_memory_digest, agent_id, budget),
        build_graph_context(agent_id, query, budget),
    )

    memory_digest = "\n\n".join(block for block in (digest, graph) if block)
    return CognitionContext(rules=rules, memory_digest=memory_digest)
