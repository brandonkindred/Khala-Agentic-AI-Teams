"""Graph index/constraint registration — the Graphiti analogue of ``TeamSchema``.

``shared_postgres`` exports a ``TeamSchema`` of DDL statements run at startup;
Graphiti instead owns its own schema and exposes
``build_indices_and_constraints()`` to create the Neo4j indexes and constraints it
needs. :func:`register_graph_indices` is the one-call wrapper a FastAPI lifespan
(or the graph sync worker) invokes once at startup. It is a no-op when the layer
is gated off, mirroring ``register_team_schemas`` returning early without Postgres.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shared_neo4j import config
from shared_neo4j.client import get_graphiti

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphSchema:
    """Identity of the knowledge graph's schema (parity with ``TeamSchema``).

    Graphiti manages the concrete indexes/constraints itself, so this carries no
    DDL — only a name for logging and a marker that the layer owns a graph schema.
    """

    name: str = "agent_cognition_knowledge_graph"


GRAPH_SCHEMA = GraphSchema()


async def register_graph_indices() -> bool:
    """Create Graphiti's Neo4j indexes/constraints (idempotent).

    Postconditions:
        * Returns ``False`` without touching Neo4j when the layer is gated off
          (``NEO4J_BOLT_URL`` unset).
        * Otherwise builds Graphiti's indices/constraints and returns ``True``.
          Graphiti's ``build_indices_and_constraints`` is itself idempotent, so
          re-running at every startup is safe.
    """
    if not config.is_neo4j_enabled():
        logger.debug("register_graph_indices: NEO4J_BOLT_URL unset; skipping")
        return False
    graphiti = get_graphiti()
    await graphiti.build_indices_and_constraints()
    logger.info("shared_neo4j graph indices/constraints ensured (%s)", GRAPH_SCHEMA.name)
    return True
