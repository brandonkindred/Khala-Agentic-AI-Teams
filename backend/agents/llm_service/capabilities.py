"""Provider-keyed capability flags for integration paths with no ``LLMClient`` instance.

Prefer :meth:`LLMClient.supports_structured_output` when a real client/model
object exists. This module exists for Strategy Lab's Bedrock-via-strands
integration path (``investment_team/strategy_lab/agents/model_factory.py``),
which constructs a raw ``strands.models.BedrockModel`` directly and never
goes through ``llm_service`` — so there is no ``LLMClient`` instance to ask.
"""

from __future__ import annotations

_STRUCTURED_OUTPUT_CAPABLE_PROVIDERS = frozenset({"ollama"})


def provider_supports_structured_output(provider: str) -> bool:
    """Whether ``provider`` can be asked for provider-enforced schema-conformant JSON.

    Preconditions: ``provider`` is a lowercase provider identifier as resolved
        by ``llm_service.config.resolve_provider`` (e.g. ``"ollama"``,
        ``"bedrock"``, ``"claude"``, ``"dummy"``).
    Postconditions:
        - Returns True only for ``"ollama"`` — mirrors
          ``OllamaLLMClient.supports_structured_output``.
        - Returns False for ``"bedrock"``: NOT a claim that AWS Bedrock's
          Converse API is incapable of constrained decoding in general (it
          could in principle be driven via tool-choice) — only that Strategy
          Lab's ``model_factory.py`` constructs a raw
          ``strands.models.BedrockModel`` directly, bypassing ``llm_service``
          and never forwarding a ``tool_choice``, so it is False on THIS
          integration path.
        - Returns False for any other/unrecognized provider string (e.g.
          ``"claude"`` is instruction-based JSON only; ``"dummy"`` has no
          wire). Never raises.
    """
    return provider in _STRUCTURED_OUTPUT_CAPABLE_PROVIDERS


# ---------------------------------------------------------------------------
# Bedrock prompt-caching model support
# ---------------------------------------------------------------------------

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

    This is the single source of truth for raw-Strands-Bedrock integration
    paths (architect agents, Strategy Lab) that construct a
    ``strands.models.BedrockModel`` directly and have no ``LLMClient``
    instance to query.

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
