"""Environment resolution for the shared Neo4j / Graphiti knowledge-graph layer.

Mirrors ``shared_postgres.client``'s env-helper style. Connection details and the
Graphiti LLM/embedder wiring are resolved from environment variables at call time
(so tests and operators can override per-process).

Env vars:

    NEO4J_BOLT_URL       Bolt URL of the Neo4j server, e.g. ``bolt://neo4j:7687``.
                         This is the **enablement gate** — when it is unset the
                         knowledge-graph layer is inert (the only tolerated unset
                         case is the unit-test harness, which fakes Graphiti).
    NEO4J_USER           Neo4j username (default ``neo4j``).
    NEO4J_PASSWORD       Neo4j password (default empty).
    NEO4J_DATABASE       Neo4j database name (default ``neo4j``).

    GRAPHITI_LLM_MODEL   Model Graphiti uses for entity/edge extraction. Defaults
                         to the platform's resolved ``cognition`` model.
    GRAPHITI_EMBED_MODEL Embedding model for Graphiti hybrid search (default
                         ``nomic-embed-text``).
    GRAPHITI_EMBED_DIM   Embedding dimensionality (default ``768``, matching
                         ``nomic-embed-text``).

Graphiti talks to the platform's Ollama (Cloud or local) through its
OpenAI-compatible endpoint, so the LLM base URL and API key are reused from the
shared ``LLM_BASE_URL`` / ``OLLAMA_API_KEY`` settings.

Invariants:
    * ``is_neo4j_enabled()`` is the single source of truth for whether the layer
      is active. It is a *test-harness seam*, not a product feature flag — a real
      deployment always sets ``NEO4J_BOLT_URL`` and runs Neo4j as required infra.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Defaults chosen to match the docker-compose Neo4j service and the
# ``nomic-embed-text`` embedding model (768-dim) Graphiti is configured against.
_DEFAULT_NEO4J_USER = "neo4j"
_DEFAULT_NEO4J_DATABASE = "neo4j"
_DEFAULT_EMBED_MODEL = "nomic-embed-text"
_DEFAULT_EMBED_DIM = 768


def is_neo4j_enabled() -> bool:
    """True when ``NEO4J_BOLT_URL`` is set.

    Postconditions:
        * Returns ``True`` iff a non-blank ``NEO4J_BOLT_URL`` is present. A real
          deployment always sets it (Neo4j is required infra); an unset value is
          tolerated only so the unit-test suite can run against a faked Graphiti
          without standing up a database.
    """
    return bool(os.getenv("NEO4J_BOLT_URL", "").strip())


def neo4j_bolt_url() -> str:
    """Bolt URL of the Neo4j server.

    Preconditions:
        * ``is_neo4j_enabled()`` — callers must gate on it; this raises otherwise
          so a misconfigured deployment fails loudly instead of connecting to a
          bogus default.
    """
    url = os.getenv("NEO4J_BOLT_URL", "").strip()
    assert url, "NEO4J_BOLT_URL is not set; gate on is_neo4j_enabled() before calling."
    return url


def neo4j_user() -> str:
    return (os.getenv("NEO4J_USER") or _DEFAULT_NEO4J_USER).strip()


def neo4j_password() -> str:
    # No default of substance — an empty password is only viable against a Neo4j
    # started with auth disabled (not the shipped compose config).
    return os.getenv("NEO4J_PASSWORD", "")


def neo4j_database() -> str:
    return (os.getenv("NEO4J_DATABASE") or _DEFAULT_NEO4J_DATABASE).strip()


def graphiti_llm_model() -> str:
    """Model Graphiti uses for extraction.

    ``GRAPHITI_LLM_MODEL`` wins; otherwise fall back to the platform's resolved
    ``cognition`` model so the graph reasons with the same model as the rest of
    the cognition stack. The import is lazy so this module stays importable when
    ``llm_service`` (or its deps) are unavailable.
    """
    override = (os.getenv("GRAPHITI_LLM_MODEL") or "").strip()
    if override:
        return override
    try:
        from llm_service.config import resolve_model  # noqa: PLC0415

        return resolve_model("cognition")
    except Exception:  # pragma: no cover - defensive; llm_service always present in-app
        logger.warning("graphiti_llm_model: could not resolve cognition model; using fallback")
        return "deepseek-v4-pro:cloud"


def graphiti_embed_model() -> str:
    return (os.getenv("GRAPHITI_EMBED_MODEL") or _DEFAULT_EMBED_MODEL).strip()


def graphiti_embed_dim() -> int:
    """Embedding dimensionality, falling back to the default on garbage input."""
    raw = os.getenv("GRAPHITI_EMBED_DIM")
    if raw is None:
        return _DEFAULT_EMBED_DIM
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_EMBED_DIM
    return value if value >= 1 else _DEFAULT_EMBED_DIM


def llm_base_url() -> str:
    """Base URL of the platform LLM endpoint (Ollama), used to derive the
    OpenAI-compatible URL Graphiti's clients expect.

    Reuses ``llm_service``'s resolver so the graph and the rest of the platform
    point at the same endpoint; falls back to the Ollama Cloud default.
    """
    try:
        from llm_service.config import resolve_base_url  # noqa: PLC0415

        return resolve_base_url()
    except Exception:  # pragma: no cover - defensive
        return (os.getenv("LLM_BASE_URL") or "https://ollama.com").strip().rstrip("/")


def openai_compatible_base_url() -> str:
    """The OpenAI-compatible ``/v1`` base URL Graphiti's OpenAI clients call.

    Ollama (Cloud and local) exposes an OpenAI-compatible surface at ``{base}/v1``.
    """
    return f"{llm_base_url()}/v1"


def ollama_api_key() -> str:
    """API key forwarded to Graphiti's OpenAI-compatible clients.

    Ollama Cloud authenticates with ``OLLAMA_API_KEY``; a local Ollama ignores
    it but the OpenAI client still requires a non-empty string, so we fall back
    to a placeholder when unset.
    """
    return os.getenv("OLLAMA_API_KEY") or "ollama"
