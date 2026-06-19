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
from .clients import ClaudeLLMClient, DummyLLMClient, OllamaLLMClient
from .interface import LLMClient
from .util import sha256_fingerprint

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, str, float], OllamaLLMClient] = {}
# Claude clients cache by (model, api-key fingerprint) so a key or model change
# yields a fresh client (and a stale key never lingers behind a cached client).
_claude_cache: dict[tuple[str, str, float], ClaudeLLMClient] = {}
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

    def __init__(
        self,
        inner: Union[DummyLLMClient, OllamaLLMClient, ClaudeLLMClient],
        agent_key: str,
    ) -> None:
        self._inner = inner
        self._agent_key = agent_key

    def __repr__(self) -> str:
        return f"_AttributingClient(agent_key={self._agent_key!r}, inner={self._inner!r})"

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


# Register as a virtual LLMClient so ``isinstance(wrapper, LLMClient)`` holds for
# resolvers that branch on the interface (e.g. the SE team's
# ``resolve_strands_model``). Virtual registration affects only isinstance/
# issubclass — it does NOT add LLMClient's concrete default methods to the MRO,
# so the transparent ``__getattr__`` delegation is preserved.
LLMClient.register(_AttributingClient)


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


def client_agent_key(client: Any) -> Optional[str]:
    """Return the ``agent_key`` bound to a wrapped client, else ``None``.

    Postconditions: returns the agent identity for an :class:`_AttributingClient`,
        otherwise ``None`` (an unwrapped client carries no bound identity).
    """
    return client._agent_key if isinstance(client, _AttributingClient) else None


def attributed_client(inner: Any, agent_key: Optional[str]) -> Any:
    """Wrap ``inner`` so its calls attribute to ``agent_key``, mirroring ``get_client``.

    Use this when code constructs a replacement provider client (e.g. a
    per-model override) but must preserve the agent attribution of the client it
    is replacing. Pairs with :func:`client_agent_key`.

    If ``inner`` is already an :class:`_AttributingClient`, it is unwrapped
    before re-wrapping so the new ``agent_key`` is the sole binding and the
    old key is not shadowed by a stacked wrapper.

    Postconditions: returns an :class:`_AttributingClient` over the unwrapped
        ``inner`` when ``agent_key`` is truthy; otherwise returns ``inner`` unchanged.
    """
    if not agent_key:
        return inner
    base = unwrap_client(inner)
    return _AttributingClient(base, agent_key)


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
    a FRESH (uncached) provider client (Ollama or Claude) is returned so the
    callback never leaks into the shared cache; the cached singleton path is used
    only when it is ``None``. The dummy provider produces no reasoning, so the hook
    is irrelevant there.

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

    if provider in ("claude", "anthropic"):
        return _get_claude_client(agent_key, on_reasoning=on_reasoning)

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
    # Falsy agent_key (None or "") binds nothing — return the raw client, matching
    # the on_reasoning branch above. Wrapping with an empty key would bind
    # ``llm_attribution(agent_key="")``, which (since "" overrides rather than
    # inherits) would clobber an enclosing orchestrator's agent_key with ``-``.
    return _AttributingClient(client, agent_key) if agent_key else client


def _get_claude_client(
    agent_key: Optional[str],
    *,
    on_reasoning: Optional[Callable[[str], None]] = None,
) -> Union[ClaudeLLMClient, "_AttributingClient"]:
    """Build/cache a :class:`ClaudeLLMClient` for ``agent_key``.

    Cached by ``(model, api_key_fingerprint, timeout)`` so a model, key, or timeout
    change yields a fresh client (the timeout dimension mirrors the Ollama cache
    key and avoids a stale-timeout client if ``resolve_timeout`` ever becomes
    per-agent). Wrapped in :class:`_AttributingClient` when ``agent_key`` is
    truthy, mirroring the Ollama path.

    ``on_reasoning`` (a per-caller thinking-token sink) forces a FRESH, uncached
    client so the callback never leaks into the shared cache — mirroring the Ollama
    path; the cached singleton is used only when it is ``None``.

    Postconditions: returns a ready client; with ``on_reasoning is None`` the
        underlying ``ClaudeLLMClient`` is the cached singleton for its
        (model, key, timeout) tuple, otherwise a distinct uncached client.
    """
    model = llm_config.resolve_claude_model(agent_key)
    api_key = llm_config.resolve_claude_api_key()
    timeout = llm_config.resolve_timeout(agent_key)
    if on_reasoning is not None:
        # Uncached: a per-job/per-caller callback must not be shared via the cache.
        client = ClaudeLLMClient(
            model=model, api_key=api_key, timeout=timeout, on_reasoning=on_reasoning
        )
        return _AttributingClient(client, agent_key) if agent_key else client
    fingerprint = sha256_fingerprint(api_key) if api_key else "no-key"
    cache_key = (model, fingerprint, timeout)
    with _cache_lock:
        client = _claude_cache.get(cache_key)
        if client is None:
            client = ClaudeLLMClient(model=model, api_key=api_key, timeout=timeout)
            _claude_cache[cache_key] = client
    if agent_key is None:
        logger.info("LLM config: %s", llm_config.get_llm_config_summary())
    return _AttributingClient(client, agent_key) if agent_key else client


def clear_client_cache() -> None:
    """Drop all cached provider clients (Ollama + Claude) and Strands adapters.

    Called by the settings endpoint after a config change so the next
    :func:`get_client` / :func:`get_strands_model` rebuilds against the new
    provider/model/key. The Strands model cache is cleared too because its entries
    pin a backing provider client built with the previous key (its cache key omits
    the key fingerprint, so an in-place API-key rotation would otherwise be served
    stale). Safe to call when nothing is cached.

    Postconditions: the factory and Strands model caches are empty afterward.
    """
    with _cache_lock:
        _client_cache.clear()
        _claude_cache.clear()
    # Lazy import to avoid a circular import (strands_provider imports get_client).
    try:
        from . import strands_provider

        strands_provider.clear_model_cache()
    except Exception:  # noqa: BLE001 - cache clear is best-effort
        # Warn (not debug): a swallowed failure here silently leaves a stale
        # key-pinned Strands adapter after a config change — the bug this clears.
        logger.warning("Failed to clear Strands model cache", exc_info=True)


def _clear_client_cache_for_testing() -> None:
    """Clear the provider client caches. For use in tests only."""
    clear_client_cache()
