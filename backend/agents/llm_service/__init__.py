"""
Central LLM service for all agent teams.

Agents obtain a client via get_client(agent_key?) and use the LLMClient interface
(complete_json, complete, get_max_context_tokens). Provider (Ollama, Dummy, future OpenAI)
and config (env vars, known context, per-agent defaults) are centralized here.
"""

from . import config as _config
from .clients import DummyLLMClient, OllamaLLMClient
from .compaction import compact_text
from .factory import _clear_client_cache_for_testing, get_client
from .interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMClient,
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
    LLMUnreachableAfterRetriesError,
)
from .util import call_llm_with_retries, extract_json_from_response


def get_llm_config_summary() -> str:
    """Return a short summary of current LLM config (provider, model, etc.) for logging."""
    return _config.get_llm_config_summary()


__all__ = [
    "_clear_client_cache_for_testing",
    "call_llm_with_retries",
    "compact_text",
    "extract_json_from_response",
    "get_client",
    "get_llm_config_summary",
    "LLMClient",
    "LLMError",
    "LLMRateLimitError",
    "LLMTemporaryError",
    "LLMUnreachableAfterRetriesError",
    "LLMPermanentError",
    "LLMJsonParseError",
    "LLMTruncatedError",
    "OLLAMA_WEEKLY_LIMIT_MESSAGE",
    "OllamaLLMClient",
    "DummyLLMClient",
]
