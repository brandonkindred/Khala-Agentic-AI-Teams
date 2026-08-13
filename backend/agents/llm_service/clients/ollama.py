"""
Ollama-backed LLM client for the central LLM service.

Uses /v1/chat/completions (OpenAI-compatible), /api/show for context size,
retries/backoff, and concurrency limit. Supports Ollama Cloud auth.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

import httpx
from pydantic import BaseModel

from shared.llm_recovery import (
    extract_json_object as _shared_extract_json_object,
)

from .. import config as llm_config
from ..attribution import (
    bind_request_id,
    current_attribution,
    current_request_id,
    llm_attribution,
    new_request_id,
)
from ..attribution import (
    caller_team as _caller_team,
)
from ..backoff import parse_rate_limit_retry_config, rate_limit_backoff_sleep
from ..concurrency import get_llm_semaphore
from ..interface import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSemanticExhaustionError,
    LLMTemporaryError,
    LLMTruncatedError,
)
from ..limit_classification import classify_ollama_limit_kind
from ..telemetry import record_llm_call
from ..util import sha256_fingerprint

logger = logging.getLogger(__name__)


# Per-call response state (caller tag, token usage, latency). These are
# ContextVars rather than instance attributes because the client is a
# process-wide cached singleton shared across concurrent agents: per-thread/task
# isolation keeps each request's telemetry self-consistent, so a concurrent call
# can't overwrite one request's usage/latency/caller before ``_record_telemetry``
# reads them. Reset at the start of every public call so a failed call never
# records a previous call's token counts.
_caller_var: ContextVar[str] = ContextVar("llm_ollama_caller", default="unknown")
_usage_var: ContextVar[Optional[Dict[str, Any]]] = ContextVar("llm_ollama_usage", default=None)
_latency_var: ContextVar[int] = ContextVar("llm_ollama_latency_ms", default=0)


def _caller_tag() -> str:
    """Return 'module.function' of the first caller outside llm_service for log context.

    Walks the stack with ``sys._getframe`` rather than ``inspect.stack()``: this
    runs once per LLM request, and ``inspect.stack()`` materializes the whole
    stack *and* reads each frame's source file off disk to populate context
    lines we never use. Frame walking reads only ``f_globals['__name__']`` and
    ``f_code.co_name`` — no file I/O. On a runtime without ``sys._getframe`` it
    degrades to ``"unknown"`` (it does not error).
    """
    import sys

    getframe = getattr(sys, "_getframe", None)
    if getframe is None:  # pragma: no cover - non-CPython fallback
        return "unknown"
    try:
        frame = getframe(2)  # skip _caller_tag and its immediate (in-llm_service) caller
    except ValueError:  # pragma: no cover - shallow stack
        return "unknown"
    while frame is not None:
        mod = frame.f_globals.get("__name__", "")
        if mod and "llm_service" not in mod:
            func = frame.f_code.co_name
            # Shorten module path: "blogging.blog_writer_agent.agent" -> "blog_writer_agent.agent"
            parts = mod.rsplit(".", 2)
            short = ".".join(parts[-2:]) if len(parts) > 1 else mod
            return f"{short}.{func}"
        frame = frame.f_back
    return "unknown"


def _attribution_log_fields() -> str:
    """Return ``"agent=<a> team=<t> objective=<o>"`` from the current attribution.

    Used to stamp every LLM lifecycle log line (request, completion, retry,
    server-error, parse-failure, truncation, semantic-exhaustion) with the same
    attribution the request line carries, so operators filtering by ``agent``/
    ``team``/``objective`` see the whole life of a call — not just its opening
    request — without a second correlation lookup by ``rid``.

    Postconditions: returns a single-line, space-joined string; empty fields are
        rendered as ``-`` so the key is always present for log-grep predicates.
    """
    attr = current_attribution()
    return (
        f"agent={attr.agent_key or '-'} team={attr.team or '-'} objective={attr.objective or '-'}"
    )


# Default cap for max_tokens
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# Provisional context size used only when num_ctx cannot be resolved from the
# known-model table, env, or a successful /api/show call. Cached for at most
# _FALLBACK_NUM_CTX_TTL_S seconds (never permanently) so a transient /api/show
# outage cannot poison the process into silently truncating large prompts for
# the rest of its lifetime.
_FALLBACK_NUM_CTX = 16384
_ENV_NUM_CTX_FALLBACK_TTL = "LLM_NUM_CTX_FALLBACK_TTL_S"
_DEFAULT_NUM_CTX_FALLBACK_TTL_S = 300.0


def _fallback_num_ctx_ttl_s() -> float:
    """Return the provisional-fallback TTL in seconds (env override, defensive parse).

    Preconditions: none.
    Postconditions: returns a non-negative float; a missing or unparseable
        ``LLM_NUM_CTX_FALLBACK_TTL_S`` yields ``_DEFAULT_NUM_CTX_FALLBACK_TTL_S``;
        a negative value is floored to ``0.0`` (immediate retry on next call).
    """
    raw = os.environ.get(_ENV_NUM_CTX_FALLBACK_TTL)
    if not raw:
        return _DEFAULT_NUM_CTX_FALLBACK_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_NUM_CTX_FALLBACK_TTL_S


# Continuation on truncation (same behavior as software_engineering_team)
MAX_CONTINUATION_CYCLES = 10
CONTINUATION_CONTEXT_CHARS = 150

# One-shot corrective follow-up when chat(response_format="json") receives prose
# instead of a tool call or JSON. Triggered especially when tools are present:
# OpenAI-compatible endpoints cannot set response_format=json_object alongside
# tools, so models (e.g. qwen3-coder) sometimes narrate instead of acting.
_CHAT_JSON_CORRECTIVE_USER = (
    "Your previous reply was rejected: it was neither a tool call nor valid JSON. "
    "Either invoke a tool via the tools API, or respond with ONLY a single JSON "
    "object — no prose, no markdown, no code fences.\n"
    "Previous reply (truncated):\n{preview}"
)

# Max response/body length to log (avoid huge logs)
_MAX_LOG_BODY = 2000

# Expected keys for "try every code block" fallback
_EXPECTED_KEYS = frozenset(
    {
        "files",
        "summary",
        "code",
        "overview",
        "issues",
        "approved",
        "components",
        "architecture_document",
        "diagrams",
        "decisions",
        "tasks",
        "execution_order",
        "bugs_found",
        "integration_tests",
        "unit_tests",
        "readme_content",
        "feedback_items",
    }
)
_JSON_NOISE_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\uFFFD]")


def _think_payload_fields(think: "bool | str") -> dict:
    """Wire fields for reasoning controls.

    The client posts to the OpenAI-compatible /v1/chat/completions, where
    Ollama controls reasoning via ``reasoning_effort`` (whose values include
    ``"none"``) and documents the native ``think`` field as ignored.
    ``think`` is kept for native proxies and forward compatibility; level
    strings are mirrored into ``reasoning_effort`` so requested levels reach
    the model, and ``False`` maps to ``reasoning_effort="none"`` so the
    kill switch (explicit ``think=False`` or ``LLM_ENABLE_THINKING=false``)
    actually disables reasoning instead of leaving the model default.
    ``True`` stays on ``think`` only: it means "model-default thinking" for
    models with no registered levels, and pinning an effort level for an
    unknown model would be a guess.

    Preconditions:
        - ``think`` has been resolved (bool or level string, never None).
    Postconditions:
        - Returns a dict carrying ``think`` and, for level strings or
          ``False``, the corresponding ``reasoning_effort``.
    """
    fields: dict = {"think": think}
    if isinstance(think, str):
        fields["reasoning_effort"] = think
    elif think is False:
        fields["reasoning_effort"] = "none"
    return fields


def _normalize_schema_for_wire(schema: "dict | type[BaseModel]") -> dict:
    """Return a plain JSON-Schema dict for the wire, from a dict or a Pydantic model class.

    Preconditions: ``schema`` is either a ``dict`` (assumed already a valid
        JSON Schema object) or a class (not instance) subclassing
        ``pydantic.BaseModel``.
    Postconditions: returns a ``dict``. A dict input is returned unchanged
        (not copied — caller must not mutate it); a ``BaseModel`` subclass
        input is converted via ``.model_json_schema()`` (which itself returns
        a fresh dict on each call). Raises ``TypeError`` for any other input
        shape — fails fast/synchronously rather than sending a malformed
        wire payload.
    """
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    raise TypeError(f"schema must be a dict or a pydantic.BaseModel subclass, got {type(schema)!r}")


def _thinking_downgrade_enabled() -> bool:
    """Whether the proof-of-change thinking-downgrade retry is enabled (default: on).

    Preconditions: none.
    Postconditions: returns ``False`` only for an explicit ``"false"``/``"0"``/
        ``"no"`` (case-insensitive) ``LLM_THINKING_DOWNGRADE_RETRY``; unset or
        any other value means enabled. Never raises.
    """
    return llm_config.env_flag_enabled(llm_config.ENV_LLM_THINKING_DOWNGRADE_RETRY)


def _semantic_retry_think(model: str, active_think: "bool | str | None") -> "bool | str | None":
    """Next thinking value for a proof-of-change retry after a reasoning-only turn.

    A reasoning model in JSON mode can spend a whole turn in its reasoning channel
    and emit no assistant content. The recovery is to re-issue the call with
    reasoning reduced — a provably different payload — ending by disabling thinking
    entirely, the strongest available proof of change and the most reliable way to
    force such a model to open the content channel.

    The ladder, from ``active_think`` (the value the just-failed attempt used):
      - the model's TOP registered tier              -> one notch down (e.g. ``max`` -> ``high``);
      - any already-reduced tier / unregistered
        level string / boolean-on (``True``)         -> ``False`` (thinking disabled);
      - already-off (``False``) / no value (``None``) -> ``None`` (no further provable change).

    Intermediate registered tiers are intentionally skipped once below the top
    tier: for the models in use (e.g. ``deepseek-v4-pro:cloud``, whose
    low/medium/high all collapse to one wire reasoning effort) the intermediate
    tiers are wire-identical retries — a wasted multi-minute call. Jumping straight
    to thinking-off from a reduced tier is both cheaper and more effective.

    Preconditions:
        - ``active_think`` is a resolved wire value (bool, level string, or None).
    Postconditions:
        - Returns the next reduced setting, or ``None`` when nothing is left to
          change. The sequence strictly reduces reasoning and always terminates.
          Pure function: no env reads, never raises.
    """
    if active_think is None or active_think is False:
        return None
    if active_think is True:
        return False
    levels = llm_config.KNOWN_MODEL_THINKING_LEVELS.get(model) or ()
    if len(levels) > 1 and active_think == levels[-1]:
        # From the model's top tier: step down one notch first, giving the model a
        # chance to answer with reduced-but-still-on reasoning before it is cut off.
        return levels[-2]
    # Already at a reduced tier, or an unregistered level string: the only
    # remaining provable change that reliably forces content is disabling thinking.
    return False


def _max_semantic_retries(model: str, resolved_think: "bool | str | None") -> int:
    """Number of proof-of-change downgrade retries the ladder can spend.

    Mirrors :func:`_semantic_retry_think`'s ladder length so the request loop's
    log denominator and its hard cap stay honest:
      - top tier of a multi-tier model                     -> 2 (one notch down, then off);
      - any other on-thinking value (reduced tier,
        unregistered level string, boolean-on ``True``)    -> 1 (thinking-off);
      - already-off (``False``) / no value (``None``) /
        feature disabled                                   -> 0.

    Postconditions: returns a non-negative int; never raises. Reads
        ``LLM_THINKING_DOWNGRADE_RETRY`` (via ``_thinking_downgrade_enabled``), so
        it is not a pure function.
    """
    if not _thinking_downgrade_enabled():
        return 0
    if resolved_think is None or resolved_think is False:
        return 0
    if resolved_think is True:
        return 1
    levels = llm_config.KNOWN_MODEL_THINKING_LEVELS.get(model) or ()
    if len(levels) > 1 and resolved_think == levels[-1]:
        return 2
    return 1


def _reasoning_json_probe(reasoning: str) -> bool:
    """Best-effort diagnostic: does ``reasoning`` contain a JSON object that parses?

    Answers whether a semantically-exhausted turn's answer may have been misrouted
    into the reasoning channel (some providers route all tokens there when
    ``reasoning_effort`` is set), so operators can decide whether a reasoning-channel
    salvage is worthwhile. Diagnostic only — never used as answer content here.

    Postconditions: True iff the first-``{``-to-last-``}`` slice parses as JSON;
        never raises (any parse failure → False). ``RecursionError`` is caught
        explicitly: ``json.loads`` recurses per nesting level, so a degenerate
        deeply-nested reasoning channel would otherwise escape a
        ``ValueError``-only guard and crash the parse of an otherwise-valid call.
    """
    if not reasoning:
        return False
    start = reasoning.find("{")
    end = reasoning.rfind("}")
    if start == -1 or end <= start:
        return False
    try:
        json.loads(reasoning[start : end + 1])
        return True
    except (ValueError, RecursionError):
        return False


def _ollama_bearer_auth_headers() -> dict[str, str]:
    """Return the Authorization Bearer header for the model-listing Ollama request.

    Serves the module-level ``list_ollama_models`` (/api/tags) only. The key is
    resolved via :func:`llm_config.resolve_ollama_api_key` (runtime config set through
    the settings UI, falling back to ``OLLAMA_API_KEY`` / ``LLM_OLLAMA_API_KEY``). The
    per-request chat/embedding paths do NOT share this helper —
    :meth:`OllamaLLMClient._ollama_auth_headers` authenticates with the provider
    entry's own key exclusively (no env fallback), so the two paths resolve auth
    independently by design.

    Preconditions: none.
    Postconditions: returns ``{"Authorization": "Bearer <key>"}`` when a key is
        resolved, else ``{}`` (local Ollama needs no auth). Never raises.
    """
    key = llm_config.resolve_ollama_api_key()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def list_ollama_models(timeout: float = 15.0) -> list[str]:
    """Return the model ids available on the effective Ollama endpoint via /api/tags.

    Calls ``GET {resolve_base_url()}/api/tags`` with the resolved Ollama key for
    auth (no header when none is set, for local Ollama). The Ollama response shape
    is ``{"models": [{"name": "...", "model": "..."}, ...]}``; each entry's
    ``name`` is preferred, falling back to ``model``.

    Preconditions: ``timeout`` is a positive number of seconds.
    Postconditions: returns a de-duplicated, sorted list of non-empty model names.
        Returns ``[]`` on any HTTP error, non-200 status, or unparseable body —
        callers fall back to the curated suggestion list. Never raises. (Returning
        ``[]`` on failure is the documented contract here, not a swallowed error:
        live model discovery is best-effort and must degrade gracefully.)
    """
    base_url = llm_config.resolve_base_url()
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        headers = _ollama_bearer_auth_headers()
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning("Ollama /api/tags returned %s for %s", resp.status_code, url)
            return []
        data = resp.json()
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        names: set[str] = set()
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        return sorted(names)
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
        logger.warning("Could not list Ollama models from %s: %s", url, e)
        return []


class _EmptyResponseSignal(Exception):
    """Internal control-flow signal: a 200 response produced no assistant content.

    Deliberately subclasses plain ``Exception`` (not ``LLMError``) so the retry
    loop's ``except LLMTemporaryError`` clause can never swallow it — only the
    dedicated semantic-exhaustion handler in ``_ollama_post`` consumes it.

    Preconditions: ``finish_reason`` is the response's finish reason;
        ``has_reasoning`` is True iff the response carried reasoning tokens;
        ``content_len`` is the (stripped-empty) raw content length in chars;
        ``reasoning_len`` is the raw reasoning-channel length in chars and
        ``reasoning_has_json`` whether a JSON object was found there (diagnostics
        for the reasoning-channel-salvage decision — no raw content is retained).
    Invariants: never escapes ``_ollama_post``.
    """

    def __init__(
        self,
        finish_reason: str,
        has_reasoning: bool,
        content_len: int,
        *,
        reasoning_len: int = 0,
        reasoning_has_json: bool = False,
    ):
        super().__init__(
            f"empty response (finish_reason={finish_reason}, has_reasoning={has_reasoning})"
        )
        self.finish_reason = finish_reason
        self.has_reasoning = has_reasoning
        self.content_len = content_len
        self.reasoning_len = reasoning_len
        self.reasoning_has_json = reasoning_has_json


def _parse_retry_config() -> tuple[int, float, float]:
    """Parse retry env vars. Returns (max_retries, initial_backoff_seconds, backoff_max_seconds).

    Backoff is exponential: wait initial * 2^attempt after each failure (first retry ~initial seconds).
    """
    raw_retries = os.environ.get(llm_config.ENV_LLM_MAX_RETRIES) or "10"
    raw_initial = os.environ.get(llm_config.ENV_LLM_BACKOFF_BASE) or "2"
    raw_max = os.environ.get(llm_config.ENV_LLM_BACKOFF_MAX) or "120"
    try:
        max_retries = max(0, int(raw_retries))
    except ValueError:
        max_retries = 10
    try:
        initial_backoff = float(raw_initial)
    except ValueError:
        initial_backoff = 2.0
    try:
        backoff_max = float(raw_max)
    except ValueError:
        backoff_max = 120.0
    return max_retries, initial_backoff, backoff_max


def _exponential_retry_delay(
    failed_attempt_index: int, initial_seconds: float, cap_seconds: float
) -> float:
    """Seconds to wait before the next HTTP attempt. failed_attempt_index is 0 after the first failure (waits ~initial_seconds)."""
    base = initial_seconds * (2**failed_attempt_index)
    jitter = random.uniform(0, min(2.0, max(0.25, base * 0.1)))
    return min(base + jitter, cap_seconds)


def _honor_retry_after_enabled() -> bool:
    """Whether a 429 ``Retry-After`` header should be honored (default: on).

    Preconditions: none.
    Postconditions: returns ``False`` only for an explicit ``"false"``/``"0"``/
        ``"no"`` (case-insensitive) ``LLM_RATE_LIMIT_HONOR_RETRY_AFTER``; unset or
        any other value means enabled. Never raises.
    """
    return llm_config.env_flag_enabled(llm_config.ENV_LLM_RATE_LIMIT_HONOR_RETRY_AFTER)


def _parse_retry_after_seconds(headers: Any) -> Optional[float]:
    """Extract an integer-seconds ``Retry-After`` value from response headers.

    Only the integer-seconds form is honored. The HTTP-date form, a non-numeric
    value, a missing header, or a non-positive value all yield ``None`` (fall
    back to the computed schedule). Honoring is gated by
    ``_honor_retry_after_enabled`` at the call site.

    Preconditions: ``headers`` is a mapping-like object exposing ``.get`` (an
        httpx ``Headers`` or a plain dict) or ``None``.
    Postconditions: returns a positive ``float`` or ``None``; never raises.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _rate_limit_error_from_response(
    *,
    body: str,
    headers: Any,
    attempt: int,
    cause: Optional[Exception] = None,
) -> LLMRateLimitError:
    """Build an ``LLMRateLimitError`` from a 429 response, classifying the body.

    Preconditions: ``attempt`` is a non-negative int (1-based attempt count in
        the raised message); ``body`` is the response text (may be empty).
    Postconditions: returns an ``LLMRateLimitError`` with ``limit_kind`` set from
        :func:`classify_ollama_limit_kind`, ``retry_after_seconds`` from headers
        when honoring is enabled, and a message that includes a body snippet.
        Never raises.
    """
    body_text = body or ""
    limit_kind = classify_ollama_limit_kind(body_text)
    snippet = body_text.strip().replace("\n", " ")
    if len(snippet) > 300:
        snippet = snippet[:300] + "…"
    detail = f": {snippet}" if snippet else ""
    retry_after = (
        _parse_retry_after_seconds(headers) if _honor_retry_after_enabled() else None
    )
    return LLMRateLimitError(
        f"LLM rate limited (429) after {attempt} attempt(s) [{limit_kind}]{detail}",
        status_code=429,
        cause=cause,
        retry_after_seconds=retry_after,
        limit_kind=limit_kind,
    )


def _rate_limit_backoff_sleep(
    rate_limit_attempt: int,
    rate_limit_max_retries: int,
    rate_limit_initial: float,
    rate_limit_cap: float,
    retry_after_seconds: Optional[float],
) -> None:
    """Sleep the slow 429 backoff for the given attempt index, logging one warning.

    Thin wrapper over the shared :func:`llm_service.backoff.rate_limit_backoff_sleep`
    so the Claude and Ollama clients share one 429 wait/log/sleep implementation;
    this only supplies the Ollama request-id and attribution context for the log.

    Preconditions: ``0 <= rate_limit_attempt < rate_limit_max_retries``; the caller
        has already exited the concurrency semaphore and HTTP stream contexts —
        this sleep can be minutes long and must not hold a shared resource.
    Postconditions: sleeps ``rate_limit_retry_delay(...)`` seconds; never raises.
    """
    rate_limit_backoff_sleep(
        rate_limit_attempt,
        rate_limit_max_retries,
        rate_limit_initial,
        rate_limit_cap,
        retry_after_seconds,
        request_id=current_request_id() or "-",
        context=_attribution_log_fields(),
    )


class OllamaLLMClient(LLMClient):
    """LLM client that talks to Ollama (or OpenAI-compatible) /v1/chat/completions."""

    def __init__(
        self,
        model: str = "deepseek-v4-flash:cloud",
        *,
        base_url: str = "https://ollama.com",
        timeout: float = 3600.0,
        on_reasoning: Optional[Callable[[str], None]] = None,
        rate_limit_max_retries: Optional[int] = None,
        api_key: str = "",
    ) -> None:
        """Construct an Ollama-backed LLM client.

        ``on_reasoning`` is an optional sink called once per reasoning ("thinking")
        delta as it streams in, with that delta's text. It lets a caller surface
        the model's thinking live (e.g. to a job record / UI) without changing the
        return contract. Best-effort: a hook exception never affects the LLM call.

        ``rate_limit_max_retries`` overrides the in-place 429 backoff retry budget
        (normally from ``LLM_RATE_LIMIT_MAX_RETRIES``). The multi-provider failover
        path passes ``0`` so a 429 raises immediately and hands off to the next
        provider instead of sleeping minutes; ``None`` keeps the env-configured
        schedule.

        ``api_key`` is this client's Ollama Cloud key (a fallback-list entry's own
        stored key). It authenticates every request with NO environment fallback —
        the provider list is the sole source and each entry is self-contained. An
        empty key means no Authorization header (a local Ollama endpoint needs none).

        Preconditions: ``on_reasoning`` is callable or ``None``;
            ``rate_limit_max_retries`` is ``None`` or ``>= 0``.
        Postconditions: when set, the hook receives every reasoning delta of every
            streamed response in arrival order; otherwise reasoning handling is
            unchanged.
        """
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.on_reasoning = on_reasoning
        self._rate_limit_max_retries_override = (
            max(0, int(rate_limit_max_retries)) if rate_limit_max_retries is not None else None
        )
        self._api_key_override = (api_key or "").strip()
        self._model_num_ctx: Optional[int] = None
        # Provisional num_ctx fallback (set only when /api/show cannot be resolved),
        # with the wall-clock time it was recorded. Never written to _model_num_ctx.
        self._fallback_num_ctx: Optional[int] = None
        self._fallback_num_ctx_ts: float = 0.0

    def _record_telemetry(
        self,
        *,
        status: str = "success",
        error_type: Optional[str] = None,
        prompt_text: Optional[str] = None,
        response_text: Optional[str] = None,
    ) -> None:
        """Record LLM call telemetry using data from the last _ollama_post call.

        Attribution (agent_key/team/objective/request_id) and response state
        (caller/usage/latency) are all sourced from per-call contextvars bound by
        the public ``complete_json``/``complete``/``chat`` entrypoints and
        ``_ollama_post``, so the whole record stays self-consistent even though the
        client is a process-wide cached singleton shared across concurrent agents.

        Invariant: this is only ever called from within one of those entrypoints'
        bound request scope (so the contextvars reflect *this* call). It is not a
        standalone public API — do not call it outside that scope.
        """
        usage = _usage_var.get() or {}
        caller = _caller_var.get()
        attr = current_attribution()
        try:
            record_llm_call(
                team=attr.team,
                agent_key=attr.agent_key,
                model=self.model,
                caller_tag=caller,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                latency_ms=_latency_var.get(),
                status=status,
                error_type=error_type,
                job_id=attr.job_id or None,
                objective=attr.objective,
                request_id=current_request_id(),
                task_id=attr.task_id,
                phase=attr.phase,
                prompt_text=prompt_text,
                response_text=response_text,
            )
        except Exception:
            logger.debug("Failed to record LLM telemetry", exc_info=True)

    def _ollama_auth_headers(self) -> dict[str, str]:
        """Return the Authorization Bearer header for this client's Ollama requests.

        The client authenticates ONLY with its own ``api_key`` (a fallback-list
        entry's stored key) — there is no environment fallback, because the provider
        list is the sole source of LLM configuration and each entry is self-contained.
        An empty key -> no header (a local Ollama endpoint needs none; a Cloud entry
        must carry its own key, enforced by the route's credentials guard). This is
        deliberately NOT the module-level :func:`_ollama_bearer_auth_headers`, which
        keeps an env fallback for the operator-only ``/ollama-models`` browse utility.

        Preconditions: none. Postconditions: returns ``{"Authorization": "Bearer
            <key>"}`` when this client has a non-empty key, else ``{}``. Never raises.
        """
        key = self._api_key_override
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _rate_limit_retry_config(self) -> "tuple[int, float, float]":
        """Return the 429 backoff config, applying this client's retry override.

        Postconditions: returns ``(max_retries, initial, cap)`` from
            :func:`parse_rate_limit_retry_config`, with ``max_retries`` replaced by
            this client's ``rate_limit_max_retries`` override when one was given
            (the failover path passes ``0`` for immediate hand-off). Never raises.
        """
        max_retries, initial, cap = parse_rate_limit_retry_config()
        if self._rate_limit_max_retries_override is not None:
            max_retries = self._rate_limit_max_retries_override
        return max_retries, initial, cap

    def _fetch_model_num_ctx(self) -> int:
        """Resolve the model's num_ctx from the known-model table, env, or Ollama /api/show.

        An authoritatively-resolved value (known table, env override, or a
        successful /api/show parse) is cached for the process lifetime. When
        /api/show cannot be resolved we degrade to ``_FALLBACK_NUM_CTX`` but cache
        it only provisionally — for at most ``_fallback_num_ctx_ttl_s()`` — so a
        transient outage is retried on a later call instead of poisoning the
        process into silently truncating large prompts forever.

        Preconditions: none.
        Postconditions: returns ``>= 2048``; an authoritative result is cached in
            ``self._model_num_ctx`` permanently; a fallback result is returned
            without writing ``self._model_num_ctx`` and is reused (skipping
            /api/show) only until its TTL elapses.
        Invariants: ``self._model_num_ctx``, once set, is only ever set to an
            authoritatively-resolved value — never ``_FALLBACK_NUM_CTX``.
        """
        if self._model_num_ctx is not None:
            return self._model_num_ctx
        ctx = llm_config.resolve_context_size_for_model(self.model)
        if ctx is not None:
            self._model_num_ctx = ctx
            logger.info(
                "LLM model %s: using known/context size %s", self.model, self._model_num_ctx
            )
            return self._model_num_ctx
        # Reuse a recent provisional fallback instead of hammering a down /api/show.
        if (
            self._fallback_num_ctx is not None
            and time.time() - self._fallback_num_ctx_ts < _fallback_num_ctx_ttl_s()
        ):
            return self._fallback_num_ctx
        try:
            url = f"{self.base_url}/api/show"
            headers = self._ollama_auth_headers()
            with httpx.Client(timeout=min(30, self.timeout)) as client:
                resp = client.post(url, json={"model": self.model}, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "Ollama /api/show returned %s for model %s; using %s (retry after %ss)",
                    resp.status_code,
                    self.model,
                    _FALLBACK_NUM_CTX,
                    _fallback_num_ctx_ttl_s(),
                )
                return self._record_fallback_num_ctx()
            data = resp.json()
            params_str = data.get("parameters") or ""
            match = re.search(r"num_ctx\s+(\d+)", params_str, re.IGNORECASE)
            if match:
                self._model_num_ctx = max(2048, int(match.group(1)))
                logger.info("Ollama model %s num_ctx=%s", self.model, self._model_num_ctx)
                return self._model_num_ctx
            for path in ("model_info", "details"):
                obj = data.get(path)
                if isinstance(obj, dict):
                    ctx_val = obj.get("num_ctx") or obj.get("context_length")
                    if ctx_val is not None:
                        self._model_num_ctx = max(2048, int(ctx_val))
                        return self._model_num_ctx
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Could not fetch Ollama model info for %s: %s; using %s (retry after %ss)",
                self.model,
                e,
                _FALLBACK_NUM_CTX,
                _fallback_num_ctx_ttl_s(),
            )
        return self._record_fallback_num_ctx()

    def _resolve_max_tokens(self, explicit: Optional[int]) -> int:
        """Resolve the output-token cap for a request.

        Precedence: an explicit ``max_tokens`` arg, else ``LLM_MAX_OUTPUT_TOKENS`` (via the
        centralized ``llm_config.resolve_max_output_tokens``), else the model's context
        window — always capped at ``DEFAULT_MAX_OUTPUT_TOKENS``.

        Preconditions: none.
        Postconditions: returns an ``int`` ``<= DEFAULT_MAX_OUTPUT_TOKENS``; a
            malformed or non-positive ``LLM_MAX_OUTPUT_TOKENS`` falls back to the model
            context. Never raises.
        """
        max_tokens = explicit
        if max_tokens is None:
            # Centralized resolver returns 0 when LLM_MAX_OUTPUT_TOKENS is unset, malformed,
            # or non-positive — in which case fall back to the model's context window.
            env_max = llm_config.resolve_max_output_tokens()
            if env_max > 0:
                max_tokens = min(env_max, DEFAULT_MAX_OUTPUT_TOKENS)
            else:
                max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
        return min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)

    def _begin_call_state(self) -> "tuple[str, Any]":
        """Tag the caller and reset per-call response contextvars.

        Preconditions: none.
        Postconditions: returns ``(caller, attribution)`` and clears the usage /
            latency contextvars up front so a failed call never reports a previous
            call's token counts or latency. Never raises.
        """
        caller = _caller_tag()
        _caller_var.set(caller)
        _usage_var.set(None)
        _latency_var.set(0)
        return caller, current_attribution()

    def _record_fallback_num_ctx(self) -> int:
        """Record the provisional num_ctx fallback with the current time and return it.

        Postconditions: ``self._fallback_num_ctx == _FALLBACK_NUM_CTX``,
            ``self._fallback_num_ctx_ts`` is the current wall-clock time, and
            ``self._model_num_ctx`` is left untouched (still ``None``).
        """
        self._fallback_num_ctx = _FALLBACK_NUM_CTX
        self._fallback_num_ctx_ts = time.time()
        return _FALLBACK_NUM_CTX

    def get_max_context_tokens(self) -> int:
        """Return model's num_ctx (cached)."""
        return self._fetch_model_num_ctx()

    def supports_structured_output(self) -> bool:
        """Ollama's OpenAI-compatible endpoint accepts a ``response_format=
        {"type": "json_schema", ...}`` payload for decoder-level schema
        enforcement (see ``_complete_json_impl``).

        Preconditions: none.
        Postconditions: always returns True. Never raises.
        """
        return True

    def _log_llm_server_error(
        self,
        status_code: int,
        response_text: Optional[str],
        response_headers: Optional[Any],
        attempt: int,
        reason: str = "",
    ) -> None:
        """Log full server error details (status, body, useful headers) at ERROR level."""
        body = (response_text or "")[:_MAX_LOG_BODY]
        if len(response_text or "") > _MAX_LOG_BODY:
            body += "... [truncated]"
        extra_headers = ""
        if response_headers is not None:
            useful = ["content-type", "retry-after", "x-request-id"]
            parts = []
            for name in useful:
                try:
                    v = response_headers.get(name)
                    if v is not None:
                        parts.append(f"{name}={v!r}")
                except (TypeError, AttributeError):
                    pass
            if parts:
                extra_headers = " headers=" + ", ".join(parts)
        reason_str = f" reason={reason}" if reason else ""
        logger.error(
            "LLM server error response: rid=%s %s status=%s model=%s base_url=%s attempt=%s%s.%s Response body: %s",
            current_request_id() or "-",
            _attribution_log_fields(),
            status_code,
            self.model,
            self.base_url,
            attempt,
            reason_str,
            extra_headers,
            body,
        )

    def _strip_json_noise(self, s: str) -> str:
        """Drop transport artifacts (BOM/replacement chars/control bytes) from JSON-ish text."""
        if not s:
            return s
        s = s.replace("\ufeff", "")
        return _JSON_NOISE_RE.sub("", s)

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Extract a single JSON object from model output. Raises LLMJsonParseError on failure.

        The salvage core is the shared ``shared.llm_recovery`` engine (one
        string-aware brace scanner + one ``json-repair`` site for the whole
        codebase); this method keeps only the two Ollama-specific pre-checks the
        shared engine can't know about:

        - ``---DRAFT---`` marker → the trailing draft is returned as ``content``.
        - A ``__tool_calls__`` envelope is passed through verbatim (it carries no
          ``_EXPECTED_KEYS`` anchor, so tier-1 salvage would otherwise drop it).
        - Salvage runs anchored on ``_EXPECTED_KEYS`` first (filters usage echoes
          / format recaps in multi-candidate output), then falls back to
          accept-any so a clean lone object with an off-schema key still parses.
        - ``repair_truncated=False`` keeps complete-but-broken repair (trailing
          commas, unescaped quotes) but lets a genuinely truncated reply surface
          as ``LLMJsonParseError``, so the caller recovers the rest via multi-turn
          continuation instead of accepting a fabricated tail. The engine — which
          strips wrappers/fences and knows the real payload boundaries — owns this
          decision, rather than a caller-side "looks truncated" heuristic that
          misfires on prose/fence prefixes and on braces inside string values.

        Preconditions:
            - ``text`` is a ``str`` (may be empty); it is the raw assistant
              content, before any structured parsing.
        Postconditions:
            - Returns a parsed ``dict`` on success; raises ``LLMJsonParseError``
              when nothing salvageable is found (a truncated payload is treated
              as unsalvageable so the caller can continue).
        """
        text = self._strip_json_noise(text)
        if "---DRAFT---" in text:
            parts = text.split("---DRAFT---", 1)
            if len(parts) == 2 and parts[1].strip():
                return {"content": parts[1].strip()}
        stripped = text.strip()
        # Tool-call envelope: not an _EXPECTED_KEYS anchor, so route it around
        # salvage and return it as-is (chat() relies on this pass-through).
        if stripped.startswith("{") and "__tool_calls__" in stripped:
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "__tool_calls__" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass
        # Prefer an _EXPECTED_KEYS-anchored object (filters usage echoes / recaps)
        # but fall back to any clean object for off-schema replies — resolved in
        # one engine pass. repair_truncated=False (not a caller-side heuristic)
        # makes only genuine truncation unsalvageable so the implicit-truncation
        # continuation path fires.
        result = _shared_extract_json_object(
            text,
            required_keys=_EXPECTED_KEYS,
            accept_any_fallback=True,
            repair_truncated=False,
        )
        if result is not None:
            return result
        logger.error(
            "LLM JSON parse failed. rid=%s %s model=%s base_url=%s. Raw content (truncated): %s",
            current_request_id() or "-",
            _attribution_log_fields(),
            self.model,
            self.base_url,
            text[:_MAX_LOG_BODY] + ("... [truncated]" if len(text) > _MAX_LOG_BODY else ""),
        )
        raise LLMJsonParseError(
            "Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. "
            f"Response preview: {text[:500]!r}...",
            error_kind="json_parse",
            response_preview=text[:500],
            raw_response=text,
        )

    def _resolve_think(
        self, think: "bool | str | None", *, response_format: str = "text"
    ) -> "bool | str":
        """Resolve the caller's think request into the wire value for this model.

        Delegates to ``llm_config.resolve_think_for_model``: explicit values
        win (string level verbatim, False off); True/None upgrade to the
        model's highest registered thinking level ("max thinking"), or plain
        True for models with no registered levels; None respects the
        LLM_ENABLE_THINKING global default. ``response_format="json"`` with no
        explicit ``think`` and no per-agent pin resolves to False instead of
        the model's max tier — see ``resolve_think_for_model``.
        """
        return llm_config.resolve_think_for_model(
            self.model, think, response_format=response_format
        )

    def _parse_response_content(self, data: dict) -> str:
        """Extract content or tool_calls from OpenAI-compatible response.

        Returns content string for normal replies, or a JSON-serialized
        ``{"__tool_calls__": [...]}`` dict string when the model invokes tools.
        Raises LLMTruncatedError if finish_reason=length with partial content.
        Empty content (no tool calls) always raises ``_EmptyResponseSignal``;
        retry POLICY (proof-of-change downgrade vs the legacy transient
        schedule) is decided solely by ``_ollama_post``'s handler, never here.

        Postconditions: a returned string is never empty after ``strip()``.
        """
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise LLMPermanentError(
                "Unexpected response format from LLM: missing or invalid 'choices'"
            )
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMPermanentError("Unexpected response format from LLM: invalid choice object")
        finish_reason = first.get("finish_reason", "")
        msg = first.get("message")
        if not msg or not isinstance(msg, dict):
            raise LLMPermanentError(
                "Unexpected response format from LLM: missing or invalid 'message'"
            )
        # Tool calls take priority — model is invoking a function rather than replying with text.
        tool_calls = msg.get("tool_calls")
        if tool_calls or finish_reason == "tool_calls":
            if not tool_calls:
                raise LLMPermanentError(
                    "LLM returned finish_reason=tool_calls but no tool_calls in message"
                )
            logger.info("LLM returned %d tool call(s)", len(tool_calls))
            envelope: dict = {"__tool_calls__": tool_calls}
            # DeepSeek thinking mode requires the tool-call turn's
            # reasoning_content to be passed back on subsequent requests
            # (400 otherwise) — surface it for the echo-back paths.
            if msg.get("reasoning_content"):
                envelope["__reasoning_content__"] = msg["reasoning_content"]
            return json.dumps(envelope)
        # reasoning_len/has_reasoning are O(1); the JSON probe is O(n) + a parse,
        # so it is computed lazily only on the two empty-content raise paths below
        # (its result is consumed solely by the semantic-exhaustion receipt) — never
        # on the hot path of a successful response.
        reasoning_text = str(msg.get("reasoning_content") or "")
        has_reasoning = bool(reasoning_text)
        reasoning_len = len(reasoning_text)
        if finish_reason == "length":
            partial_content = msg.get("content", "")
            partial_content = str(partial_content) if partial_content else ""
            if not partial_content.strip():
                raise _EmptyResponseSignal(
                    "length",
                    has_reasoning,
                    len(partial_content),
                    reasoning_len=reasoning_len,
                    reasoning_has_json=_reasoning_json_probe(reasoning_text),
                )
            logger.warning(
                "LLM response truncated (rid=%s, %s, finish_reason=length). Partial content: %d chars",
                current_request_id() or "-",
                _attribution_log_fields(),
                len(partial_content),
            )
            raise LLMTruncatedError(
                "Response truncated due to token limit (finish_reason=length)",
                partial_content=partial_content,
                finish_reason=finish_reason,
            )
        content = msg.get("content")
        if content is None:
            raise LLMPermanentError("Unexpected response format from LLM: missing 'content'")
        content_str = str(content)
        if not content_str.strip():
            raise _EmptyResponseSignal(
                str(finish_reason or "stop"),
                has_reasoning,
                len(content_str),
                reasoning_len=reasoning_len,
                reasoning_has_json=_reasoning_json_probe(reasoning_text),
            )
        return content_str

    def _ollama_post(
        self,
        payload: dict,
        max_retries: int,
        initial_backoff: float,
        backoff_max: float,
        rate_limit_max_retries: int,
        rate_limit_initial: float,
        rate_limit_cap: float,
        sem: threading.BoundedSemaphore,
        *,
        resolved_think: "bool | str | None" = None,
        schema_forced: bool = False,
    ) -> str:
        """POST to /v1/chat/completions with SSE streaming; return raw content. Raises LLM* on non-200 or malformed.

        Token usage and latency from the response are stored in the per-call
        ``_usage_var`` / ``_latency_var`` contextvars after each successful call,
        keeping each request's telemetry self-consistent under concurrency.

        Retries use two INDEPENDENT budgets: transient 5xx/network faults retry on
        the fast schedule (``max_retries`` / ``initial_backoff`` / ``backoff_max``),
        while HTTP 429 rate limits retry on the slow rate-limit schedule
        (``rate_limit_max_retries`` / ``rate_limit_initial`` / ``rate_limit_cap``).

        Semantic exhaustion (HTTP 200 with zero assistant content, typically a
        reasoning-only response) is a THIRD failure class with its own budget: an
        immediate proof-of-change retry ladder that steps reasoning down and ends
        by disabling thinking entirely (see ``_semantic_retry_think`` /
        ``_max_semantic_retries`` — up to two rungs from a top tier, e.g.
        ``max`` -> ``high`` -> off), after which the call fails hard with
        ``LLMSemanticExhaustionError`` instead of re-sending the same payload on
        the transient schedule.

        Preconditions: ``max_retries`` and ``rate_limit_max_retries`` are ``>= 0``;
            ``rate_limit_cap >= rate_limit_initial > 0``; ``sem`` is the global
            concurrency semaphore. ``resolved_think`` is the resolved thinking
            wire value baked into ``payload`` (bool or level string), or ``None``
            when the caller cannot offer a thinking downgrade. ``schema_forced``
            True means ``payload["response_format"]`` requests provider-enforced
            JSON-Schema decoding (see ``_normalize_schema_for_wire``).
        Postconditions: returns the raw assistant content on success. A 429 retry's
            ``time.sleep`` happens at the loop-level handler — AFTER the semaphore
            and HTTP stream contexts have been released — never while holding them.
            The transient schedule is unaffected by the rate-limit schedule and
            vice versa. Raises ``LLMRateLimitError`` only after the rate-limit
            budget is exhausted, ``LLMTemporaryError`` after the transient budget,
            ``LLMSemanticExhaustionError`` after the thinking-downgrade ladder is
            exhausted, or ``LLMPermanentError``/``LLMTruncatedError`` immediately.
            When ``schema_forced`` is True, the FIRST empty-response signal
            bypasses the thinking-downgrade ladder entirely (and the
            ``_thinking_downgrade_enabled`` kill switch) and immediately raises
            ``LLMSemanticExhaustionError(schema_forced=True)`` — a schema-forced
            starvation is a terminal fallback signal, never a proof-of-change
            retry candidate, so this path cannot regress into the
            previously-reverted retry-loop-on-starvation bug because there is no
            retry loop for it at all.
        """
        url = f"{self.base_url}/v1/chat/completions"
        last_error: Optional[Exception] = None
        headers = self._ollama_auth_headers()
        stream_payload = {**payload, "stream": True}
        # Three INDEPENDENT retry budgets share one loop: a 429 must never consume
        # a transient attempt and vice versa, and the semantic-exhaustion budget
        # (a thinking-downgrade ladder) is separate from both. The loop is bounded
        # by the sum of all budgets (+1 for the first attempt) so it always
        # terminates by returning or raising.
        transient_attempt = 0
        rate_limit_attempt = 0
        # Semantic-exhaustion state: `semantic_attempt` counts the proof-of-change
        # downgrade retries spent for this call. The ladder steps reasoning down
        # (ending in thinking-off) up to `max_semantic_retries` times before the
        # call is declared exhausted — one max->high notch alone often still
        # exhausts, so a reduced tier is followed by a decisive thinking-off retry.
        semantic_attempt = 0
        active_think: "bool | str | None" = resolved_think
        any_content_bytes = False
        # Reasoning-channel diagnostics accumulated ACROSS attempts (like
        # any_content_bytes): the final thinking-off rung carries no reasoning, so
        # taking these from the last signal alone would report 0/False even when an
        # earlier reasoning-heavy rung held the misrouted answer.
        reasoning_len_seen = 0
        reasoning_has_json_seen = False
        max_semantic_retries = _max_semantic_retries(self.model, resolved_think)
        # Log denominator: one slot for the first attempt plus every budget.
        max_total_attempts = max_retries + rate_limit_max_retries + 1 + max_semantic_retries
        rl_log_body: Optional[str] = None
        rl_log_headers: Any = None
        # Same capture-and-log-outside pattern as the 429 path, for HTTP 5xx: the
        # body/headers are grabbed inside the stream context and logged on
        # exhaustion by the outer ``except LLMTemporaryError`` handler, so the 5xx
        # transient backoff sleep never runs while holding the shared gate.
        srv_log_body: Optional[str] = None
        srv_log_headers: Any = None
        attempt = 0

        def _retry_transient_step(detail: str, kind: str = "temporary error") -> bool:
            """Consume one transient-schedule attempt: log, sleep, count.

            Single owner of the transient backoff mechanics for every handler
            in this loop, so the schedule cannot drift between failure kinds.

            Preconditions: called only from a failure branch of the request loop.
            Postconditions: True → one attempt was consumed (slept on the
                exponential schedule; caller must ``continue``); False → the
                transient budget is exhausted and nothing changed (caller raises).
            """
            nonlocal transient_attempt
            if transient_attempt >= max_retries:
                return False
            wait = _exponential_retry_delay(transient_attempt, initial_backoff, backoff_max)
            logger.warning(
                "LLM %s (rid=%s, %s, attempt %d/%d): %s. Retrying in %.1fs",
                kind,
                current_request_id() or "-",
                _attribution_log_fields(),
                attempt + 1,
                max_total_attempts,
                detail,
                wait,
            )
            time.sleep(wait)
            transient_attempt += 1
            return True

        while True:
            attempt = transient_attempt + rate_limit_attempt + semantic_attempt
            try:
                with sem:
                    logger.info(
                        "Waiting for LLM response (timeout=%ss, attempt %d/%d)...",
                        int(self.timeout),
                        attempt + 1,
                        max_total_attempts,
                    )
                    t0 = time.monotonic()
                    with httpx.Client(timeout=self.timeout) as client:
                        with client.stream(
                            "POST", url, json=stream_payload, headers=headers
                        ) as response:
                            status = response.status_code
                            if status != 200:
                                response.read()
                            if status == 200:
                                content_parts: list[str] = []
                                reasoning_parts: list[str] = []
                                finish_reason: Optional[str] = None
                                tool_call_buffers: dict[int, dict] = {}
                                has_reasoning: bool = False
                                partial_buf = ""  # buffer for lines split across TCP chunks
                                usage_data: Optional[Dict[str, Any]] = (
                                    None  # token usage from final chunk
                                )
                                for raw_line in response.iter_lines():
                                    if not raw_line:
                                        continue

                                    # --- Resolve partial-buffer / current-line into chunk_data ---
                                    chunk_data: Optional[str] = None
                                    if partial_buf:
                                        # Try joining buffered partial with this line
                                        combined = partial_buf + raw_line
                                        partial_buf = ""
                                        if combined.startswith("data:"):
                                            cdata = combined[5:].lstrip()
                                            if cdata.strip() == "[DONE]":
                                                break
                                            try:
                                                json.loads(cdata)  # validate
                                                chunk_data = cdata
                                            except json.JSONDecodeError:
                                                # Combined still invalid — discard buffer,
                                                # fall through to try raw_line on its own.
                                                logger.debug(
                                                    "Discarding unrecoverable partial SSE buffer"
                                                )

                                    if chunk_data is None:
                                        # Process raw_line normally
                                        if not raw_line.startswith("data:"):
                                            continue
                                        chunk_data = raw_line[5:].lstrip()
                                        if chunk_data.strip() == "[DONE]":
                                            break
                                        try:
                                            json.loads(chunk_data)  # validate
                                        except json.JSONDecodeError:
                                            # May be split across TCP frames — buffer for next line
                                            partial_buf = raw_line
                                            continue

                                    chunk = json.loads(chunk_data)
                                    # Capture token usage (typically in the last SSE chunk)
                                    chunk_usage = chunk.get("usage")
                                    if chunk_usage and isinstance(chunk_usage, dict):
                                        usage_data = chunk_usage
                                    choices = chunk.get("choices") or []
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        piece = delta.get("content")
                                        if piece:
                                            content_parts.append(piece)
                                        # Track reasoning tokens so we can distinguish
                                        # "model thought but produced no answer" from
                                        # "model returned nothing at all". Ollama's
                                        # OpenAI-compatible endpoint streams thinking
                                        # as delta.reasoning (openai.go); DeepSeek's
                                        # native dialect uses reasoning_content.
                                        reasoning = (
                                            delta.get("reasoning")
                                            or delta.get("reasoning_content")
                                            or delta.get("thinking")
                                        )
                                        if reasoning:
                                            has_reasoning = True
                                            reasoning_text = str(reasoning)
                                            # Kept whole: DeepSeek thinking mode
                                            # requires tool-call turns to echo
                                            # reasoning_content back on the next
                                            # request (400 otherwise).
                                            reasoning_parts.append(reasoning_text)
                                            # Surface the thinking delta live to an
                                            # optional sink (e.g. a job record / UI).
                                            # Best-effort: a hook error must never
                                            # affect the LLM call or the stream.
                                            if self.on_reasoning is not None:
                                                try:
                                                    self.on_reasoning(reasoning_text)
                                                except Exception:  # noqa: BLE001
                                                    logger.debug(
                                                        "on_reasoning hook raised; ignoring",
                                                        exc_info=True,
                                                    )
                                        for tc in delta.get("tool_calls") or []:
                                            idx = tc.get("index", 0)
                                            if idx not in tool_call_buffers:
                                                tool_call_buffers[idx] = {
                                                    "id": "",
                                                    "type": "function",
                                                    "function": {"name": "", "arguments": ""},
                                                }
                                            buf = tool_call_buffers[idx]
                                            if tc.get("id"):
                                                buf["id"] = tc["id"]
                                            if tc.get("type"):
                                                buf["type"] = tc["type"]
                                            fn = tc.get("function") or {}
                                            if fn.get("name"):
                                                buf["function"]["name"] = fn["name"]
                                            if fn.get("arguments"):
                                                buf["function"]["arguments"] += fn["arguments"]
                                        fr = choices[0].get("finish_reason")
                                        if fr:
                                            finish_reason = fr
                                elapsed = time.monotonic() - t0
                                joined_content = "".join(content_parts)
                                caller = _caller_var.get()

                                # Store usage for telemetry consumers (per-call,
                                # see _usage_var / _latency_var).
                                _usage_var.set(usage_data)
                                _latency_var.set(int(elapsed * 1000))
                                prompt_tokens = (usage_data or {}).get("prompt_tokens", 0)
                                completion_tokens = (usage_data or {}).get("completion_tokens", 0)
                                total_tokens = (usage_data or {}).get("total_tokens", 0)

                                logger.info(
                                    "LLM streaming response complete in %.1fs (rid=%s, %s, caller=%s, content=%d chars, reasoning=%s, finish=%s, tokens=%d/%d/%d p/c/t)",
                                    elapsed,
                                    current_request_id() or "-",
                                    _attribution_log_fields(),
                                    caller,
                                    len(joined_content),
                                    has_reasoning,
                                    finish_reason or "stop",
                                    prompt_tokens,
                                    completion_tokens,
                                    total_tokens,
                                )
                                if not joined_content.strip() and has_reasoning:
                                    # Expected for thinking models — reasoning is normal
                                    # stream data, not an error. The stream is still read
                                    # to completion; if the model genuinely produced no
                                    # answer the downstream empty-content handling applies
                                    # the proof-of-change thinking-downgrade retry.
                                    # Logged at INFO, not WARNING.
                                    logger.info(
                                        "LLM returned reasoning only (no content) for caller=%s; "
                                        "the empty-response handler will retry with progressively "
                                        "reduced thinking (ending in thinking-off).",
                                        caller,
                                    )
                                tool_calls = None
                                if tool_call_buffers:
                                    tool_calls = []
                                    for idx in sorted(tool_call_buffers.keys()):
                                        buf = tool_call_buffers[idx]
                                        try:
                                            buf["function"]["arguments"] = json.loads(
                                                buf["function"]["arguments"]
                                            )
                                        except (json.JSONDecodeError, ValueError):
                                            pass
                                        tool_calls.append(buf)
                                synthetic = {
                                    "choices": [
                                        {
                                            "message": {
                                                "content": joined_content,
                                                "tool_calls": tool_calls,
                                                "reasoning_content": "".join(reasoning_parts),
                                            },
                                            "finish_reason": finish_reason or "stop",
                                        }
                                    ]
                                }
                                return self._parse_response_content(synthetic)
                            if status == 429:
                                # Capture the body/headers for exhaustion logging,
                                # then RAISE so the `with stream / with Client /
                                # with sem` contexts unwind (releasing the
                                # concurrency slot and closing the stream) BEFORE
                                # the slow 429 backoff sleep, which is owned by the
                                # loop-level `except LLMRateLimitError` handler.
                                rl_log_body = response.text
                                rl_log_headers = response.headers
                                raise _rate_limit_error_from_response(
                                    body=rl_log_body,
                                    headers=rl_log_headers,
                                    attempt=attempt + 1,
                                )
                            if 500 <= status < 600:
                                hint = ""
                                if (
                                    "ollama.com" in self.base_url
                                    and "qwen3.5" in self.model.lower()
                                ):
                                    hint = " If using Ollama Cloud with qwen3.5, try passing think=False."
                                # Capture body/headers for the exhaustion log, then
                                # raise so the transient backoff runs in the outer
                                # ``except LLMTemporaryError`` handler — AFTER the
                                # stream/client/semaphore contexts unwind. Sleeping
                                # here would hold the process-global concurrency gate
                                # through the backoff, blocking unrelated calls even
                                # though no request is in flight (mirrors the 429 path).
                                srv_log_body = response.text
                                srv_log_headers = response.headers
                                raise LLMTemporaryError(
                                    f"LLM server error {status} after {attempt + 1} attempt(s): {response.text}.{hint}",
                                    status_code=status,
                                )
                            if 400 <= status < 500:
                                err_text = response.text
                                self._log_llm_server_error(
                                    status,
                                    response.text,
                                    response.headers,
                                    attempt + 1,
                                    reason="client error",
                                )
                                if status == 404 and (
                                    "not found" in err_text.lower() or "model" in err_text.lower()
                                ):
                                    raise LLMPermanentError(
                                        f"LLM model not found (404). API at {self.base_url} does not have model '{self.model}'. Original: {err_text}",
                                        status_code=status,
                                    )
                                if status == 401:
                                    auth_hint = (
                                        " Set OLLAMA_API_KEY (or LLM_OLLAMA_API_KEY) for Ollama Cloud."
                                        if not headers
                                        else " Check that the key is valid and not expired."
                                    )
                                    raise LLMPermanentError(
                                        f"LLM unauthorized (401): {err_text}.{auth_hint}",
                                        status_code=status,
                                    )
                                raise LLMPermanentError(
                                    f"LLM client error {status}: {err_text}", status_code=status
                                )
                            self._log_llm_server_error(
                                status,
                                response.text,
                                response.headers,
                                attempt + 1,
                                reason="unexpected status",
                            )
                            raise LLMPermanentError(
                                f"Unexpected LLM response status {status}: {response.text}",
                                status_code=status,
                            )
            except LLMPermanentError:
                raise
            except LLMTruncatedError as e:
                # Stamp the thinking level of the attempt that produced the
                # partial content so continuation requests resume at the SAME
                # level — a call downgraded mid-flight must not be silently
                # continued at the original (just-failed) thinking level.
                e.think_used = active_think
                raise
            except LLMRateLimitError as e:
                # The single owner of 429 backoff. Control reaches here only after
                # the `with sem / with Client / with stream` contexts have unwound,
                # so the (slow, possibly multi-minute) sleep holds no shared
                # resource — the headline fix.
                if rate_limit_attempt < rate_limit_max_retries:
                    _rate_limit_backoff_sleep(
                        rate_limit_attempt,
                        rate_limit_max_retries,
                        rate_limit_initial,
                        rate_limit_cap,
                        e.retry_after_seconds,
                    )
                    rate_limit_attempt += 1
                    continue
                if rl_log_body is not None:
                    self._log_llm_server_error(
                        429, rl_log_body, rl_log_headers, attempt + 1, reason="rate limited"
                    )
                raise e
            except _EmptyResponseSignal as sig:
                if schema_forced:
                    # One strike, no ladder, no retry loop: schema-forced
                    # decoding starved the content channel (the exact failure
                    # mode Strategy Lab's earlier decoder-level
                    # format=<json-schema> attempt hit on long code-emitting
                    # turns — see strategy_lab/agents/_response_schemas.py).
                    # Bail immediately with an explicit signal callers can
                    # catch/branch on, regardless of the thinking-downgrade
                    # kill switch state — falling through to the legacy
                    # "retry verbatim" branch below would resurrect exactly
                    # the retry-loop shape that was reverted.
                    fingerprint = sha256_fingerprint(
                        json.dumps(stream_payload, sort_keys=True, default=str)
                    )
                    logger.error(
                        "LLM schema-forced decoding starved the content channel: rid=%s %s "
                        "failure_class=semantic_exhaustion schema_forced=True attempts_used=%d "
                        "finish_reason=%s payload_fingerprint=%s",
                        current_request_id() or "-",
                        _attribution_log_fields(),
                        attempt + 1,
                        sig.finish_reason,
                        fingerprint,
                    )
                    raise LLMSemanticExhaustionError(
                        "Schema-forced structured decoding produced no assistant content; "
                        "caller should catch this and retry with schema=None (unconstrained "
                        "json_object mode)",
                        attempts_used=attempt + 1,
                        original_thinking_level=resolved_think,
                        retry_thinking_level=None,
                        content_bytes_seen=sig.content_len > 0,
                        payload_fingerprint=fingerprint,
                        finish_reason=sig.finish_reason,
                        schema_forced=True,
                    )
                if not _thinking_downgrade_enabled():
                    # Kill switch off: legacy behavior — empty responses retry
                    # verbatim on the transient schedule, then fail as a plain
                    # LLMTemporaryError. Handled inline because a raise here
                    # cannot be caught by the sibling LLMTemporaryError clause.
                    logger.warning(
                        "LLM returned empty response (rid=%s, %s, finish_reason=%s). Treating as transient error for retry.",
                        current_request_id() or "-",
                        _attribution_log_fields(),
                        sig.finish_reason,
                    )
                    last_error = LLMTemporaryError(
                        "Empty response from LLM; treating as transient for retry",
                    )
                    if _retry_transient_step(str(last_error)):
                        continue
                    raise last_error
                # Receipt state — only consumed by the semantic-exhaustion
                # receipt below, so only tracked on the downgrade path. The
                # reasoning diagnostics accumulate across rungs so the receipt
                # reflects the reasoning-heavy early attempts, not the empty
                # thinking-off rung that usually raises.
                any_content_bytes = any_content_bytes or sig.content_len > 0
                reasoning_len_seen = max(reasoning_len_seen, sig.reasoning_len)
                reasoning_has_json_seen = reasoning_has_json_seen or sig.reasoning_has_json
                new_think = (
                    _semantic_retry_think(self.model, active_think)
                    if semantic_attempt < max_semantic_retries
                    else None
                )
                if new_think is not None:
                    logger.warning(
                        "LLM produced no assistant content (rid=%s, %s, finish=%s, has_reasoning=%s, attempt %d); "
                        "proof-of-change retry with thinking %r -> %r",
                        current_request_id() or "-",
                        _attribution_log_fields(),
                        sig.finish_reason,
                        sig.has_reasoning,
                        attempt + 1,
                        active_think,
                        new_think,
                    )
                    semantic_attempt += 1
                    active_think = new_think
                    stream_payload = {**stream_payload, **_think_payload_fields(new_think)}
                    # Immediate retry: the changed payload IS the proof of
                    # change — backoff would not alter the outcome.
                    continue
                fingerprint = sha256_fingerprint(
                    json.dumps(stream_payload, sort_keys=True, default=str)
                )
                retry_level = active_think if semantic_attempt else None
                logger.error(
                    "LLM semantic exhaustion: rid=%s %s failure_class=semantic_exhaustion attempts_used=%d "
                    "original_thinking_level=%r retry_thinking_level=%r content_bytes_seen=%s "
                    "finish_reason=%s reasoning_len=%d reasoning_has_json=%s payload_fingerprint=%s",
                    current_request_id() or "-",
                    _attribution_log_fields(),
                    attempt + 1,
                    resolved_think,
                    retry_level,
                    any_content_bytes,
                    sig.finish_reason,
                    reasoning_len_seen,
                    reasoning_has_json_seen,
                    fingerprint,
                )
                # Raising inside this handler guarantees the sibling
                # `except LLMTemporaryError` clause can never catch it — the
                # call terminates here, with no fall-back into the transient loop.
                raise LLMSemanticExhaustionError(
                    "LLM produced no assistant content and no further proof-of-change retry is available",
                    attempts_used=attempt + 1,
                    original_thinking_level=resolved_think,
                    retry_thinking_level=retry_level,
                    content_bytes_seen=any_content_bytes,
                    payload_fingerprint=fingerprint,
                    finish_reason=sig.finish_reason,
                )
            except LLMTemporaryError as e:
                last_error = e
                if _retry_transient_step(str(e)):
                    continue
                # On exhaustion, emit the structured server-error log for a 5xx
                # (its body/headers were captured inside the stream context before
                # the gate was released); other transient errors carry their detail
                # in the raised exception message.
                if srv_log_body is not None:
                    self._log_llm_server_error(
                        getattr(e, "status_code", None) or 0,
                        srv_log_body,
                        srv_log_headers,
                        attempt + 1,
                        reason="server error",
                    )
                raise last_error
            except httpx.HTTPStatusError as e:
                resp = e.response
                status = resp.status_code if resp else None
                if resp is not None:
                    self._log_llm_server_error(
                        resp.status_code,
                        resp.text,
                        resp.headers,
                        attempt + 1,
                        reason="HTTPStatusError",
                    )
                if status == 429:
                    # This sibling except clause cannot funnel into the dedicated
                    # `except LLMRateLimitError` above (a raise here is not caught
                    # by a sibling clause of the same try), so apply the rate-limit
                    # schedule inline using the SAME shared counter. We are already
                    # outside the semaphore/stream contexts here.
                    if rate_limit_attempt < rate_limit_max_retries:
                        retry_after = (
                            _parse_retry_after_seconds(resp.headers)
                            if (resp is not None and _honor_retry_after_enabled())
                            else None
                        )
                        _rate_limit_backoff_sleep(
                            rate_limit_attempt,
                            rate_limit_max_retries,
                            rate_limit_initial,
                            rate_limit_cap,
                            retry_after,
                        )
                        rate_limit_attempt += 1
                        continue
                    body = resp.text if resp is not None else ""
                    headers = resp.headers if resp is not None else None
                    raise _rate_limit_error_from_response(
                        body=body,
                        headers=headers,
                        attempt=attempt + 1,
                        cause=e,
                    )
                if status and 500 <= status < 600:
                    last_error = LLMTemporaryError(str(e), status_code=status, cause=e)
                    if _retry_transient_step(f"server error {status}", kind="server error"):
                        continue
                    raise last_error
                if status and 400 <= status < 500:
                    raise LLMPermanentError(str(e), status_code=status or 0, cause=e)
                raise LLMPermanentError(str(e), status_code=status or 0, cause=e)
            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.WriteError,
                httpx.ReadError,
                httpx.ProxyError,
            ) as e:
                hint = ""
                if "name resolution" in str(e).lower() or "temporary failure" in str(e).lower():
                    hint = (
                        f" Cannot reach LLM at {self.base_url}. "
                        "If running in Docker, set LLM_BASE_URL to a reachable endpoint "
                        "(e.g. http://host.docker.internal:11434 for local Ollama, or ensure the container has DNS/outbound access)."
                    )
                elif isinstance(e, httpx.RemoteProtocolError):
                    hint = " (server closed connection; retrying with exponential backoff)"
                last_error = LLMTemporaryError(
                    f"LLM connection/transport error ({type(e).__name__}): {e}.{hint}",
                    cause=e,
                )
                if _retry_transient_step(type(e).__name__, kind="transport error"):
                    continue
                timeout_hint = ""
                if isinstance(e, httpx.ReadTimeout):
                    timeout_hint = (
                        f" Per-request timeout is {int(self.timeout)}s; increase LLM_TIMEOUT "
                        "(e.g. 900–1200) for slow cloud models or very long prompts."
                    )
                logger.error(
                    "LLM connection/timeout failed after all retries. rid=%s %s model=%s base_url=%s attempt=%s error=%s%s%s",
                    current_request_id() or "-",
                    _attribution_log_fields(),
                    self.model,
                    self.base_url,
                    attempt + 1,
                    type(e).__name__,
                    hint,
                    timeout_hint,
                )
                raise last_error
        # Unreachable: the `while True` loop above always returns or raises (every
        # status/exception branch ends in return/continue/raise, and both retry
        # budgets are finite). Kept as a defensive guard.
        if last_error:  # pragma: no cover - defensive, loop is exhaustive
            raise last_error
        raise LLMTemporaryError(  # pragma: no cover - defensive, loop is exhaustive
            "LLM request failed after all retries"
        )

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        think: "bool | str | None" = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run the model with JSON mode and return a decoded dict.

        ``objective`` (required) is stamped onto every log line and telemetry
        record for this call so it can be attributed in the logs.

        ``think=None`` (default) resolves to the platform default — the
        model's max registered thinking level when known; ``False`` disables;
        a string selects a specific level.

        ``schema`` (a JSON Schema dict or ``pydantic.BaseModel`` subclass) is
        accepted via ``**kwargs`` and forwarded to ``_complete_json_impl``,
        which requests provider-enforced schema-conformant decoding on the
        wire — see ``LLMClient.complete_json`` and
        ``OllamaLLMClient.supports_structured_output``.
        """
        if not objective or not objective.strip():
            # DbC precondition (see LLMClient): every call must declare a
            # non-empty objective so log/telemetry attribution is meaningful.
            raise ValueError("objective must be a non-empty string")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            return self._complete_json_impl(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                think=think,
                **kwargs,
            )

    def _complete_json_impl(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        think: "bool | str | None" = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        think = self._resolve_think(think, response_format="json")
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        rl_max_retries, rl_initial, rl_cap = self._rate_limit_retry_config()
        sem = get_llm_semaphore()
        caller, _attr = self._begin_call_state()
        logger.info(
            "LLM request: rid=%s agent=%s team=%s objective=%s caller=%s provider=ollama model=%s think=%s",
            current_request_id() or "-",
            _attr.agent_key or "-",
            _attr.team or "-",
            _attr.objective or "-",
            caller,
            self.model,
            think,
        )
        system_message = system_prompt or (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences. "
            "If you use a code block, put only the JSON object inside it with no surrounding text."
        )
        max_tokens = self._resolve_max_tokens(kwargs.pop("max_tokens", None))
        tools = kwargs.pop("tools", None)
        schema = kwargs.pop("schema", None)
        if tools and schema is not None:
            # Fails fast/synchronously — cheaper and less surprising than
            # silently dropping one of the two on an OpenAI-compatible
            # endpoint where tools and json_schema response_format are
            # mutually exclusive wire modes.
            raise ValueError(
                "complete_json: 'tools' and 'schema' are mutually exclusive on the "
                "Ollama wire protocol; pass only one"
            )
        payload: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
            **_think_payload_fields(think),
        }
        schema_forced = False
        if tools:
            payload["tools"] = tools
        elif schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "schema": _normalize_schema_for_wire(schema),
                    "strict": True,
                },
            }
            schema_forced = True
        else:
            payload["response_format"] = {"type": "json_object"}
        try:
            content = self._ollama_post(
                payload,
                max_retries,
                backoff_base,
                backoff_max,
                rl_max_retries,
                rl_initial,
                rl_cap,
                sem,
                resolved_think=think,
                schema_forced=schema_forced,
            )
            if not (
                content or ""
            ).strip():  # pragma: no cover - _ollama_post raises on empty content
                # Postcondition guard only: _parse_response_content never
                # returns empty-stripped content in either retry mode.
                self._record_telemetry(status="error", error_type="empty_response")
                raise LLMTemporaryError(
                    "Empty response from LLM after retries; try again or pass think=False if thinking is enabled."
                )
            result = self._extract_json(content)
            self._record_telemetry(status="success", prompt_text=prompt, response_text=content)
            return result
        except LLMSemanticExhaustionError:
            self._record_telemetry(status="error", error_type="semantic_exhaustion")
            raise
        except LLMTruncatedError as e:
            self._record_telemetry(status="truncated", error_type="truncated")
            return self._complete_json_with_continuation(
                initial_partial=e.partial_content,
                prompt=prompt,
                system_message=system_message,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=max_retries,
                backoff_base=backoff_base,
                backoff_max=backoff_max,
                rl_max_retries=rl_max_retries,
                rl_initial=rl_initial,
                rl_cap=rl_cap,
                sem=sem,
                use_think=e.think_used if e.think_used is not None else think,
            )
        except LLMJsonParseError:
            self._record_telemetry(
                status="error", error_type="json_parse", prompt_text=prompt, response_text=content
            )
            # If content starts with '{' but is unparseable, the server likely cut off the
            # response before the JSON was complete (finish_reason="stop" despite truncation).
            # Attempt continuation to recover the rest of the JSON.
            stripped = (content or "").strip()
            if stripped.startswith(("{", "[")):
                logger.warning(
                    "JSON parse failed on content starting with '%s'; treating as implicit truncation and attempting continuation.",
                    stripped[0],
                )
                return self._complete_json_with_continuation(
                    initial_partial=content,
                    prompt=prompt,
                    system_message=system_message,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=max_retries,
                    backoff_base=backoff_base,
                    backoff_max=backoff_max,
                    rl_max_retries=rl_max_retries,
                    rl_initial=rl_initial,
                    rl_cap=rl_cap,
                    sem=sem,
                    use_think=think,
                )
            raise

    def _merge_continuation(self, accumulated: str, next_chunk: str, min_overlap: int = 10) -> str:
        """Append next_chunk to accumulated, stripping overlap at boundary."""
        if not next_chunk:
            return accumulated
        if not accumulated:
            return next_chunk
        max_check = min(len(accumulated), len(next_chunk), 500)
        for overlap_len in range(max_check, min_overlap - 1, -1):
            if accumulated[-overlap_len:] == next_chunk[:overlap_len]:
                return accumulated + next_chunk[overlap_len:]
        return accumulated + next_chunk

    def _continuation_user_message(self, partial_content: str) -> str:
        """Prompt for the model to continue from where it left off."""
        last_chars = partial_content[-CONTINUATION_CONTEXT_CHARS:] if partial_content else ""
        last_escaped = last_chars.replace("\n", "\\n")
        return (
            f"Please continue exactly from where you left off. "
            f"Your previous response ended with: '{last_escaped}'. "
            f"Continue the response seamlessly without repeating what you already wrote."
        )

    def _complete_json_with_continuation(
        self,
        initial_partial: str,
        prompt: str,
        system_message: str,
        temperature: float,
        max_tokens: int,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        rl_max_retries: int,
        rl_initial: float,
        rl_cap: float,
        sem: threading.BoundedSemaphore,
        use_think: "bool | str",
    ) -> Dict[str, Any]:
        """On truncation: continue via multi-turn conversation, then parse JSON (same as SE team)."""
        accumulated = initial_partial
        for cycle in range(MAX_CONTINUATION_CYCLES):
            logger.info(
                "Continuation cycle %d/%d (accumulated %d chars)",
                cycle + 1,
                MAX_CONTINUATION_CYCLES,
                len(accumulated),
            )
            messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": accumulated},
                {"role": "user", "content": self._continuation_user_message(accumulated)},
            ]
            payload = {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                # Continuation never re-applies a caller's schema= constraint —
                # always falls back to plain json_object (see the schema_forced
                # bail-out in _ollama_post's _EmptyResponseSignal handler for
                # why: continuation itself is triggered by a long generation,
                # exactly the risk profile schema-forced decoding must avoid).
                "response_format": {"type": "json_object"},
                "messages": messages,
                **_think_payload_fields(use_think),
            }

            try:
                next_content = self._ollama_post(
                    payload,
                    max_retries,
                    backoff_base,
                    backoff_max,
                    rl_max_retries,
                    rl_initial,
                    rl_cap,
                    sem,
                    resolved_think=use_think,
                )
                accumulated = self._merge_continuation(accumulated, next_content)
                return self._extract_json(accumulated)
            except LLMTruncatedError as e2:
                accumulated = self._merge_continuation(accumulated, e2.partial_content)
        logger.warning(
            "Continuation exhausted after %d cycles (%d chars). Re-raising truncation.",
            MAX_CONTINUATION_CYCLES,
            len(accumulated),
        )
        raise LLMTruncatedError(
            f"Response still truncated after {MAX_CONTINUATION_CYCLES} continuation cycles",
            partial_content=accumulated,
            finish_reason="length",
        )

    def complete(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        """Return raw text from the model (no JSON mode). Pass tools for function/tool calling.

        ``objective`` (required) is stamped onto every log line and telemetry
        record for this call so it can be attributed in the logs.

        ``think=None`` (default) resolves to the platform default — the
        model's max registered thinking level when known; ``False`` disables;
        a string selects a specific level.
        """
        if not objective or not objective.strip():
            # DbC precondition (see LLMClient): every call must declare a
            # non-empty objective so log/telemetry attribution is meaningful.
            raise ValueError("objective must be a non-empty string")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            return self._complete_impl(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                system_prompt=system_prompt,
                tools=tools,
                think=think,
            )

    def _complete_impl(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        think = self._resolve_think(think)
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        rl_max_retries, rl_initial, rl_cap = self._rate_limit_retry_config()
        sem = get_llm_semaphore()
        caller, _attr = self._begin_call_state()
        logger.info(
            "LLM request (text): rid=%s agent=%s team=%s objective=%s caller=%s provider=ollama model=%s think=%s",
            current_request_id() or "-",
            _attr.agent_key or "-",
            _attr.team or "-",
            _attr.objective or "-",
            caller,
            self.model,
            think,
        )
        max_tokens = self._resolve_max_tokens(max_tokens)
        payload: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            **_think_payload_fields(think),
        }
        if system_prompt:
            payload["messages"] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        if tools:
            payload["tools"] = tools
        try:
            result = self._ollama_post(
                payload,
                max_retries,
                backoff_base,
                backoff_max,
                rl_max_retries,
                rl_initial,
                rl_cap,
                sem,
                resolved_think=think,
            )
            self._record_telemetry(status="success", prompt_text=prompt, response_text=result)
            return result
        except LLMSemanticExhaustionError:
            self._record_telemetry(status="error", error_type="semantic_exhaustion")
            raise
        except LLMTruncatedError as e:
            self._record_telemetry(status="truncated", error_type="truncated")
            return self._complete_text_with_continuation(
                initial_partial=e.partial_content,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=max_retries,
                backoff_base=backoff_base,
                backoff_max=backoff_max,
                rl_max_retries=rl_max_retries,
                rl_initial=rl_initial,
                rl_cap=rl_cap,
                sem=sem,
                use_think=e.think_used if e.think_used is not None else think,
            )

    def _complete_text_with_continuation(
        self,
        initial_partial: str,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        rl_max_retries: int,
        rl_initial: float,
        rl_cap: float,
        sem: threading.BoundedSemaphore,
        use_think: "bool | str",
    ) -> str:
        """On truncation: continue via multi-turn conversation, return merged text."""
        accumulated = initial_partial
        system_message = system_prompt or ""
        for cycle in range(MAX_CONTINUATION_CYCLES):
            logger.info(
                "Continuation cycle %d/%d (text, accumulated %d chars)",
                cycle + 1,
                MAX_CONTINUATION_CYCLES,
                len(accumulated),
            )
            messages: list = []
            if system_message:
                messages.append({"role": "system", "content": system_message})
            messages.append({"role": "user", "content": prompt})
            messages.append({"role": "assistant", "content": accumulated})
            messages.append(
                {"role": "user", "content": self._continuation_user_message(accumulated)}
            )
            payload = {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
                **_think_payload_fields(use_think),
            }
            try:
                next_content = self._ollama_post(
                    payload,
                    max_retries,
                    backoff_base,
                    backoff_max,
                    rl_max_retries,
                    rl_initial,
                    rl_cap,
                    sem,
                    resolved_think=use_think,
                )
                accumulated = self._merge_continuation(accumulated, next_content)
                return accumulated
            except LLMTruncatedError as e2:
                accumulated = self._merge_continuation(accumulated, e2.partial_content)
        logger.warning(
            "Continuation exhausted after %d cycles (text, %d chars). Re-raising truncation.",
            MAX_CONTINUATION_CYCLES,
            len(accumulated),
        )
        raise LLMTruncatedError(
            f"Response still truncated after {MAX_CONTINUATION_CYCLES} continuation cycles",
            partial_content=accumulated,
            finish_reason="length",
        )

    def chat(
        self,
        messages: list,
        *,
        objective: str,
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """One chat completion round, parameterized by ``response_format``.

        ``objective`` (required) is stamped onto every log line and telemetry
        record for this call so it can be attributed in the logs.

        See ``LLMClient.chat``. JSON-only differences from the text path are
        local to the two if-statements below: the wire payload includes
        ``response_format=json_object`` when no tools are present, and the
        assistant content is parsed via ``_extract_json`` instead of being
        returned raw. Tool-invocation envelopes are returned identically in
        both modes. When tools are present (so ``json_object`` cannot be forced
        on the wire) and the model emits prose instead of a tool call or JSON,
        one corrective follow-up asks it to either invoke a tool or emit JSON
        only. ``think=None`` (default) resolves to the platform default
        (max registered thinking level when known).
        """
        if not objective or not objective.strip():
            # DbC precondition (see LLMClient): every call must declare a
            # non-empty objective so log/telemetry attribution is meaningful.
            raise ValueError("objective must be a non-empty string")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            return self._chat_impl(
                messages,
                response_format=response_format,
                temperature=temperature,
                tools=tools,
                think=think,
                max_tokens=max_tokens,
                **kwargs,
            )

    def _chat_json_self_correct(
        self,
        *,
        messages: list,
        bad_content: str,
        tools: list,
        think: "bool | str",
        temperature: float,
        max_tokens: int,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        rl_max_retries: int,
        rl_initial: float,
        rl_cap: float,
        sem: threading.BoundedSemaphore,
        first_error: LLMJsonParseError,
    ) -> Any:
        """One corrective chat turn after a non-JSON/non-tool reply with tools present.

        Preconditions:
            - ``tools`` is a non-empty tool list (caller already gated on this);
              ``json_object`` cannot be forced on the wire alongside tools.
            - ``bad_content`` is the rejected assistant text from the prior turn.
        Postconditions:
            - Returns a parsed JSON ``dict``, or a ``{"__tool_calls__": [...]}``
              envelope when the correction invokes a tool.
            - On a second non-JSON reply, re-raises ``first_error`` (or the
              second parse error) after recording telemetry — never loops.
        """
        preview = (bad_content or "")[:500]
        corrective_messages = list(messages) + [
            {"role": "assistant", "content": bad_content},
            {
                "role": "user",
                "content": _CHAT_JSON_CORRECTIVE_USER.format(preview=preview or "(empty)"),
            },
        ]
        payload: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": corrective_messages,
            "tools": tools,
            **_think_payload_fields(think),
        }
        logger.info(
            "chat JSON self-correction: rid=%s %s model=%s (tools present; prior reply was prose)",
            current_request_id() or "-",
            _attribution_log_fields(),
            self.model,
        )
        try:
            content = self._ollama_post(
                payload,
                max_retries,
                backoff_base,
                backoff_max,
                rl_max_retries,
                rl_initial,
                rl_cap,
                sem,
                resolved_think=think,
            )
        except LLMSemanticExhaustionError:
            self._record_telemetry(status="error", error_type="semantic_exhaustion")
            raise
        stripped = (content or "").strip()
        if stripped.startswith("{") and "__tool_calls__" in stripped:
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "__tool_calls__" in parsed:
                    logger.info(
                        "chat JSON self-correction succeeded (tool call) after 1 retry (model=%s)",
                        self.model,
                    )
                    self._record_telemetry(status="success")
                    return parsed
            except json.JSONDecodeError:
                pass
        try:
            result = self._extract_json(content)
        except LLMJsonParseError as second_err:
            self._record_telemetry(status="error", error_type="json_parse")
            logger.warning(
                "chat JSON self-correction failed terminally (model=%s, preview=%r)",
                self.model,
                (content or "")[:500],
            )
            second_err.correction_attempts_used = 1
            first_error.correction_attempts_used = 1
            raise second_err from first_error
        logger.info(
            "chat JSON self-correction succeeded after 1 retry (model=%s)",
            self.model,
        )
        self._record_telemetry(status="success")
        return result

    def _chat_impl(
        self,
        messages: list,
        *,
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        if response_format not in ("json", "text"):
            raise ValueError(f"response_format must be 'json' or 'text', got {response_format!r}")
        think = self._resolve_think(think, response_format=response_format)
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        rl_max_retries, rl_initial, rl_cap = self._rate_limit_retry_config()
        sem = get_llm_semaphore()
        caller, _attr = self._begin_call_state()
        logger.info(
            "LLM request (chat): rid=%s agent=%s team=%s objective=%s caller=%s provider=ollama model=%s think=%s rf=%s",
            current_request_id() or "-",
            _attr.agent_key or "-",
            _attr.team or "-",
            _attr.objective or "-",
            caller,
            self.model,
            think,
            response_format,
        )
        max_tokens = self._resolve_max_tokens(max_tokens)
        payload: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
            **_think_payload_fields(think),
        }
        if tools:
            payload["tools"] = tools
        elif response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        try:
            content = self._ollama_post(
                payload,
                max_retries,
                backoff_base,
                backoff_max,
                rl_max_retries,
                rl_initial,
                rl_cap,
                sem,
                resolved_think=think,
            )
        except LLMSemanticExhaustionError:
            self._record_telemetry(status="error", error_type="semantic_exhaustion")
            raise
        stripped = (content or "").strip()
        if stripped.startswith("{") and "__tool_calls__" in stripped:
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "__tool_calls__" in parsed:
                    self._record_telemetry(status="success")
                    return parsed
            except json.JSONDecodeError:
                pass
        if response_format == "text":
            self._record_telemetry(status="success")
            return content
        # JSON mode: parse with the existing repair/continue fallbacks.
        try:
            result = self._extract_json(content)
            self._record_telemetry(status="success")
            return result
        except LLMJsonParseError as parse_err:
            self._record_telemetry(status="error", error_type="json_parse")
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict) and "__tool_calls__" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    pass
            # Tools and json_object are mutually exclusive on the OpenAI-compat
            # wire, so a prose reply here is common (not a truncated `{...`).
            # One corrective follow-up recovers most of these turns.
            if tools:
                return self._chat_json_self_correct(
                    messages=list(messages),
                    bad_content=content or "",
                    tools=tools,
                    think=think,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=max_retries,
                    backoff_base=backoff_base,
                    backoff_max=backoff_max,
                    rl_max_retries=rl_max_retries,
                    rl_initial=rl_initial,
                    rl_cap=rl_cap,
                    sem=sem,
                    first_error=parse_err,
                )
            raise
