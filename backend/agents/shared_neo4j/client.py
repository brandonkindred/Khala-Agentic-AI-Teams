"""Process-wide Graphiti client for the knowledge-graph layer.

Graphiti owns the Neo4j async driver, so this module is the Graphiti analogue of
``shared_postgres.client``: a single lazily-built, lock-guarded ``Graphiti``
instance shared across the process, plus an async teardown. The heavy
``graphiti_core`` imports are deferred to first use so importing this package
(for linting, docs, or a unit test of an unrelated module) never requires the
dependency to be installed — exactly how ``shared_postgres`` defers ``psycopg``.

Graphiti is configured against the platform's Ollama via its OpenAI-compatible
endpoint (see :mod:`shared_neo4j.config`). The LLM client, embedder, and reranker
all point at the same ``/v1`` surface so the layer needs no second provider.

Invariants:
    * At most one ``Graphiti`` instance exists per process; ``get_graphiti`` is
      idempotent and thread-safe.
    * ``get_graphiti`` raises :class:`GraphUnavailable` when the layer is gated
      off (``NEO4J_BOLT_URL`` unset), so callers never silently no-op a write.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from shared_neo4j import config

logger = logging.getLogger(__name__)


class GraphUnavailable(RuntimeError):
    """Raised when a Graphiti client is requested while the layer is disabled."""


_client_lock = threading.Lock()
_graphiti: Any | None = None


def _build_graphiti() -> Any:
    """Construct a configured ``Graphiti`` instance (lazy imports inside).

    Preconditions:
        * ``config.is_neo4j_enabled()`` — checked by :func:`get_graphiti`.
    Postconditions:
        * Returns a ``Graphiti`` wired to Neo4j with OpenAI-compatible LLM,
          embedder, and reranker clients pointed at the platform Ollama endpoint.

    Separated from :func:`get_graphiti` so tests can monkeypatch it with a fake
    builder without a live Neo4j or the ``graphiti_core`` dependency.
    """
    from graphiti_core import Graphiti  # noqa: PLC0415
    from graphiti_core.cross_encoder.openai_reranker_client import (  # noqa: PLC0415
        OpenAIRerankerClient,
    )
    from graphiti_core.embedder.openai import (  # noqa: PLC0415
        OpenAIEmbedder,
        OpenAIEmbedderConfig,
    )
    from graphiti_core.llm_client.config import LLMConfig  # noqa: PLC0415
    from graphiti_core.llm_client.openai_generic_client import (  # noqa: PLC0415
        OpenAIGenericClient,
    )

    base_url = config.openai_compatible_base_url()
    api_key = config.ollama_api_key()
    model = config.graphiti_llm_model()

    llm_config = LLMConfig(api_key=api_key, model=model, base_url=base_url)
    llm_client = OpenAIGenericClient(config=llm_config)
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=api_key,
            embedding_model=config.graphiti_embed_model(),
            embedding_dim=config.graphiti_embed_dim(),
            base_url=base_url,
        )
    )
    # Reranking reuses the same OpenAI-compatible endpoint so the layer depends on
    # a single provider; Graphiti falls back to reciprocal-rank fusion if the
    # reranker is unavailable at query time.
    cross_encoder = OpenAIRerankerClient(config=llm_config)

    graphiti = Graphiti(
        config.neo4j_bolt_url(),
        config.neo4j_user(),
        config.neo4j_password(),
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )
    logger.info(
        "shared_neo4j Graphiti client built: bolt=%s db=%s model=%s embed=%s",
        config.neo4j_bolt_url(),
        config.neo4j_database(),
        model,
        config.graphiti_embed_model(),
    )
    return graphiti


def get_graphiti() -> Any:
    """Return the process-wide ``Graphiti`` instance, building it on first use.

    Preconditions:
        * ``config.is_neo4j_enabled()`` is ``True``.
    Postconditions:
        * Returns the singleton ``Graphiti``; repeated calls return the same
          object until :func:`close_graphiti` resets it.
    Raises:
        GraphUnavailable: when the knowledge-graph layer is gated off.
    """
    if not config.is_neo4j_enabled():
        raise GraphUnavailable("NEO4J_BOLT_URL is not set; the knowledge-graph layer is disabled.")
    global _graphiti
    with _client_lock:
        if _graphiti is None:
            _graphiti = _build_graphiti()
        return _graphiti


async def close_graphiti() -> None:
    """Close the Graphiti client (and its Neo4j driver) at shutdown.

    Postconditions:
        * The singleton is torn down and reset to ``None``. Safe to call when no
          client was ever built and safe to call more than once.
    """
    global _graphiti
    with _client_lock:
        graphiti = _graphiti
        _graphiti = None
    if graphiti is None:
        return
    try:
        await graphiti.close()
        logger.info("shared_neo4j Graphiti client closed")
    except Exception as e:  # pragma: no cover - best-effort teardown
        logger.warning("shared_neo4j Graphiti close failed: %s", e)
