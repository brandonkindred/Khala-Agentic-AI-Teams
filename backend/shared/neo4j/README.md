# shared_neo4j

The graph counterpart to [`shared_postgres`](../shared_postgres/README.md): a thin,
env-gated wrapper around a process-wide [Graphiti](https://github.com/getzep/graphiti)
client that owns the Neo4j async driver. It is the infrastructure layer for the
**knowledge-graph layer over Agent Cognition** — Graphiti ingests agent memories as
temporal episodes and extracts entities/relationships with bi-temporal
(recency-aware) edges, partitioned per agent via Graphiti's `group_id` (set to the
`agent_id`).

## Enablement gate

`is_neo4j_enabled()` returns `True` only when `NEO4J_BOLT_URL` is set.

A real deployment **always** runs Neo4j as required infrastructure — Graphiti depends
on it, so it is not an optional feature flag. The disabled path exists **only** so the
unit-test suite can run against a faked Graphiti without standing up a database,
mirroring how `shared_postgres` runs without a live Postgres. Do not treat the unset
state as a supported production configuration.

## Usage

```python
from shared_neo4j import is_neo4j_enabled, get_graphiti, close_graphiti, register_graph_indices

# at startup (FastAPI lifespan / the graph sync worker):
await register_graph_indices()           # no-op when NEO4J_BOLT_URL is unset

# on a hot path (always in an async context — Graphiti is async):
graphiti = get_graphiti()                # raises GraphUnavailable when disabled
await graphiti.add_episode(group_id=agent_id, ...)
results = await graphiti.search(query, group_ids=[agent_id])

# at shutdown:
await close_graphiti()
```

`get_graphiti()` builds the singleton lazily and is thread-safe; the heavy
`graphiti_core` imports are deferred to first use, so importing this package for a
lint run or an unrelated unit test never requires the dependency to be installed.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `NEO4J_BOLT_URL` | (unset) | **Enablement gate.** Bolt URL of the Neo4j server, e.g. `bolt://neo4j:7687`. |
| `NEO4J_USER` | `neo4j` | Neo4j username. |
| `NEO4J_PASSWORD` | (empty) | Neo4j password. |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database name. |
| `GRAPHITI_LLM_MODEL` | resolved `cognition` model | Model Graphiti uses for entity/edge extraction. |
| `GRAPHITI_EMBED_MODEL` | `nomic-embed-text` | Embedding model for hybrid search. |
| `GRAPHITI_EMBED_DIM` | `768` | Embedding dimensionality (must match the embed model). |
| `NEO4J_SLOW_OP_MS` | `1000` | `timed_graph_op` slow-call log threshold (ms). |

Graphiti talks to the platform's Ollama through its OpenAI-compatible endpoint, so
the LLM/embedder/reranker clients reuse the shared `LLM_BASE_URL` (→ `{base}/v1`)
and `OLLAMA_API_KEY`.

## Per-agent partitioning

Every Graphiti call passes `group_id=agent_id`. There is no physical Neo4j database
per agent — `group_id` is Graphiti's partition key. Groups are created lazily on the
first `add_episode`, so no provision-time initialization is needed. Cross-agent
isolation is enforced by always scoping reads to `group_ids=[agent_id]`.

## Compatibility notes / risks

- **Neo4j ≥ 5.26** is required by Graphiti.
- Graphiti is OpenAI-first and relies on structured-output / function-calling for
  extraction; verify the configured `GRAPHITI_LLM_MODEL` honors it over Ollama's
  `/v1`. The reranker reuses the same endpoint and Graphiti falls back to
  reciprocal-rank fusion when it is unavailable.
- Hybrid search needs an embeddings model on Ollama's `/v1/embeddings`; confirm the
  configured `GRAPHITI_EMBED_MODEL`/dim is available on the deployment tier.
