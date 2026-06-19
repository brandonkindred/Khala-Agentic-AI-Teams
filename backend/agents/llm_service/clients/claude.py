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

import logging
import os
import time
from typing import Any, Dict, Optional

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
from ..interface import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
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
    I/O); degrades to ``"unknown"`` where unavailable. Never raises.
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
        timeout: float = 900.0,
        max_retries: int = 2,
    ) -> None:
        """Construct a Claude client.

        Preconditions: ``model`` is a non-empty Claude model id; ``timeout`` > 0;
            ``max_retries`` >= 0. ``api_key`` may be empty here — it is validated on
            first use so the client can be constructed in environments that resolve
            the key lazily.
        """
        assert model, "model must be non-empty"
        self.model = model
        self.api_key = api_key or ""
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._client: Any = None

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
        anthropic = _import_anthropic()
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
        """Resolve the output token cap. Explicit -> ``LLM_MAX_TOKENS`` -> default.

        Postconditions: returns an int in ``[1, CLAUDE_MAX_OUTPUT_TOKENS]``.
        """
        if explicit is not None:
            try:
                return max(1, min(int(explicit), CLAUDE_MAX_OUTPUT_TOKENS))
            except (TypeError, ValueError):
                pass
        env = os.environ.get(llm_config.ENV_LLM_MAX_TOKENS)
        if env:
            try:
                return max(1, min(int(env), CLAUDE_MAX_OUTPUT_TOKENS))
            except ValueError:
                pass
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

        Maps Anthropic SDK exceptions onto the unified LLM error hierarchy. The SDK
        already retries 429/5xx/connection errors (``max_retries``); a raised error
        means retries are exhausted.

        Postconditions: returns the assembled final message on success. Raises
            ``LLMRateLimitError`` (429), ``LLMTemporaryError`` (5xx / connection),
            or ``LLMPermanentError`` (other 4xx / unexpected) otherwise.
        """
        anthropic = _import_anthropic()
        client = self._get_client()
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
            with client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise LLMRateLimitError(
                f"Claude rate limited (429): {e}",
                status_code=429,
                cause=e,
                retry_after_seconds=_retry_after_seconds(e),
            ) from e
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status == 429:
                raise LLMRateLimitError(
                    f"Claude rate limited (429): {e}",
                    status_code=429,
                    cause=e,
                    retry_after_seconds=_retry_after_seconds(e),
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

    def _content_from_message(self, message: Any) -> tuple[str, Any]:
        """Reduce a final message to ``(kind, value)``.

        Returns ``("tools", envelope)`` when the model invoked tools (``envelope``
        is ``{"__tool_calls__": [...]}``), else ``("text", text)`` with the joined
        text blocks. Maps ``stop_reason`` edge cases onto the unified hierarchy.

        Postconditions: raises ``LLMPermanentError`` on a ``refusal`` stop reason,
            ``LLMTruncatedError`` on ``max_tokens`` with partial text. A ``"text"``
            value may be empty (the caller decides whether that is an error).
        """
        blocks = list(getattr(message, "content", None) or [])
        tool_calls = []
        text_parts: list[str] = []
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

        stop_reason = getattr(message, "stop_reason", None)
        if tool_calls:
            logger.info("Claude returned %d tool call(s)", len(tool_calls))
            return "tools", {"__tool_calls__": tool_calls}
        if stop_reason == "refusal":
            raise LLMPermanentError(
                "Claude refused the request (stop_reason=refusal). Do not retry the same prompt."
            )
        text = "".join(text_parts)
        if stop_reason == "max_tokens" and text.strip():
            raise LLMTruncatedError(
                "Claude response truncated due to token limit (stop_reason=max_tokens)",
                partial_content=text,
                finish_reason="max_tokens",
            )
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
        """Record one LLM call to telemetry, sourcing attribution from contextvars."""
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
        the Anthropic API. Tool invocations return ``{"__tool_calls__": [...]}``.

        Preconditions: ``objective`` is a non-empty string.
        """
        if not objective or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            caller = _caller_tag()
            self._log_request(caller=caller, think=think, kind="json")
            system = (
                f"{system_prompt.strip()}\n\n{_JSON_ONLY_INSTRUCTION}"
                if system_prompt and system_prompt.strip()
                else _JSON_ONLY_INSTRUCTION
            )
            max_tokens = self._resolve_max_tokens(kwargs.pop("max_tokens", None))
            t0 = time.monotonic()
            message = self._invoke(
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

        Preconditions: ``objective`` is a non-empty string.
        """
        if not objective or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            caller = _caller_tag()
            self._log_request(caller=caller, think=think, kind="text")
            resolved_max = self._resolve_max_tokens(max_tokens)
            t0 = time.monotonic()
            message = self._invoke(
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
                import json  # noqa: PLC0415

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
        """
        if not objective or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        if response_format not in ("json", "text"):
            raise ValueError(f"response_format must be 'json' or 'text', got {response_format!r}")
        team = current_attribution().team or _caller_team()
        with bind_request_id(new_request_id()), llm_attribution(objective=objective, team=team):
            caller = _caller_tag()
            self._log_request(caller=caller, think=think, kind=f"chat:{response_format}")
            system, anthropic_messages = _split_system(messages)
            if response_format == "json" and not tools:
                system = (
                    f"{system.strip()}\n\n{_JSON_ONLY_INSTRUCTION}"
                    if system and system.strip()
                    else _JSON_ONLY_INSTRUCTION
                )
            resolved_max = self._resolve_max_tokens(max_tokens)
            t0 = time.monotonic()
            message = self._invoke(
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
                )
                raise
            self._record(status="success", message=message, latency_ms=latency_ms, caller=caller)
            return result


def _retry_after_seconds(error: Any) -> Optional[float]:
    """Extract an integer-seconds ``retry-after`` from an Anthropic error.

    Postconditions: returns a positive float or ``None``; never raises.
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


def _split_system(messages: list) -> tuple[str, list]:
    """Split a chat ``messages`` list into ``(system_text, anthropic_messages)``.

    Anthropic carries the system prompt as a top-level parameter, not as a
    ``role: "system"`` message. Any system entries are concatenated into the
    returned system text; the remaining user/assistant turns (string content) are
    passed through.

    Preconditions: ``messages`` is a list of ``{role, content}`` dicts.
    Postconditions: returns ``(system_text, [{"role","content"}, ...])`` with only
        ``user``/``assistant`` roles in the second element. Never raises for a
        reasonably-shaped list.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role in ("user", "assistant"):
            out.append({"role": role, "content": content})
    return "\n\n".join(system_parts), out
