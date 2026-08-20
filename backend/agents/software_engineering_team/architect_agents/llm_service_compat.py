"""Compatibility shim for llm_service.capabilities when running standalone.

When the architect-agents package is launched via its own ``main.py`` (which
only places ``architect_agents/`` on ``sys.path``), the full ``llm_service``
package is not importable.  This module re-exports the subset of capability
helpers needed by ``agents/prompts.py`` using a vendored copy of the same
logic that lives in ``llm_service/capabilities.py``.

When running inside the SE-team orchestrator (tests, Temporal worker),
``llm_service`` IS on the path and ``agents/prompts.py`` imports directly
from there — this shim is never loaded in that context.
"""
from __future__ import annotations

# Keep in sync with llm_service/capabilities._BEDROCK_CACHE_SUPPORTED_FRAGMENTS
_BEDROCK_CACHE_SUPPORTED_FRAGMENTS: tuple[str, ...] = (
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    "claude-3-7-sonnet",
    "claude-3-5-sonnet-20241022-v2",
    "claude-3-5-haiku",
    "amazon.nova",
)


def bedrock_model_supports_prompt_caching(model_id: str) -> bool:
    """Standalone-compatible version of llm_service.capabilities predicate."""
    if not model_id:
        return True
    model_lower = model_id.lower()
    return any(frag in model_lower for frag in _BEDROCK_CACHE_SUPPORTED_FRAGMENTS)
