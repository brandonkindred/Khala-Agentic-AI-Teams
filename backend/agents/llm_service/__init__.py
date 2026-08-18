"""
Central LLM service for all agent teams.

Agents obtain a client via get_client(agent_key?) and use the LLMClient interface
(complete_json, complete, get_max_context_tokens). Provider (Ollama, Dummy, future OpenAI)
and config (env vars, known context, per-agent defaults) are centralized here.
"""

from typing import TYPE_CHECKING, Any

from . import config as _config
from .api import generate_structured, generate_text
from .attribution import (
    LLMAttribution,
    bind_request_id,
    current_attribution,
    current_request_id,
    llm_attribution,
    new_request_id,
)
from .backoff import parse_rate_limit_retry_config, rate_limit_retry_delay
from .cache_breakpoint import CacheBreakpoint
from .capabilities import provider_supports_structured_output
from .clients import ClaudeLLMClient, DummyLLMClient, OllamaLLMClient, RunPodLLMClient
from .compaction import clear_compaction_cache, compact_text, supports_compaction
from .factory import (
    attributed_client,
    clear_client_cache,
    client_agent_key,
    get_client,
    unwrap_client,
    with_model_override,
)
from .interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMClient,
    LLMError,
    LLMJsonParseError,
    LLMNotConfiguredError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaValidationError,
    LLMSemanticExhaustionError,
    LLMTemporaryError,
    LLMTruncatedError,
    LLMUnreachableAfterRetriesError,
)
from .limit_classification import (
    LIMIT_KIND_RATE,
    LIMIT_KIND_SESSION,
    LIMIT_KIND_WEEKLY,
    classify_ollama_limit_kind,
)
from .pricing import estimate_cost_usd
from .structured import (
    complete_json_via_reasoning,
    complete_validated,
    complete_validated_via_reasoning,
)
from .telemetry import (
    get_recent_calls,
    get_usage_summary,
    record_llm_call,
    register_call_observer,
    unregister_call_observer,
)
from .tool_loop import complete_json_with_tool_loop
from .util import (
    call_llm_with_retries,
    call_llm_with_retries_async,
    extract_json_from_response,
)

# ``strands_adapter`` / ``strands_provider`` depend on the optional
# ``strands-agents`` package and import ``strands`` at module scope. Resolve
# their public names lazily via PEP 562 ``__getattr__`` so ``import llm_service``
# (and ``import llm_service.clients.dummy``) does not pull Strands/botocore into
# ``sys.modules`` until a Strands code path is exercised.
if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .strands_adapter import (  # noqa: F401
        LLMClientModel,
        run_json_via_strands,
    )
    from .strands_provider import (  # noqa: F401
        _clear_strands_model_cache_for_testing,
        get_strands_model,
    )

_LAZY_STRANDS_ADAPTER_EXPORTS = {"LLMClientModel", "run_json_via_strands"}
_LAZY_STRANDS_PROVIDER_EXPORTS = {"get_strands_model", "_clear_strands_model_cache_for_testing"}


def __getattr__(name: str) -> Any:
    if name in _LAZY_STRANDS_ADAPTER_EXPORTS:
        from . import strands_adapter  # noqa: PLC0415 - intentional lazy import

        value = getattr(strands_adapter, name)
        globals()[name] = value
        return value
    if name in _LAZY_STRANDS_PROVIDER_EXPORTS:
        from . import strands_provider  # noqa: PLC0415 - intentional lazy import

        value = getattr(strands_provider, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_llm_config_summary() -> str:
    """Return a short summary of current LLM config (provider, model, etc.) for logging."""
    return _config.get_llm_config_summary()


__all__ = [
    "_clear_strands_model_cache_for_testing",
    "clear_client_cache",
    "CacheBreakpoint",
    "LLMAttribution",
    "llm_attribution",
    "current_attribution",
    "current_request_id",
    "new_request_id",
    "bind_request_id",
    "complete_json_with_tool_loop",
    "complete_validated",
    "complete_json_via_reasoning",
    "complete_validated_via_reasoning",
    "call_llm_with_retries",
    "call_llm_with_retries_async",
    "compact_text",
    "clear_compaction_cache",
    "supports_compaction",
    "extract_json_from_response",
    "generate_structured",
    "generate_text",
    "parse_rate_limit_retry_config",
    "rate_limit_retry_delay",
    "get_client",
    "unwrap_client",
    "client_agent_key",
    "attributed_client",
    "with_model_override",
    "get_strands_model",
    "get_llm_config_summary",
    "LLMClient",
    "LLMClientModel",
    "LLMError",
    "LLMRateLimitError",
    "LLMSemanticExhaustionError",
    "LLMTemporaryError",
    "LLMUnreachableAfterRetriesError",
    "LLMPermanentError",
    "LLMNotConfiguredError",
    "LLMJsonParseError",
    "LLMSchemaValidationError",
    "LLMTruncatedError",
    "OLLAMA_WEEKLY_LIMIT_MESSAGE",
    "LIMIT_KIND_RATE",
    "LIMIT_KIND_SESSION",
    "LIMIT_KIND_WEEKLY",
    "classify_ollama_limit_kind",
    "OllamaLLMClient",
    "ClaudeLLMClient",
    "DummyLLMClient",
    "RunPodLLMClient",
    "record_llm_call",
    "register_call_observer",
    "unregister_call_observer",
    "estimate_cost_usd",
    "get_recent_calls",
    "get_usage_summary",
    "provider_supports_structured_output",
]
