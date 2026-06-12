"""
Factory for obtaining an LLM client by agent key or default.

Resolves provider and model from config (env + per-agent overrides + default table).
Caches Ollama clients by (model, base_url, timeout). Thread-safe.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, Union

from . import config as llm_config
from .attribution import llm_attribution
from .clients import DummyLLMClient, OllamaLLMClient

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, str, float], OllamaLLMClient] = {}
_cache_lock = threading.Lock()


class _AttributingClient:
    """Thin wrapper that binds ``agent_key`` onto the attribution context.

    Wraps the four generation entry points so that every call made through a
    client obtained via :func:`get_client` is automatically attributed to the
    ``agent_key`` that was requested — without each call site threading the
    agent identity through by hand. All other attribute access (``.model``,
    ``get_max_context_tokens``, private helpers, the Strands adapter surface)
    delegates transparently to the wrapped client via ``__getattr__``.

    ``agent_key`` is bound with ``None``-inherit semantics for the other
    attribution fields, so an enclosing ``llm_attribution(team=..., objective=...)``
    block set by an orchestrator is never clobbered.

    Invariants:
        - The wrapped client (``_inner``) is the shared, cached singleton; the
          wrapper holds no other mutable state and is cheap to construct.
    """

    def __init__(self, inner: Union[DummyLLMClient, OllamaLLMClient], agent_key: str) -> None:
        self._inner = inner
        self._agent_key = agent_key

    def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        with llm_attribution(agent_key=self._agent_key):
            return self._inner.complete_json(*args, **kwargs)

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        with llm_attribution(agent_key=self._agent_key):
            return self._inner.complete(*args, **kwargs)

    def complete_text(self, *args: Any, **kwargs: Any) -> Any:
        with llm_attribution(agent_key=self._agent_key):
            return self._inner.complete_text(*args, **kwargs)

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        with llm_attribution(agent_key=self._agent_key):
            return self._inner.chat(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on the wrapper itself.
        return getattr(self._inner, name)


def unwrap_client(client: Any) -> Any:
    """Return the underlying provider client, unwrapping the attribution wrapper.

    Use this when code needs the concrete client type — e.g. an
    ``isinstance(c, OllamaLLMClient)`` check or reading provider-specific
    attributes for reconstruction — since :func:`get_client` returns an
    :class:`_AttributingClient` for keyed clients.

    Postconditions: returns ``client._inner`` when ``client`` is an
        :class:`_AttributingClient`, otherwise returns ``client`` unchanged.
    """
    return client._inner if isinstance(client, _AttributingClient) else client


def get_client(
    agent_key: Optional[str] = None,
    *,
    on_reasoning: Optional[Callable[[str], None]] = None,
) -> Union[DummyLLMClient, OllamaLLMClient, "_AttributingClient"]:
    """
    Return an LLM client for the given agent key or default.

    Model resolution: LLM_MODEL_<agent_key>, then LLM_MODEL, then AGENT_DEFAULT_MODELS[agent_key], then fallback.
    When LLM_PROVIDER=dummy, returns DummyLLMClient. Otherwise returns OllamaLLMClient (cached by model, base_url, timeout).

    ``on_reasoning`` is an optional per-caller thinking-token sink. When provided,
    a FRESH (uncached) OllamaLLMClient is returned so the callback never leaks into
    the shared cache; the cached singleton path is used only when it is ``None``.
    The dummy provider produces no reasoning, so the hook is irrelevant there.

    When ``agent_key`` is provided, the returned object is an
    :class:`_AttributingClient` wrapper that binds that agent identity onto the
    attribution context around every generation call (so logs/telemetry attribute
    the call to the agent); the underlying client it delegates to is still the
    cached singleton. When ``agent_key`` is ``None`` the cached client is returned
    directly. The dummy provider is always returned unwrapped.

    Preconditions: ``on_reasoning`` is callable or ``None``.
    Postconditions: with ``on_reasoning is None`` the wrapped/underlying client is
        the cached singleton for (model, base_url, timeout); otherwise it is a
        distinct, uncached client carrying the hook.
    """
    provider = llm_config.resolve_provider()
    if provider == "dummy":
        # The dummy stub is returned unwrapped: it doubles as a Strands ``Model``
        # (passed directly to ``strands.Agent``) and records no telemetry, so
        # there is no attribution to bind.
        return DummyLLMClient()

    model = llm_config.resolve_model(agent_key)
    base_url = llm_config.resolve_base_url()
    timeout = llm_config.resolve_timeout(agent_key)

    if on_reasoning is not None:
        # Uncached: a per-job/per-caller callback must not be shared via the cache.
        client = OllamaLLMClient(
            model=model, base_url=base_url, timeout=timeout, on_reasoning=on_reasoning
        )
        return _AttributingClient(client, agent_key) if agent_key else client

    cache_key = (model, base_url, timeout)
    with _cache_lock:
        if cache_key not in _client_cache:
            _client_cache[cache_key] = OllamaLLMClient(
                model=model, base_url=base_url, timeout=timeout
            )
        client = _client_cache[cache_key]

    if agent_key is None:
        logger.info("LLM config: %s", llm_config.get_llm_config_summary())
        return client
    return _AttributingClient(client, agent_key)


def _clear_client_cache_for_testing() -> None:
    """Clear the Ollama client cache. For use in tests only."""
    with _cache_lock:
        _client_cache.clear()
