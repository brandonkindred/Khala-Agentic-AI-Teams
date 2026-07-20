"""Shared Neo4j / Graphiti knowledge-graph layer.

The graph counterpart to ``shared_postgres``: a thin, env-gated wrapper around a
process-wide `Graphiti <https://github.com/getzep/graphiti>`_ client that owns the
Neo4j async driver. Graphiti ingests agent memories as temporal episodes and
extracts entities/relationships with bi-temporal (recency-aware) edges, partitioned
per agent via Graphiti's ``group_id`` (set to the ``agent_id``).

Enablement is gated on ``NEO4J_BOLT_URL``. A real deployment always runs Neo4j as
required infrastructure (Graphiti depends on it); the disabled path exists only so
the unit-test suite can run against a faked Graphiti without a live database —
mirroring how ``shared_postgres`` runs without a live Postgres.

Typical usage::

    from shared_neo4j import is_neo4j_enabled, get_graphiti, close_graphiti

    if is_neo4j_enabled():
        graphiti = get_graphiti()
        await graphiti.add_episode(group_id=agent_id, ...)
    # at shutdown:
    await close_graphiti()
"""

from shared_neo4j.client import GraphUnavailable, close_graphiti, get_graphiti
from shared_neo4j.config import is_neo4j_enabled
from shared_neo4j.metrics import timed_graph_op
from shared_neo4j.schema import GRAPH_SCHEMA, GraphSchema, register_graph_indices

__all__ = [
    "GRAPH_SCHEMA",
    "GraphSchema",
    "GraphUnavailable",
    "close_graphiti",
    "get_graphiti",
    "is_neo4j_enabled",
    "register_graph_indices",
    "timed_graph_op",
]
