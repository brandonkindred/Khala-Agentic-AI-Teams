"""Knowledge-graph layer for the Agent Cognition Core.

This subpackage drains an agent's episodic memory and rollup summaries into the
Graphiti/Neo4j knowledge graph (see :mod:`shared_neo4j`) and reads recency-ranked
related knowledge back out. It is the *meaning* layer above the rote day/week/
month/year rollups: Graphiti extracts entities/relationships with bi-temporal
edges, partitioned per agent by ``group_id = agent_id``.

Modules:
    * ``watermark_store`` — per-agent ingestion progress (Postgres).
    * ``sync_worker`` — the background worker that ingests new memory into the graph.
"""
