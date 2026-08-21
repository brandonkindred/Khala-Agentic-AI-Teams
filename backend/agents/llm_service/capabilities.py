"""Provider-keyed capability flags for integration paths with no ``LLMClient`` instance.

Prefer :meth:`LLMClient.supports_structured_output` when a real client/model
object exists. This module exists for Strategy Lab's Bedrock-via-strands
integration path (``investment_team/strategy_lab/agents/model_factory.py``),
which constructs a raw ``strands.models.BedrockModel`` directly and never
goes through ``llm_service`` — so there is no ``LLMClient`` instance to ask.
"""

from __future__ import annotations

_STRUCTURED_OUTPUT_CAPABLE_PROVIDERS = frozenset({"ollama", "runpod"})


def provider_supports_structured_output(provider: str) -> bool:
    """Whether ``provider`` can be asked for provider-enforced schema-conformant JSON.

    Preconditions: ``provider`` is a lowercase provider identifier as resolved
        by ``llm_service.config.resolve_provider`` (e.g. ``"ollama"``,
        ``"bedrock"``, ``"claude"``, ``"dummy"``).
    Postconditions:
        - Returns True for ``"ollama"`` and ``"runpod"`` — mirrors
          ``OllamaLLMClient.supports_structured_output`` and
          ``RunPodLLMClient.supports_structured_output``.
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
# Canonical implementation lives in shared.llm_capabilities (zero-dependency,
# importable by standalone packages without llm_service).  Re-exported here
# for callers that already import from llm_service.capabilities.
from shared.llm_capabilities import bedrock_model_supports_prompt_caching  # noqa: F401,E402
