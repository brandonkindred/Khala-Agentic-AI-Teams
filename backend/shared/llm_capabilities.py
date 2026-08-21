"""Lightweight, dependency-free model-capability helpers.

This module lives in ``backend/shared/`` so it is importable by any backend
component with ``backend/`` on ``sys.path`` — including standalone packages
(architect-agents, Strategy Lab) that do NOT have ``llm_service`` or its
heavy transitive dependencies (pydantic, etc.) available.

The canonical re-export for callers that DO have ``llm_service`` available is
:func:`llm_service.capabilities.bedrock_model_supports_prompt_caching`, which
delegates here.
"""
from __future__ import annotations

__all__ = ["bedrock_model_supports_prompt_caching"]

# -------------------------------------------------------------------------
# Bedrock prompt-caching model support
# -------------------------------------------------------------------------

#: Model-id substrings that identify Bedrock models with prompt-caching
#: support (``cachePoint`` in system content).  Per the AWS documentation
#: (https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html),
#: supported models include Claude 3.5 Sonnet **v2**, Claude 3.7 Sonnet,
#: Claude Sonnet 4+, Claude Haiku 4.5+, Claude Opus 4.5+, and Amazon Nova.
#: Older Claude 3 originals and Claude 3.5 Sonnet v1 are NOT supported.
_BEDROCK_CACHE_SUPPORTED_FRAGMENTS: tuple[str, ...] = (
    # Claude 4.x+ family (Sonnet 4, Opus 4, Haiku 4.5, etc.)
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    # Claude 3.7 Sonnet
    "claude-3-7-sonnet",
    # Claude 3.5 Sonnet v2 only (v1 20240620 does NOT support caching)
    "claude-3-5-sonnet-20241022-v2",
    # Claude 3.5 Haiku
    "claude-3-5-haiku",
    # Amazon Nova models
    "amazon.nova",
)


def bedrock_model_supports_prompt_caching(model_id: str) -> bool:
    """Whether a Bedrock model-id supports the ``cachePoint`` system-content block.

    This is the single source of truth for all integration paths —
    both those that go through ``llm_service`` and standalone packages
    (architect-agents, Strategy Lab) that construct a raw
    ``strands.models.BedrockModel`` directly.

    Preconditions:
        ``model_id`` is a Bedrock model identifier string (e.g.
        ``"anthropic.claude-sonnet-4-20250514-v1:0"``).  Empty string is
        treated as "unknown model — assume caching is supported" to allow
        the default (Anthropic) models to benefit without requiring every
        call site to pass the resolved model id.

    Postconditions:
        - Returns ``True`` when ``model_id`` is empty OR contains a
          substring from the supported-model allowlist.
        - Returns ``False`` for model ids that do not match any known
          caching-capable fragment (e.g. older Claude 3 originals,
          non-Anthropic models like Llama/Mistral).
        - Never raises.
    """
    if not model_id:
        return True
    model_lower = model_id.lower()
    return any(frag in model_lower for frag in _BEDROCK_CACHE_SUPPORTED_FRAGMENTS)
