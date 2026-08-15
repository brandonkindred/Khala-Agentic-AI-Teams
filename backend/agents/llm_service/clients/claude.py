"""Claude (Anthropic) LLM client for the central LLM service.

Implements the shared :class:`~llm_service.interface.LLMClient` contract on top of
the **official Anthropic Python SDK** (`anthropic`). Teams never construct this
directly — they call :func:`llm_service.get_client`, which builds and caches a
``ClaudeLLMClient`` when the resolved provider is ``claude``.

Design choices (see ``llm_service/README.md`` and the Anthropic API guidance):

* **Streaming + ``get_final_message()``** — every request streams and assembles
  the final message, so large ``max_tokens`` values never hit HTTP idle timeouts.
* **Adaptive thinking only** — thinking maps to ``{"type": "adaptive"}`` plus an
  optional ``output_config.effort`` level. ``temperature`` / ``top_p`` are never
  sent (the current Opus/Fable family rejects them with a 400).
* **JSON via instruction + shared parser** — JSON mode is done by instructing the
  model to emit a single JSON object and parsing with the shared
  :func:`llm_service.util.extract_json_from_response` (repair fallbacks included).
* **Unified errors** — Anthropic SDK exceptions are mapped onto the
  ``LLMRateLimitError`` / ``LLMTemporaryError`` / ``LLMPermanentError`` /
  ``LLMTruncatedError`` hierarchy so every team handles failures identically.

Invariants:
    - ``self.model`` is the Claude model id used for every call and telemetry record.
    - ``temperature`` / ``top_p`` are never forwarded to the Anthropic API.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

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
    LLMTemporaryError,
    LLMTruncatedError,
    record_complete_json_raw,
    reset_complete_json_observer_state,
)
from ..telemetry import record_llm_call
from ..util import extract_json_from_response

logger = logging.getLogger(__name__)

# Opus/Fable family supports up to 128K output tokens (streaming required). We
# default to a conservative cap and only go higher when explicitly requested.
CLAUDE_MAX_OUTPUT_TOKENS = 128_000
DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS = 32_768

# Effort levels accepted by ``output_config.effort`` on the current models.
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

_JSON_ONLY_INSTRUCTION = (
    "You are a strict JSON generator. Respond with a single valid JSON object only, "
    "no explanatory text, no Markdown, no code fences. If you must use a code block, "
    "put only the JSON object inside it with no surrounding text."
)


def _caller_tag() -> str:
    """Return ``module.function`` of the first caller outside llm_service.

    Mirrors the Ollama client's tag so telemetry ``caller_tag`` values are
    comparable across providers. Frame-walks with ``sys._getframe`` (no file
    I/O); degrades to ``"unknown"`` where unavailable. Never raises. ``sys._getframe``
    is a CPython-specific implementation detail, so this is best-effort telemetry
    tagging only — never required for correctness; the ``"unknown"`` fallback is an
    acceptable result on interpreters that do not expose it.
    """
    import sys

    getframe = getattr(sys, "_getframe", None)
    if getframe is None:  # pragma: no cover - non-CPython fallback
        return "unknown"
    try:
        frame = getframe(2)
    except ValueError:  # pragma: no cover - shallow stack
        return "unknown"
    while frame is not None:
        mod = frame.f_globals.get("__name__", "")
        if mod and "llm_service" not in mod:
            func = frame.f_code.co_name
            parts = mod.rsplit(".", 2)
            short = ".".join(parts[-2:]) if len(parts) > 1 else mod
            return f"{short}.{func}"
        frame = frame.f_back
    return "unknown"


def _import_anthropic():
    """Lazily import and return the ``anthropic`` module.

    Lazy so importing ``llm_service`` does not require ``anthropic`` unless the
    Claude provider is actually selected.

    Postconditions: returns the imported module; raises ``LLMPermanentError`` with
        an actionable message when the package is not installed.
    """
    try:
        import anthropic  # noqa: PLC0415

        return anthropic
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise LLMPermanentError(
            "The 'anthropic' package is required for LLM_PROVIDER=claude. "
            "Install it (pip install 'anthropic') — it is listed in "
            "backend/requirements.txt and backend/agents/requirements.txt."
        ) from e


def _to_anthropic_tools(tools: Optional[list]) -> Optional[list]:
    """Translate OpenAI-style tool defs to Anthropic ``input_schema`` form.

    Accepts either OpenAI shape (``{"type": "function", "function": {name,
    description, parameters}}``) or already-Anthropic shape (``{name, description,
    input_schema}``) and normalizes to the Anthropic shape.

    Preconditions: ``tools`` is ``None`` or a list of dicts.
    Postconditions: returns ``None`` when ``tools`` is falsy, else a list of
        Anthropic tool dicts. Never raises for a reasonably-shaped entry.
    """
    if not tools:
        return None
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "input_schema" in tool and "name" in tool:
            out.append(
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool["input_schema"],
                }
            )
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if fn and fn.get("name"):
            out.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
    return out or None


def _require_text(name: str, value: str) -> None:
    """Validate a required non-empty text argument.

    Preconditions: ``name`` is the argument's display name.
    Postconditions: returns ``None`` when ``value`` is a non-empty, non-whitespace
        string; raises ``ValueError`` naming ``name`` otherwise.
    """
    if not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _json_system(system_prompt: Optional[str], tools: Optional[list]) -> Optional[str]:
    """Build the system prompt for JSON mode.

    Preconditions: none.
    Postconditions: when ``tools`` are present, returns the stripped ``system_prompt``
        (or ``None``) WITHOUT the JSON-only instruction — it would fight the tool-use
        protocol; otherwise appends ``_JSON_ONLY_INSTRUCTION`` (alone when there is no
        system prompt). Never raises.
    """
    base = system_prompt.strip() if system_prompt and system_prompt.strip() else ""
    if tools:
        return base or None
    return f"{base}\n\n{_JSON_ONLY_INSTRUCTION}" if base else _JSON_ONLY_INSTRUCTION


class ClaudeLLMClient(LLMClient):
    """LLM client backed by the official Anthropic SDK.

    Invariants:
        - The Anthropic client is constructed lazily and reused for the life of
          this instance (the factory caches instances by model + key fingerprint).
    """

    def __init__(
        self,
        model: str = llm_config.DEFAULT_CLAUDE_MODEL,
        *,
        api_key: str = "",
        timeout: float = 3600.0,
        max_retries: int = 2,
        on_reasoning: Optional[Callable[[str], None]] = None,
        rate_limit_max_retries: Optional[int] = None,
    ) -> None:
        """Construct a Claude client.

        ``rate_limit_max_retries`` overrides the in-place 429 backoff retry budget
        (normally from ``LLM_RATE_LIMIT_MAX_RETRIES``); distinct from ``max_retries``,
        which configures the Anthropic SDK's own transport retries. The multi-provider
        failover path passes ``0`` so a 429 raises immediately and hands off to the
        next provider instead of sleeping minutes; ``None`` keeps the env schedule.

        Preconditions: ``model`` is a non-empty Claude model id (validated with an
            explicit ``ValueError`` so it survives ``python -O``); ``timeout`` > 0;
            ``max_retries`` >= 0; ``rate_limit_max_retries`` is ``None`` or ``>= 0``;
            ``on_reasoning`` is callable or ``None``. ``api_key`` may be empty here —
            it is validated on first use so the client can be constructed in
            environments that resolve the key lazily.
        Postconditions: a ready client; when ``on_reasoning`` is set, thinking-token
            deltas are streamed to it during each call (mirrors the Ollama client).
        """
        if not model or not model.strip():
            raise ValueError("model must be a non-empty string")
        self.model = model
        self.api_key = api_key or ""
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._rate_limit_max_retries_override = (
            max(0, int(rate_limit_max_retries)) if rate_limit_max_retries is not None else None
        )
        self.on_reasoning = on_reasoning
        self._client: Any = None
        # Cached reference to the imported ``anthropic`` module, populated alongside
        # ``_client`` so the hot ``_invoke`` path reads the SDK's exception classes
        # without re-importing on every call.
        self._anthropic_mod: Any = None
        # Guards lazy creation of the underlying Anthropic client: the factory
        # caches one ClaudeLLMClient per (model, key), so concurrent first-use
        # across threads must not each build a separate SDK client.
        self._client_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Client + helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return the cached Anthropic client, building it on first use.

        Postconditions: returns a ready ``anthropic.Anthropic``; raises
            ``LLMPermanentError`` when no API key is configured (clear auth error
            instead of an opaque SDK failure).
        """
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LLMPermanentError(
                "No Claude API key configured. Set it in the LLM Provider settings, "
                "or via LLM_CLAUDE_API_KEY / ANTHROPIC_API_KEY."
            )
        with self._client_lock:
            if self._client is None:
                anthropic = _import_anthropic()
                self._anthropic_mod = anthropic
                self._client = anthropic.Anthropic(
                    api_key=self.api_key, timeout=self.timeout, max_retries=self.max_retries
                )
        return self._client

    def get_max_context_tokens(self) -> int:
        """Return the model's input-token context window.

        Postconditions: returns ``LLM_CONTEXT_SIZE`` (if set) else the known
            Claude window, else a conservative default. Always ``>= 2048``.
        """
        return llm_config.resolve_claude_context_size(self.model)

    def _resolve_max_tokens(self, explicit: Optional[int]) -> int:
        """Resolve the output token cap. Explicit -> ``LLM_MAX_OUTPUT_TOKENS`` -> default.

        Preconditions: ``explicit`` is ``None`` or an int.
        Postconditions: returns an int in ``[1, CLAUDE_MAX_OUTPUT_TOKENS]``. A
            non-positive (``0`` / negative) explicit value is treated as "unset"
            and falls through to the env/default — never coerced to a 1-token cap.
        """
        if explicit is not None:
            try:
                explicit_int = int(explicit)
            except (TypeError, ValueError):
                explicit_int = 0
            if explicit_int > 0:
                return min(explicit_int, CLAUDE_MAX_OUTPUT_TOKENS)
        # Centralized resolver returns 0 for unset/invalid/non-positive (mirrors the
        # explicit path above), never a 1-token cap that truncates every call.
        env_int = llm_config.resolve_max_output_tokens()
        if env_int > 0:
            return min(env_int, CLAUDE_MAX_OUTPUT_TOKENS)
        return DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS

    def _thinking_kwargs(self, think: "bool | str | None") -> dict:
        """Return the ``thinking`` / ``output_config`` kwargs for ``think``.

        Resolves ``think`` against the global thinking default via
        :func:`llm_config.resolve_think_for_model`, then maps:
        ``False`` -> ``{}`` (omit thinking); ``True`` -> adaptive thinking; a level
        string -> adaptive thinking plus ``output_config.effort`` when the level is
        one Anthropic accepts.

        Postconditions: returns a dict mergeable into the create kwargs. Never sends
            ``temperature``/``top_p``. Never raises.
        """
        resolved = llm_config.resolve_think_for_model(self.model, think)
        if resolved is False:
            return {}
        kwargs: dict = {"thinking": {"type": "adaptive"}}
        if isinstance(resolved, str) and resolved in _EFFORT_LEVELS:
            kwargs["output_config"] = {"effort": resolved}
        return kwargs

    def _emit_reasoning(self, event: Any) -> None:
        """Forward one streamed thinking-token delta to ``on_reasoning`` (best-effort).

        Preconditions: ``event`` is a streamed Anthropic event object.
        Postconditions: calls ``self.on_reasoning(text)`` for a ``thinking_delta``
            event with non-empty text; a callback exception is logged, never raised.
            A no-op when ``on_reasoning`` is ``None`` or the event is not a thinking
            delta.
        """
        if self.on_reasoning is None:
            return
        if getattr(event, "type", None) != "content_block_delta":
            return
        delta = getattr(event, "delta", None)
        if getattr(delta, "type", None) != "thinking_delta":
            return
        text = getattr(delta, "thinking", "") or ""
        if not text:
            return
        try:
            self.on_reasoning(text)
        except Exception:  # noqa: BLE001 - reasoning sink must never break a call
            logger.debug("on_reasoning callback raised", exc_info=True)

    def _invoke(
        self,
        *,
        system: Optional[str],
        messages: list,
        tools: Optional[list],
        think: "bool | str | None",
        max_tokens: int,
    ) -> Any:
        """Stream one request and return the final Anthropic message.

        The network exchange runs under the process-global concurrency gate
        (``get_llm_semaphore``), released as soon as the stream context exits, so
        no slot is held during the outer 429 backoff sleep.

        Maps Anthropic SDK exceptions onto the unified LLM error hierarchy. The SDK
        already retries 429/5xx/connection errors (``max_retries``); a raised error
        means retries are exhausted.

        Postconditions: returns the assembled final message on success. Raises
            ``LLMRateLimitError`` (429), ``LLMTemporaryError`` (5xx / connection),
            or ``LLMPermanentError`` (other 4xx / any other Anthropic SDK error, or
            any non-Anthropic exception raised client-side) otherwise — no raw
            exception escapes the unified hierarchy.
        """
        client = self._get_client()
        # Normal path: _get_client populated _anthropic_mod alongside _client, so
        # reuse the cached reference rather than re-importing on every invocation.
        # Fall back to the (module-level cached) import only when _client was
        # injected directly without going through _get_client (e.g. in tests).
        anthropic = self._anthropic_mod or _import_anthropic()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        anthropic_tools = _to_anthropic_tools(tools)
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        kwargs.update(self._thinking_kwargs(think))
        try:
            # Hold the process-global concurrency gate for the network exchange
            # only. It is released the moment this stream context exits — on both
            # the normal return and any exception — so the multi-minute 429 backoff
            # sleep in _invoke_with_rate_limit_retry (which runs OUTSIDE _invoke)
            # never waits while holding a slot. This is the single global cap the
            # Ollama client also acquires, so concurrent review workers can never
            # exceed LLM_MAX_CONCURRENCY in-flight requests regardless of provider.
            with get_llm_semaphore():
                with client.messages.stream(**kwargs) as stream:
                    if self.on_reasoning is not None:
                        # Forward thinking-token deltas to the per-caller sink while
                        # the stream is consumed; get_final_message() still returns
                        # the fully assembled message afterward.
                        for event in stream:
                            self._emit_reasoning(event)
                    return stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise LLMRateLimitError(
                f"Claude rate limited (429): {e}",
                status_code=429,
                cause=e,
                retry_after_seconds=_retry_after_seconds(e)
                if _honor_retry_after_enabled()
                else None,
            ) from e
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None)
            # Defensive fallback: the real SDK raises RateLimitError (caught above)
            # for a 429, so this branch only fires for a *base* APIStatusError that
            # carries status 429 without being the RateLimitError subclass. Keep it
            # so such a 429 still maps to the rate-limit policy rather than a
            # permanent error (covered by test_plain_apistatuserror_429_maps_rate_limit).
            if status == 429:
                raise LLMRateLimitError(
                    f"Claude rate limited (429): {e}",
                    status_code=429,
                    cause=e,
                    retry_after_seconds=_retry_after_seconds(e)
                    if _honor_retry_after_enabled()
                    else None,
                ) from e
            if status is not None and 500 <= status < 600:
                raise LLMTemporaryError(
                    f"Claude server error {status}: {e}", status_code=status, cause=e
                ) from e
            raise LLMPermanentError(
                f"Claude client error {status}: {e}", status_code=status or 0, cause=e
            ) from e
        except anthropic.APIConnectionError as e:
            raise LLMTemporaryError(f"Claude connection/transport error: {e}", cause=e) from e
        except anthropic.AnthropicError as e:
            # Catch-all for any other SDK error (bare APIError, request-construction
            # / stream-parsing failures) so callers only ever see the unified
            # hierarchy. Unknown faults are treated as permanent (not blindly retried).
            raise LLMPermanentError(f"Claude SDK error: {e}", cause=e) from e
        except Exception as e:  # noqa: BLE001 - even a non-Anthropic error maps to the hierarchy
            # e.g. a TypeError from a thinking/output_config kwarg an older SDK
            # rejects client-side, before any HTTP call. Log with a stack trace so a
            # real bug in our request-building code is diagnosable rather than
            # masquerading as an opaque provider failure; never let it escape raw.
            logger.exception("Unexpected non-Anthropic error during Claude call")
            raise LLMPermanentError(f"Claude call failed: {e}", cause=e) from e

    def _invoke_with_rate_limit_retry(
        self,
        *,
        system: Optional[str],
        messages: list,
        tools: Optional[list],
        think: "bool | str | None",
        max_tokens: int,
    ) -> Any:
        """Call :meth:`_invoke`, retrying 429s on the shared slow rate-limit schedule.

        The Anthropic SDK's built-in ``max_retries`` already absorbs transient
        429/5xx blips on a fast schedule; a 429 that survives it means the
        account/budget is rate-limited and will not clear in seconds. So — mirroring
        the Ollama client — the call is retried on the dedicated
        ``LLM_RATE_LIMIT_*`` schedule (default first wait 30s, doubling to a 120s
        cap), honoring a parsed ``Retry-After`` when present and not disabled via
        ``LLM_RATE_LIMIT_HONOR_RETRY_AFTER``. The sleep happens here,
        above the HTTP stream context in :meth:`_invoke`, so no connection or shared
        resource is held while waiting.

        Preconditions: same as :meth:`_invoke`.
        Postconditions: returns the assembled final message on success. Only
            ``LLMRateLimitError`` is retried; after the configured number of
            rate-limit retries is exhausted it is re-raised. All other ``LLM*``
            errors propagate immediately.
        """
        max_retries, initial, cap = parse_rate_limit_retry_config()
        if self._rate_limit_max_retries_override is not None:
            max_retries = self._rate_limit_max_retries_override
        rate_limit_attempt = 0
        while True:
            try:
                return self._invoke(
                    system=system,
                    messages=messages,
                    tools=tools,
                    think=think,
                    max_tokens=max_tokens,
                )
            except LLMRateLimitError as e:
                if rate_limit_attempt >= max_retries:
                    raise
                rate_limit_backoff_sleep(
                    rate_limit_attempt,
                    max_retries,
                    initial,
                    cap,
                    e.retry_after_seconds,
                    provider="Claude",
                    request_id=current_request_id() or "-",
                )
                rate_limit_attempt += 1

    def _content_from_message(self, message: Any) -> tuple[str, Any]:
        """Reduce a final message to ``(kind, value)``.

        Returns ``("tools", envelope)`` when the model invoked tools (``envelope``
        is ``{"__tool_calls__": [...]}``, plus ``"__thinking_blocks__": [...]`` when
        the turn carried signed ``thinking``/``redacted_thinking`` blocks so they can
        be replayed unchanged on the next request), else ``("text", text)`` with the
        joined text blocks. Maps ``stop_reason`` edge cases onto the unified hierarchy.

        Postconditions: raises ``LLMPermanentError`` on a ``refusal`` stop reason
            and ``LLMTruncatedError`` on a ``max_tokens`` or ``pause_turn`` stop
            reason — all are checked BEFORE returning a tool envelope, so a tool
            call truncated at
            the token cap (possibly incomplete arguments) surfaces as a truncation
            error rather than a "successful" tool invocation. A ``"text"`` value
            may be empty (the caller decides whether that is an error).
        """
        blocks = list(getattr(message, "content", None) or [])
        tool_calls = []
        text_parts: list[str] = []
        # Signed thinking blocks from a tool-use turn must be replayed unchanged on
        # the next request (Anthropic 400s otherwise under extended thinking). We
        # capture them here, in Anthropic *input* shape, and carry them through the
        # tool-call envelope so the caller (tool loop) can echo them back verbatim.
        thinking_blocks: list[dict] = []
        for block in blocks:
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                tool_calls.append(
                    {
                        "id": getattr(block, "id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(block, "name", ""),
                            "arguments": getattr(block, "input", {}) or {},
                        },
                    }
                )
            elif btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "thinking":
                tb: dict = {"type": "thinking", "thinking": getattr(block, "thinking", "") or ""}
                signature = getattr(block, "signature", None)
                # Preserve the opaque signature verbatim — it is what Anthropic
                # validates on replay; never regenerate or drop it when present.
                if signature is not None:
                    tb["signature"] = signature
                thinking_blocks.append(tb)
            elif btype == "redacted_thinking":
                thinking_blocks.append(
                    {"type": "redacted_thinking", "data": getattr(block, "data", "") or ""}
                )

        text = "".join(text_parts)
        stop_reason = getattr(message, "stop_reason", None)
        # Check terminal stop reasons before the tool branch: a tool_use block
        # under stop_reason=max_tokens is a truncated (possibly invalid) call, and
        # must not be reported as a complete invocation (mirrors the Ollama path,
        # which raises on finish_reason=length regardless of content).
        if stop_reason == "refusal":
            raise LLMPermanentError(
                "Claude refused the request (stop_reason=refusal). Do not retry the same prompt."
            )
        if stop_reason == "max_tokens":
            # Distinguish "ran out of budget before emitting any output" (commonly
            # thinking-token exhaustion) from a genuinely partial response, so the
            # empty-partial_content case is diagnosable rather than a silent ''.
            detail = (
                "no output produced (likely thinking-token exhaustion); raise max_tokens"
                if not text.strip()
                else "response is partial"
            )
            raise LLMTruncatedError(
                f"Claude response truncated due to token limit (stop_reason=max_tokens): {detail}",
                partial_content=text,
                finish_reason="max_tokens",
            )
        if stop_reason == "pause_turn":
            # Anthropic paused a long turn and expects the caller to resend to
            # resume. We do not support resume, so surface it as a truncation rather
            # than silently returning the partial text/tool envelope as if complete.
            raise LLMTruncatedError(
                "Claude turn paused (stop_reason=pause_turn); resume is not supported, "
                "so the response is incomplete. Lower the work per turn or raise max_tokens.",
                partial_content=text,
                finish_reason="pause_turn",
            )
        if tool_calls:
            logger.info("Claude returned %d tool call(s)", len(tool_calls))
            envelope: dict = {"__tool_calls__": tool_calls}
            if thinking_blocks:
                envelope["__thinking_blocks__"] = thinking_blocks
            return "tools", envelope
        return "text", text

    def _record(
        self,
        *,
        status: str,
        message: Any = None,
        error_type: Optional[str] = None,
        latency_ms: int = 0,
        caller: str = "",
        prompt_text: Optional[str] = None,
        response_text: Optional[str] = None,
    ) -> None:
        """Record one LLM call to telemetry, sourcing attribution from contextvars.

        Preconditions: ``status`` is a telemetry status string; ``message`` is the
            final Anthropic message or ``None``.
        Postconditions: emits one ``record_llm_call`` row (token counts read from
            ``message.usage``); any telemetry failure is swallowed so it never breaks
            the LLM call. Never raises.
        """
        usage = getattr(message, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        attr = current_attribution()
        try:
            record_llm_call(
                team=attr.team,
                agent_key=attr.agent_key,
                model=self.model,
                caller_tag=caller,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                latency_ms=latency_ms,
                status=status,
                error_type=error_type,
                job_id=attr.job_id or None,
                objective=attr.objective,
                request_id=current_request_id(),
                prompt_text=prompt_text,
                response_text=response_text,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break a call
            logger.debug("Failed to record Claude LLM telemetry", exc_info=True)

    def _log_request(self, *, caller: str, think: "bool | str | None", kind: str) -> None:
        """Log one structured 'LLM request' line for this call.

        Preconditions: ``kind`` is a short request-kind tag (e.g. ``"json"``).
        Postconditions: emits one info log line with attribution + model + resolved
            thinking level; no secrets are logged. Never raises.
        """
        attr = current_attribution()
        logger.info(
            "LLM request (%s): rid=%s agent=%s team=%s objective=%s caller=%s provider=claude model=%s think=%s",
            kind,
            current_request_id() or "-",
            attr.agent_key or "-",
            attr.team or "-",
            attr.objective or "-",
            caller,
            self.model,
            llm_config.resolve_think_for_model(self.model, think),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run the model in JSON mode and return a decoded dict.

        ``temperature`` is accepted for interface compatibility but never sent to
        the Anthropic API.

        Preconditions: ``objective`` and ``prompt`` are non-empty strings.
        Postconditions: returns a parsed ``dict`` — either the JSON object the model
            produced (via the shared repair-tolerant parser) or, on tool use, the
            ``{"__tool_calls__": [...]}`` envelope. Raises ``LLMJsonParseError`` when
            the text is not parseable JSON, or the unified ``LLM*`` errors on
            transport/refusal/truncation. A telemetry record is emitted for the call.
        """
        _require_text("objective", objective)
        _require_text("prompt", prompt)
        reset_complete_json_observer_state()
        think = llm_config.resolve_think_for_model(self.model, think, response_format="json")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            caller = _caller_tag()
            self._log_request(caller=caller, think=think, kind="json")
            system = _json_system(system_prompt, tools)
            max_tokens = self._resolve_max_tokens(kwargs.pop("max_tokens", None))
            t0 = time.monotonic()
            message = self._invoke_with_rate_limit_retry(
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=tools,
                think=think,
                max_tokens=max_tokens,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            kind, value = self._content_from_message(message)
            if kind == "tools":
                self._record(
                    status="success", message=message, latency_ms=latency_ms, caller=caller
                )
                return value
            record_complete_json_raw(value)
            try:
                result = extract_json_from_response(value)
            except LLMJsonParseError:
                self._record(
                    status="error",
                    message=message,
                    error_type="json_parse",
                    latency_ms=latency_ms,
                    caller=caller,
                    prompt_text=prompt,
                    response_text=value,
                )
                raise
            self._record(
                status="success",
                message=message,
                latency_ms=latency_ms,
                caller=caller,
                prompt_text=prompt,
                response_text=value,
            )
            return result

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
        """Run the model and return raw text (no JSON forcing).

        ``temperature`` is accepted for interface compatibility but never sent. A
        tool invocation is returned as a JSON string of the ``__tool_calls__``
        envelope (matching the Ollama client).

        Preconditions: ``objective`` and ``prompt`` are non-empty strings.
        Postconditions: returns the model's text, or a JSON string of the
            ``{"__tool_calls__": [...]}`` envelope on tool use. Raises the unified
            ``LLM*`` errors on transport/refusal/truncation. A telemetry record is
            emitted for the call.
        """
        _require_text("objective", objective)
        _require_text("prompt", prompt)
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            caller = _caller_tag()
            self._log_request(caller=caller, think=think, kind="text")
            resolved_max = self._resolve_max_tokens(max_tokens)
            t0 = time.monotonic()
            message = self._invoke_with_rate_limit_retry(
                system=system_prompt or None,
                messages=[{"role": "user", "content": prompt}],
                tools=tools,
                think=think,
                max_tokens=resolved_max,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            kind, value = self._content_from_message(message)
            self._record(
                status="success",
                message=message,
                latency_ms=latency_ms,
                caller=caller,
                prompt_text=prompt,
                response_text=value if kind == "text" else None,
            )
            if kind == "tools":
                return json.dumps(value)
            return value

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

        Returns ``{"__tool_calls__": [...]}`` on tool invocation, a parsed dict for
        ``response_format="json"``, or raw text for ``response_format="text"``.

        Preconditions: ``objective`` is a non-empty string; ``response_format`` is
            ``"json"`` or ``"text"``.
        Postconditions: returns the ``{"__tool_calls__": [...]}`` envelope on tool
            use; otherwise a parsed ``dict`` (json mode) or ``str`` (text mode).
            OpenAI-style ``role:"tool"`` results and assistant ``tool_calls`` in
            ``messages`` are translated to Anthropic blocks (orphan tool results
            with no matching tool_use are dropped); signed ``thinking`` blocks carried
            on a replayed assistant tool-use turn are re-emitted unchanged so
            extended-thinking tool loops do not 400. Raises ``LLMPermanentError``
            when ``messages`` yields no user/assistant turn, ``LLMJsonParseError``
            (json mode, unparseable), or the unified ``LLM*`` errors. A telemetry
            record is emitted for the call.
        """
        _require_text("objective", objective)
        if response_format not in ("json", "text"):
            raise ValueError(f"response_format must be 'json' or 'text', got {response_format!r}")
        think = llm_config.resolve_think_for_model(
            self.model, think, response_format=response_format
        )
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            caller = _caller_tag()
            self._log_request(caller=caller, think=think, kind=f"chat:{response_format}")
            system, anthropic_messages = _to_anthropic_messages(messages)
            if not anthropic_messages:
                # System-only (or otherwise empty) input has nothing to send;
                # surface a clear error instead of an opaque Anthropic 400.
                raise LLMPermanentError(
                    "Claude chat requires at least one user/assistant message; got none after translation."
                )
            if response_format == "json":
                system = _json_system(system, tools)
            resolved_max = self._resolve_max_tokens(max_tokens)
            t0 = time.monotonic()
            message = self._invoke_with_rate_limit_retry(
                system=system or None,
                messages=anthropic_messages,
                tools=tools,
                think=think,
                max_tokens=resolved_max,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            kind, value = self._content_from_message(message)
            if kind == "tools":
                self._record(
                    status="success", message=message, latency_ms=latency_ms, caller=caller
                )
                return value
            if response_format == "text":
                self._record(
                    status="success", message=message, latency_ms=latency_ms, caller=caller
                )
                return value
            try:
                result = extract_json_from_response(value)
            except LLMJsonParseError:
                self._record(
                    status="error",
                    message=message,
                    error_type="json_parse",
                    latency_ms=latency_ms,
                    caller=caller,
                    prompt_text=json.dumps(messages, default=str),
                    response_text=value,
                )
                raise
            self._record(status="success", message=message, latency_ms=latency_ms, caller=caller)
            return result


def _honor_retry_after_enabled() -> bool:
    """Whether a 429 ``Retry-After`` should be honored (default: on).

    Mirrors the Ollama client's gate so the ``LLM_RATE_LIMIT_HONOR_RETRY_AFTER``
    kill-switch applies uniformly across providers — an operator who disables
    trusting provider ``Retry-After`` headers gets that behavior for Claude too.

    Preconditions: none.
    Postconditions: returns ``False`` only for an explicit ``"false"``/``"0"``/
        ``"no"`` (case-insensitive) ``LLM_RATE_LIMIT_HONOR_RETRY_AFTER``; unset or
        any other value means enabled. Never raises.
    """
    return llm_config.env_flag_enabled(llm_config.ENV_LLM_RATE_LIMIT_HONOR_RETRY_AFTER)


def _retry_after_seconds(error: Any) -> Optional[float]:
    """Extract an integer-seconds ``retry-after`` from an Anthropic error.

    Postconditions: returns a positive float or ``None``; never raises. Whether a
        parsed value is actually honored is gated by :func:`_honor_retry_after_enabled`
        at the call site (mirrors the Ollama client).
    """
    resp = getattr(error, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _to_anthropic_messages(messages: list) -> tuple[str, list]:
    """Translate an OpenAI-style chat ``messages`` list to Anthropic shape.

    Anthropic carries the system prompt as a top-level parameter (not a
    ``role:"system"`` message) and represents tool use with content blocks, not
    OpenAI's ``assistant.tool_calls`` + ``role:"tool"`` messages. This translator
    is what makes :func:`llm_service.tool_loop.complete_json_with_tool_loop` work
    under the Claude provider:

    - ``role:"system"`` entries are concatenated into the returned system text.
    - ``role:"assistant"`` with ``tool_calls`` becomes an assistant turn whose
      content is ``tool_use`` blocks (plus any leading text); string ``arguments``
      are parsed to a dict (Anthropic requires an object ``input``). Any signed
      ``thinking``/``redacted_thinking`` blocks carried on the message under a
      ``thinking_blocks`` key are re-emitted unchanged and FIRST (before text and
      ``tool_use``), as Anthropic requires under extended thinking.
    - ``role:"tool"`` results become ``tool_result`` blocks; consecutive tool
      results are coalesced into a single user turn (Anthropic groups all results
      for one assistant turn in one user message).
    - plain ``user``/``assistant`` string turns pass through.

    Preconditions: ``messages`` is a list of ``{role, content, ...}`` dicts that
        includes at least one ``user``/``assistant`` turn with content (a
        system-only conversation cannot be sent to Anthropic — the caller, e.g.
        :meth:`ClaudeLLMClient.chat`, surfaces a clear error for that case). The
        caller must also keep tool results immediately after the assistant turn
        that requested them: Anthropic rejects an intervening ``user`` turn between
        an assistant ``tool_use`` and its ``tool_result`` blocks. Pending tool
        results are flushed here on the next non-tool message, so an intervening
        user turn would split them into a separate user message.
        :func:`llm_service.tool_loop.complete_json_with_tool_loop` guarantees this
        ordering; this translator does not re-order to enforce it. It assumes the
        caller has already ordered messages correctly (tool results immediately
        after their assistant turn) and neither re-orders nor rejects out-of-order
        tool results — it only flushes pending results on the next non-tool message,
        so a mis-ordered list is faithfully (and possibly invalidly) translated.
    Postconditions: returns ``(system_text, anthropic_messages)`` where every
        emitted entry has role ``user``/``assistant`` and Anthropic-valid
        (non-empty) content, and every emitted ``tool_result`` has a matching
        ``tool_use`` earlier in the list (orphans are dropped, logged at debug, so
        Anthropic never sees a dangling ``tool_result``). Never raises for a
        reasonably-shaped list.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    pending_tool_results: list[dict] = []
    emitted_tool_use_ids: set[str] = set()

    def _flush_tool_results() -> None:
        if not pending_tool_results:
            return
        # Only emit a tool_result whose tool_use was actually sent; an orphan
        # (its owning assistant turn was dropped, or ids never matched) would make
        # Anthropic reject the whole request with "tool_result without tool_use".
        valid = [r for r in pending_tool_results if r["tool_use_id"] in emitted_tool_use_ids]
        dropped = len(pending_tool_results) - len(valid)
        if dropped:
            logger.debug(
                "Dropping %d orphan tool_result block(s) with no matching tool_use", dropped
            )
        if valid:
            out.append({"role": "user", "content": valid})
        pending_tool_results.clear()

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "tool":
            # OpenAI tool result -> Anthropic tool_result block (coalesced).
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(msg.get("tool_call_id") or ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }
            )
            continue
        # Any non-tool message ends the current run of tool results.
        _flush_tool_results()
        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                blocks: list[dict] = []
                # Anthropic requires the signed thinking/redacted_thinking blocks from
                # a tool-use turn to be replayed unchanged and FIRST (ahead of text and
                # tool_use), or an extended-thinking tool loop 400s on the next request
                # ("thinking blocks are required / signature mismatch"). Emit them
                # verbatim; skip anything that is not a well-formed thinking block.
                for tb in msg.get("thinking_blocks") or []:
                    if isinstance(tb, dict) and tb.get("type") in ("thinking", "redacted_thinking"):
                        blocks.append(tb)
                if isinstance(content, str) and content.strip():
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            args = {}
                    tool_use_id = str(tc.get("id") or "")
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": fn.get("name") or "",
                            "input": args if isinstance(args, dict) else {},
                        }
                    )
                    # Only track non-empty ids: an empty id would let an orphan
                    # tool_result (also empty id) false-match and reach Anthropic.
                    if tool_use_id:
                        emitted_tool_use_ids.add(tool_use_id)
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
                continue
            # Plain assistant turn: skip empty/whitespace/None content — Anthropic
            # rejects an empty assistant text block with a 400.
            if isinstance(content, str):
                if content.strip():
                    out.append({"role": "assistant", "content": content})
            elif content:
                out.append({"role": "assistant", "content": content})
            continue
        if role == "user":
            # Mirror the assistant branch: skip empty/whitespace/None content —
            # Anthropic rejects an empty content block with a 400.
            if isinstance(content, str):
                if content.strip():
                    out.append({"role": "user", "content": content})
            elif content:
                out.append({"role": "user", "content": content})
    _flush_tool_results()
    return "\n\n".join(system_parts), out
