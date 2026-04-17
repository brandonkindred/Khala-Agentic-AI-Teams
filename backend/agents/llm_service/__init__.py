"""
Central LLM service for all agent teams.

Agents obtain a client via get_client(agent_key?) and use the LLMClient interface
(complete_json, complete, get_max_context_tokens). Provider (Ollama, Dummy, future OpenAI)
and config (env vars, known context, per-agent defaults) are centralized here.
"""

from typing import TYPE_CHECKING, Any

from . import config as _config
from .api import generate_structured, generate_text
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
    LLMSchemaValidationError,
    LLMTemporaryError,
    LLMTruncatedError,
    LLMUnreachableAfterRetriesError,
)
from .strands_provider import _clear_strands_model_cache_for_testing, get_strands_model
from .structured import complete_validated
from .telemetry import get_recent_calls, get_usage_summary, record_llm_call
from .tool_loop import complete_json_with_tool_loop
from .util import call_llm_with_retries, extract_json_from_response

# ``strands_adapter`` depends on the optional ``strands-agents`` package. Many
# teams in this monorepo ship with a narrower requirements file that does NOT
# include strands, and importing the adapter module eagerly would force every
# team to install it just to use ``llm_service``. Resolve ``LLMClientModel``
# and ``get_strands_model`` lazily via PEP 562 ``__getattr__`` so they only
# trigger the strands import when a consumer actually asks for them.
if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .strands_adapter import (  # noqa: F401
        LLMClientModel,
        get_strands_model,
        run_json_via_strands,
    )

_LAZY_STRANDS_EXPORTS = {"LLMClientModel", "get_strands_model", "run_json_via_strands"}


def __getattr__(name: str) -> Any:
    if name in _LAZY_STRANDS_EXPORTS:
        from . import strands_adapter  # noqa: PLC0415 - intentional lazy import

        value = getattr(strands_adapter, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_llm_config_summary() -> str:
    """Return a short summary of current LLM config (provider, model, etc.) for logging."""
    return _config.get_llm_config_summary()


__all__ = [
    "_clear_client_cache_for_testing",
    "_clear_strands_model_cache_for_testing",
    "complete_json_with_tool_loop",
    "complete_validated",
    "call_llm_with_retries",
    "compact_text",
    "extract_json_from_response",
    "generate_structured",
    "generate_text",
    "get_client",
    "get_strands_model",
    "get_llm_config_summary",
    "get_strands_model",
    "LLMClient",
    "LLMClientModel",
    "LLMError",
    "LLMRateLimitError",
    "LLMTemporaryError",
    "LLMUnreachableAfterRetriesError",
    "LLMPermanentError",
    "LLMJsonParseError",
    "LLMSchemaValidationError",
    "LLMTruncatedError",
    "OLLAMA_WEEKLY_LIMIT_MESSAGE",
    "OllamaLLMClient",
    "DummyLLMClient",
    "record_llm_call",
    "get_recent_calls",
    "get_usage_summary",
]
