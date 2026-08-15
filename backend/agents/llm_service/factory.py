"""LLM client factory: provider selection, client caching, and agent attribution.

Resolves the active provider and model from :mod:`llm_service.config` (runtime UI ->
env -> per-agent/default tables) and hands back the matching provider client:

- **Provider selection** — ``dummy`` -> :class:`DummyLLMClient`; ``claude`` ->
  :class:`ClaudeLLMClient`; ``ollama`` (or unset) -> :class:`OllamaLLMClient`.
- **Caching** — Ollama clients are cached by ``(model, base_url, timeout-ms)`` and
  Claude clients by ``(model, api-key-fingerprint, timeout-ms)`` (the timeout is
  quantized to integer milliseconds so float jitter never fragments the cache), so
  a model/key/base-url change yields a fresh client and a stale key never lingers.
- **on_reasoning fresh-client path** — a per-caller thinking-token sink forces a
  FRESH, uncached client so the callback never leaks into the shared cache.
- **Agent attribution** — when ``agent_key`` is given, the cached client is wrapped
  in :class:`_AttributingClient`, which binds that identity onto the attribution
  context around every generation call (logs/telemetry attribute to the agent).

Thread-safe: all cache reads/writes are guarded by ``_cache_lock``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Union

from . import config as llm_config
from . import provider_store
from .attribution import llm_attribution
from .clients import ClaudeLLMClient, DummyLLMClient, OllamaLLMClient, RunPodLLMClient
from .interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMClient,
    LLMNotConfiguredError,
    LLMRateLimitError,
)
from .limit_classification import (
    LIMIT_KIND_RATE,
    LIMIT_KIND_SESSION,
    LIMIT_KIND_WEEKLY,
    resolve_limit_kind,
)
from .util import sha256_fingerprint

logger = logging.getLogger(__name__)

# Shared message for the "no LLM configured" failure — the provider list is the sole
# source of LLM resolution (``dummy`` aside), so an empty list means no client to build.
_NO_PROVIDER_MSG = (
    "No LLM provider is configured. Add a provider in the LLM Provider settings "
    "(/llm-config) to run agents."
)

# Cache keys carry a 4th element — the rate-limit retry override (``-1`` for "use the
# env schedule", ``0`` for the failover fast-fail clients) — so a fast-fail failover
# client and a normal client for the same (model, base_url/key, timeout) never alias.
# The Ollama key also carries a 5th element (api-key fingerprint) so a per-provider
# Cloud key from the fallback list never aliases a client keyed differently.
_client_cache: dict[tuple[str, str, int, int, str], OllamaLLMClient] = {}
# Claude clients cache by (model, api-key fingerprint, timeout-ms, rl-override) so a
# key, model, timeout, or rate-limit-override change yields a fresh client (and a
# stale key never lingers behind a cached client).
_claude_cache: dict[tuple[str, str, int, int], ClaudeLLMClient] = {}
# RunPod clients cache by (model, base_url, timeout-ms, rl-override, api-key fingerprint)
# mirroring the Ollama cache layout; api_key is mandatory for RunPod (no empty-key path).
_runpod_cache: dict[tuple[str, str, int, int, str], RunPodLLMClient] = {}
_cache_lock = threading.Lock()


def _rl_key(rate_limit_max_retries: Optional[int]) -> int:
    """Map a rate-limit override to a hashable cache-key component.

    Preconditions: ``rate_limit_max_retries`` is an int or ``None``.
    Postconditions: returns the override unchanged when set, else ``-1`` (the
        "use the env schedule" sentinel — distinct from a real ``0`` override).
    """
    return rate_limit_max_retries if rate_limit_max_retries is not None else -1


def _ollama_cached(
    model: str,
    base_url: str,
    timeout: float,
    rate_limit_max_retries: Optional[int],
    api_key: str = "",
) -> "tuple[OllamaLLMClient, bool]":
    """Return a cached Ollama client for the args, building one on a miss.

    The cache key includes an api-key fingerprint (mirroring the Claude cache) so a
    per-provider Cloud key from the fallback list yields a distinct client and never
    aliases one keyed differently; an empty key (the single-provider path) fingerprints
    to ``"no-key"`` and falls back to the globally-resolved key at request time.

    Preconditions: ``model``/``base_url`` are resolved strings; ``timeout`` is a
        number; ``api_key`` may be ``""`` (no Authorization header).
    Postconditions: returns ``(client, was_miss)`` where ``client`` is the shared
        singleton for ``(model, base_url, timeout, rl-override, key-fingerprint)`` and
        ``was_miss`` is True only when this call constructed it (so the caller can log
        the effective config exactly once, on a genuine miss). Thread-safe.
    """
    fingerprint = sha256_fingerprint(api_key) if api_key else "no-key"
    cache_key = (
        model,
        base_url,
        _timeout_cache_key(timeout),
        _rl_key(rate_limit_max_retries),
        fingerprint,
    )
    with _cache_lock:
        client = _client_cache.get(cache_key)
        miss = client is None
        if miss:
            client = OllamaLLMClient(
                model=model,
                base_url=base_url,
                timeout=timeout,
                rate_limit_max_retries=rate_limit_max_retries,
                api_key=api_key,
            )
            _client_cache[cache_key] = client
    return client, miss


def _runpod_cached(
    model: str,
    base_url: str,
    timeout: float,
    rate_limit_max_retries: Optional[int],
    api_key: str,
) -> "tuple[RunPodLLMClient, bool]":
    """Return a cached RunPod client for the args, building one on a miss.

    Cache key: ``(model, base_url, timeout_ms, rl_key, key_fingerprint)``.
    RunPod always requires an API key, so ``api_key`` is mandatory (no default).

    Preconditions: ``model``/``base_url`` are resolved strings; ``timeout`` is a
        number; ``api_key`` is non-empty.
    Postconditions: returns ``(client, was_miss)`` where ``client`` is the shared
        singleton for the given args and ``was_miss`` is True only when this call
        constructed it. Thread-safe.
    """
    fingerprint = sha256_fingerprint(api_key)
    cache_key = (
        model,
        base_url,
        _timeout_cache_key(timeout),
        _rl_key(rate_limit_max_retries),
        fingerprint,
    )
    with _cache_lock:
        client = _runpod_cache.get(cache_key)
        miss = client is None
        if miss:
            client = RunPodLLMClient(
                model=model,
                base_url=base_url,
                timeout=timeout,
                rate_limit_max_retries=rate_limit_max_retries,
                api_key=api_key,
            )
            _runpod_cache[cache_key] = client
    return client, miss


def _claude_cached(
    model: str, api_key: str, timeout: float, rate_limit_max_retries: Optional[int]
) -> "tuple[ClaudeLLMClient, bool]":
    """Return a cached Claude client for the args, building one on a miss.

    Cached by ``(model, api-key fingerprint, timeout-ms, rl-override)`` so a key,
    model, timeout, or rate-limit-override change yields a fresh client.

    Preconditions: ``model``/``api_key`` are resolved strings (``api_key`` may be
        ``""``); ``timeout`` is a number.
    Postconditions: returns ``(client, was_miss)`` (see :func:`_ollama_cached`).
        Thread-safe.
    """
    fingerprint = sha256_fingerprint(api_key) if api_key else "no-key"
    cache_key = (model, fingerprint, _timeout_cache_key(timeout), _rl_key(rate_limit_max_retries))
    with _cache_lock:
        client = _claude_cache.get(cache_key)
        miss = client is None
        if miss:
            client = ClaudeLLMClient(
                model=model,
                api_key=api_key,
                timeout=timeout,
                rate_limit_max_retries=rate_limit_max_retries,
            )
            _claude_cache[cache_key] = client
    return client, miss


def _timeout_cache_key(timeout: float) -> int:
    """Quantize a float ``timeout`` (seconds) to integer milliseconds for the cache key.

    A float timeout would let imperceptible jitter (e.g. 900.0 vs 900.0000001)
    fragment the client cache into near-duplicate entries; rounding to whole
    milliseconds gives a stable, hashable key while preserving any meaningful
    per-agent timeout difference.

    Preconditions: ``timeout`` is a number.
    Postconditions: returns ``round(timeout * 1000)`` as an ``int``. Never raises
        for a numeric input.
    """
    return int(round(timeout * 1000))


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
        inner: Union[DummyLLMClient, OllamaLLMClient, ClaudeLLMClient, "FailoverLLMClient"],
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

    It peels ONLY the attribution wrapper. When multi-provider failover is active,
    the underlying client is a :class:`FailoverLLMClient`, and this returns *that*
    (not the concrete provider client behind it) so the Strands adapter — which
    dispatches via ``unwrap_client(self._client).chat`` — still routes through
    failover. A :class:`FailoverLLMClient` duck-types as its active provider client
    (``.model``, ``get_max_context_tokens``, etc. delegate through ``__getattr__``),
    so most consumers are unaffected; an ``isinstance(inner, OllamaLLMClient)`` gate
    simply sees the failover wrapper and falls to its default branch.

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


# ---------------------------------------------------------------------------
# Multi-provider failover
# ---------------------------------------------------------------------------


def _build_claude_concrete(
    model: str,
    api_key: str,
    timeout: float,
    on_reasoning: Optional[Callable[[str], None]],
    rate_limit_max_retries: Optional[int],
) -> ClaudeLLMClient:
    """Build an unwrapped Claude client — cached unless ``on_reasoning`` is set.

    Shared by the entry (fallback-list) and legacy concrete builders so the "fresh
    client when a per-caller hook is present, else the shared cached singleton" rule
    lives in exactly one place.

    Preconditions: ``model``/``api_key`` are resolved strings; ``timeout`` is a
        number; ``on_reasoning`` is callable or ``None``.
    Postconditions: returns a ready client whose 429 backoff budget is
        ``rate_limit_max_retries``; goes through the shared cache only when
        ``on_reasoning is None`` (a per-caller hook must never be shared).
    """
    if on_reasoning is not None:
        return ClaudeLLMClient(
            model=model,
            api_key=api_key,
            timeout=timeout,
            on_reasoning=on_reasoning,
            rate_limit_max_retries=rate_limit_max_retries,
        )
    client, _ = _claude_cached(model, api_key, timeout, rate_limit_max_retries)
    return client


def _build_ollama_concrete(
    model: str,
    base_url: str,
    timeout: float,
    on_reasoning: Optional[Callable[[str], None]],
    rate_limit_max_retries: Optional[int],
    api_key: str = "",
) -> OllamaLLMClient:
    """Build an unwrapped Ollama client — cached unless ``on_reasoning`` is set.

    Shared by the entry (fallback-list) and legacy concrete builders (see
    :func:`_build_claude_concrete`).

    Preconditions: ``model``/``base_url`` are resolved strings; ``timeout`` is a
        number; ``api_key`` may be ``""`` (no Authorization header); ``on_reasoning``
        is callable or ``None``.
    Postconditions: returns a ready client whose 429 backoff budget is
        ``rate_limit_max_retries``; goes through the shared cache only when
        ``on_reasoning is None``.
    """
    if on_reasoning is not None:
        return OllamaLLMClient(
            model=model,
            base_url=base_url,
            timeout=timeout,
            on_reasoning=on_reasoning,
            rate_limit_max_retries=rate_limit_max_retries,
            api_key=api_key,
        )
    client, _ = _ollama_cached(model, base_url, timeout, rate_limit_max_retries, api_key)
    return client


def _build_entry_client(
    entry: "provider_store.ProviderEntry",
    agent_key: Optional[str],
    on_reasoning: Optional[Callable[[str], None]],
    rate_limit_max_retries: Optional[int],
    model_override: Optional[str] = None,
) -> Union[OllamaLLMClient, ClaudeLLMClient]:
    """Build the concrete provider client for one configured fallback entry.

    The entry is self-contained for credentials: its ``api_key`` authenticates the
    client with NO environment fallback (a Claude or Ollama-Cloud entry must carry
    its own key — enforced at write time by the route's credentials guard; a local
    Ollama entry resolves to ``""`` → no Authorization header). Empty non-secret
    fields (``model``/``base_url``) fall back to the provider defaults via the shared
    resolvers. Goes through the shared client caches except when ``on_reasoning`` is
    set (a per-caller hook must never be shared via the cache).

    ``model_override``, when set, pins the model for an **Ollama** entry only (a
    per-stage override such as the blog planning model names an Ollama model). A
    Claude entry ignores it and keeps its configured model, so a failover hop across
    provider types still resolves a model valid for that provider.

    Preconditions: ``entry.provider`` is ``"ollama"`` or ``"claude"``. Postconditions:
        returns a ready concrete client whose 429 backoff budget is
        ``rate_limit_max_retries`` (``0`` for fast-fail failover hops); an Ollama
        client uses ``model_override`` (when given) in place of its configured model.
        Never wraps in attribution — the failover client is wrapped once by the caller.
    """
    timeout = llm_config.resolve_timeout(agent_key)
    if entry.provider == "claude":
        model = entry.model.strip() or llm_config.resolve_claude_model(agent_key)
        return _build_claude_concrete(model, entry.api_key, timeout, on_reasoning, rate_limit_max_retries)
    model = (model_override or "").strip() or entry.model.strip() or llm_config.resolve_model(agent_key)
    base_url = entry.base_url.strip() or llm_config.resolve_base_url()
    # The entry carries its own key (empty → no Authorization header, i.e. a local
    # Ollama endpoint). No env fallback — a Cloud entry must store its own key.
    return _build_ollama_concrete(model, base_url, timeout, on_reasoning, rate_limit_max_retries, entry.api_key)


def _mark_entry_exhausted(entry: "provider_store.ProviderEntry", err: LLMRateLimitError) -> None:
    """Mark ``entry`` usage-limited from a 429, computing its ``reset_at``.

    Classification prefers ``err.limit_kind`` (set by the Ollama client from the
    429 body), then falls back to legacy message phrases
    (:data:`OLLAMA_WEEKLY_LIMIT_MESSAGE` / Ollama Cloud body text). Windows:

    - ``session`` — fixed :func:`llm_config.failover_session_window_seconds`
      (default 65m); **ignores** ``Retry-After``.
    - ``weekly`` — fixed :func:`llm_config.failover_weekly_window_seconds`
      (default 24h); **ignores** ``Retry-After``.
    - ``rate`` — honors ``Retry-After`` when present (including ``0`` = retry
      immediately); otherwise :func:`llm_config.failover_rate_window_seconds`
      (default 5m).

    ``reset_at`` (not ``limit_type``) is the load-bearing field for selection.

    Preconditions: ``err.retry_after_seconds`` is a non-negative number or ``None``.
    Postconditions: persists the mark via :func:`provider_store.mark_exhausted`
        (idempotent, swallows its own write errors). Never raises.
    """
    secs = err.retry_after_seconds
    message = str(err) or getattr(err, "message", "") or ""
    limit_type = resolve_limit_kind(
        limit_kind=getattr(err, "limit_kind", None),
        message=message,
        weekly_legacy_message=OLLAMA_WEEKLY_LIMIT_MESSAGE,
    )
    if limit_type == LIMIT_KIND_SESSION:
        window = llm_config.failover_session_window_seconds()
    elif limit_type == LIMIT_KIND_WEEKLY:
        window = llm_config.failover_weekly_window_seconds()
    else:
        limit_type = LIMIT_KIND_RATE
        has_retry_after = secs is not None and secs >= 0
        window = secs if has_retry_after else llm_config.failover_rate_window_seconds()
    reset_at = datetime.now(timezone.utc) + timedelta(seconds=float(window))
    provider_store.mark_exhausted(entry.id, limit_type=limit_type, reset_at=reset_at)


class FailoverLLMClient:
    """Ordered multi-provider client that fails over on a usage-limit (429).

    Each generation call re-loads the current ordered candidate list (the active
    provider first, then less-preferred ones), so a provider whose reset window has
    elapsed is reconsidered and a provider deleted/marked since construction is
    respected — the snapshot is per-CALL, never frozen at construction. Within one
    call it tries each candidate in order; a :class:`LLMRateLimitError` marks that
    provider exhausted (computing its ``reset_at``) and advances to the next. Any
    other error propagates unchanged (failover is strictly additive for 429). When
    every candidate is exhausted the last 429 is re-raised. An ``attempted`` set
    over the per-call snapshot guards against retrying the same provider twice.

    Earlier (non-last) candidates are built with a zero 429-retry budget (when
    ``LLM_FAILOVER_FAST_429`` is on, the default) so a hand-off isn't delayed by the
    slow in-place backoff; the LAST candidate keeps the configured backoff (nowhere
    left to fail over to), preserving single-provider behavior.

    ``.model``, ``get_max_context_tokens`` and the Strands surface delegate through
    ``__getattr__`` to the active provider client, so the wrapper duck-types as its
    current provider. Registered as a virtual ``LLMClient``.

    Invariants:
        - Holds no provider client itself — clients are pulled from the shared
          factory caches per call, so a config change is picked up within the TTL.
    """

    def __init__(
        self,
        load_candidates: "Callable[[], list[provider_store.ProviderEntry]]",
        build: "Callable[[provider_store.ProviderEntry, Optional[int], Optional[str]], Any]",
        mark: "Callable[[provider_store.ProviderEntry, LLMRateLimitError], None]",
        *,
        model_override: Optional[str] = None,
    ) -> None:
        self._load_candidates = load_candidates
        self._build = build
        self._mark = mark
        # Optional per-stage model override applied to Ollama candidates only (see
        # ``with_model_override``); ``None`` = each candidate uses its configured model.
        self._model_override = model_override

    def __repr__(self) -> str:
        return "FailoverLLMClient(multi-provider)"

    def with_model_override(self, model: Optional[str]) -> "FailoverLLMClient":
        """Return a shallow variant that pins Ollama candidates to ``model``.

        Reuses the same load/build/mark closures (so failover, attribution, and the
        per-call candidate snapshot are unchanged) — only the model applied to Ollama
        candidates differs. Used for per-stage overrides (e.g. the blog planning model)
        that must keep failing over across providers rather than collapsing to a single
        pinned client.

        Preconditions: ``model`` is a non-empty model name or ``None``. Postconditions:
            returns a new :class:`FailoverLLMClient`; ``self`` is unmodified.
        """
        return FailoverLLMClient(self._load_candidates, self._build, self._mark, model_override=model)

    def _dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        candidates = self._load_candidates()
        if not candidates:
            # The list was emptied at runtime — there is no legacy fallback, so the
            # provider list is the sole source. Fail the call explicitly.
            raise LLMNotConfiguredError(_NO_PROVIDER_MSG)
        fast = llm_config.failover_fast_429_enabled()
        last_index = len(candidates) - 1
        attempted: set[int] = set()
        last_error: Optional[LLMRateLimitError] = None
        for index, entry in enumerate(candidates):
            if entry.id in attempted:
                continue
            attempted.add(entry.id)
            # Non-last candidates fast-fail so the hand-off isn't delayed; the last
            # candidate keeps the configured (slow) backoff since there is no next.
            rl_override = 0 if (fast and index < last_index) else None
            client = self._build(entry, rl_override, self._model_override)
            try:
                return getattr(client, method)(*args, **kwargs)
            except LLMRateLimitError as e:
                self._mark(entry, e)
                last_error = e
                continue
        if last_error is not None:
            raise last_error
        # Unreachable for a non-empty candidate list (every entry either returns or
        # raises a 429), but fail loudly rather than returning None.
        raise LLMNotConfiguredError(_NO_PROVIDER_MSG)

    def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("complete_json", *args, **kwargs)

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("complete", *args, **kwargs)

    def complete_text(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("complete_text", *args, **kwargs)

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("chat", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on the wrapper itself: delegate to
        # the active provider client (``.model``, ``get_max_context_tokens``, the
        # Strands surface, ...) so the wrapper duck-types as its current provider.
        candidates = self._load_candidates()
        if not candidates:
            raise LLMNotConfiguredError(_NO_PROVIDER_MSG)
        return getattr(self._build(candidates[0], None, self._model_override), name)


# Virtual registration so ``isinstance(c, LLMClient)`` holds (mirrors
# ``_AttributingClient`` above) without adding LLMClient's concrete methods to the
# MRO — the transparent ``__getattr__`` delegation is preserved.
LLMClient.register(FailoverLLMClient)


def _build_failover_client(
    agent_key: Optional[str], *, on_reasoning: Optional[Callable[[str], None]]
) -> Optional[Any]:
    """Return a wrapped :class:`FailoverLLMClient` when a provider list is configured.

    Returns ``None`` when Postgres is disabled or the list is empty, so
    :func:`get_client` raises :class:`LLMNotConfiguredError` (the provider list is the
    sole source of LLM resolution). Otherwise returns the failover client wrapped in an
    :class:`_AttributingClient` when ``agent_key`` is truthy (attribution outermost,
    so a retried provider still attributes to the same agent).

    Preconditions: ``agent_key`` is a non-empty key or ``None``; ``on_reasoning`` is
        callable or ``None``.
    Postconditions: ``None`` -> caller raises ``LLMNotConfiguredError``; non-``None``
        is ready to use. Never raises (a store read failure yields ``None``).
    """
    try:
        entries = provider_store.load_ordered_entries()
    except Exception as e:  # noqa: BLE001 - a store read failure yields None -> caller raises
        logger.debug("provider list read failed: %s", e)
        return None
    if not entries:
        return None

    def load_candidates() -> "list[provider_store.ProviderEntry]":
        es = provider_store.load_ordered_entries()
        if not es:
            return []
        active = provider_store.select_active_entry(es)
        if active is None:
            return []
        start = next((i for i, e in enumerate(es) if e.id == active.id), 0)
        return es[start:]

    def build(
        entry: "provider_store.ProviderEntry",
        rl_override: Optional[int],
        model_override: Optional[str] = None,
    ) -> Any:
        return _build_entry_client(entry, agent_key, on_reasoning, rl_override, model_override)

    inner = FailoverLLMClient(load_candidates, build, _mark_entry_exhausted)
    return _AttributingClient(inner, agent_key) if agent_key else inner


def with_model_override(client: Any, model: Optional[str]) -> Any:
    """Return a variant of ``client`` that pins Ollama candidates to ``model``.

    For a multi-provider client (a :class:`FailoverLLMClient`, optionally wrapped for
    attribution), returns a variant whose Ollama fallback candidates use ``model``
    while non-Ollama candidates keep their configured model — so per-stage model
    overrides (e.g. the blog planning model) still fail over across providers instead
    of collapsing to one pinned client. Agent attribution is preserved. Any other
    client (e.g. :class:`DummyLLMClient`), or a falsy ``model``, returns ``client``
    unchanged.

    Preconditions: ``client`` is an LLM client from this module; ``model`` is a
        non-empty model name or falsy. Postconditions: returns a client ready to use;
        the input ``client`` is never mutated.
    """
    if not model:
        return client
    inner = unwrap_client(client)
    if isinstance(inner, FailoverLLMClient):
        return attributed_client(inner.with_model_override(model), client_agent_key(client))
    return client


def get_client(
    agent_key: Optional[str] = None,
    *,
    on_reasoning: Optional[Callable[[str], None]] = None,
) -> Union[DummyLLMClient, "_AttributingClient", "FailoverLLMClient"]:
    """
    Return an LLM client for the given agent key.

    The Postgres-backed ordered provider list (see :mod:`llm_service.provider_store`)
    is the SOLE source of LLM resolution. Resolution order:

    - ``LLM_PROVIDER=dummy`` -> :class:`DummyLLMClient` (a hard override that pre-empts
      the list, for tests / no-LLM dev — never touches Postgres).
    - Otherwise, when the list is non-empty, a :class:`FailoverLLMClient` (wrapped in
      :class:`_AttributingClient` for a keyed call) that selects the most-preferred
      non-exhausted provider and fails over to the next on a 429. Each entry is
      self-contained: it carries its own model/base URL/key (blank non-secret fields
      fall back to provider defaults; keys have no env fallback).
    - When the list is empty (or Postgres is unset), :class:`LLMNotConfiguredError` is
      raised — there is no legacy single-provider fallback. The operator configures a
      provider in the LLM Provider settings (``/llm-config``).

    ``on_reasoning`` is an optional per-caller thinking-token sink threaded into each
    per-entry client so the callback never leaks into the shared cache. When
    ``agent_key`` is provided, the returned object is an :class:`_AttributingClient`
    wrapper binding that agent identity onto the attribution context around every
    generation call. The dummy provider is always returned unwrapped.

    Preconditions: ``on_reasoning`` is callable or ``None``.
    Postconditions: returns a ready client, or raises :class:`LLMNotConfiguredError`
        when no provider is configured (and the provider is not ``dummy``).
    """
    if llm_config.resolve_provider() == "dummy":
        # The dummy stub is returned unwrapped: it doubles as a Strands ``Model``
        # (passed directly to ``strands.Agent``) and records no telemetry, so there is
        # no attribution to bind. Dummy is a hard override — it pre-empts the provider
        # list so no-LLM tests/dev never touch Postgres.
        return DummyLLMClient()

    # The provider list is the sole source: build a failover client from it, or raise
    # when it is empty/disabled (no legacy single-provider fallback).
    failover = _build_failover_client(agent_key, on_reasoning=on_reasoning)
    if failover is not None:
        return failover
    raise LLMNotConfiguredError(_NO_PROVIDER_MSG)


def clear_client_cache() -> None:
    """Drop all cached provider clients (Ollama + Claude) and Strands adapters.

    Called by the settings endpoint after a config change so the next
    :func:`get_client` / :func:`get_strands_model` rebuilds against the new
    provider/model/key. The Strands model cache is cleared too because its entries
    pin a backing provider client built with the previous key (its cache key omits
    the key fingerprint, so an in-place API-key rotation would otherwise be served
    stale). The provider-list (failover) cache is dropped too so a list edit takes
    effect immediately in this process. Safe to call when nothing is cached.

    Postconditions: the factory and provider-list caches are empty afterward.
        The Strands model cache is cleared on a best-effort basis; if the
        clear fails, a warning is logged and the cache may retain stale
        entries.
    """
    with _cache_lock:
        _client_cache.clear()
        _claude_cache.clear()
    provider_store.clear_cache()
    # Lazy import to avoid a circular import (strands_provider imports get_client).
    # Kept broad (not just ImportError): this guards BOTH the optional-dependency
    # import AND the clear_model_cache() call, and a cache clear must be best-effort
    # so a config change still succeeds. Warn (not debug): a swallowed failure here
    # silently leaves a stale key-pinned Strands adapter after a config change — the
    # bug this clears — so it must stay visible.
    try:
        from . import strands_provider

        strands_provider.clear_model_cache()
    except Exception:  # noqa: BLE001 - cache clear is best-effort (import or clear)
        logger.warning("Failed to clear Strands model cache", exc_info=True)
