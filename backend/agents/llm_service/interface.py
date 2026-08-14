"""
Abstract interface and exceptions for the central LLM service.

All agent teams should depend on this interface and get_client(); they must not
construct provider-specific clients directly.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Exceptions (unified for all teams)
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base exception for LLM-related errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.cause = cause


class LLMRateLimitError(LLMError):
    """Raised when the LLM returns 429 Too Many Requests and retries are exhausted.

    ``retry_after_seconds`` optionally carries the value parsed from a provider
    ``Retry-After`` response header so the retry loop that catches this error can
    honor it (raising the wait to at least that long, never below the configured
    floor). ``None`` when no header was present or honoring is disabled.

    ``limit_kind`` optionally carries a structured classification (``session``,
    ``weekly``, or ``rate``) so failover marking can park the provider for the
    correct window without re-parsing opaque message text. ``None`` when the
    raiser did not classify the 429.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        cause: Optional[Exception] = None,
        retry_after_seconds: Optional[float] = None,
        limit_kind: Optional[str] = None,
    ):
        super().__init__(message, status_code=status_code, cause=cause)
        self.retry_after_seconds = retry_after_seconds
        self.limit_kind = limit_kind


class LLMTemporaryError(LLMError):
    """Raised when the LLM returns 5xx or network errors and retries are exhausted."""


class LLMUnreachableAfterRetriesError(LLMTemporaryError):
    """Raised when the caller exhausted retries and could not reach the LLM. Orchestrator should pause job."""


class LLMSemanticExhaustionError(LLMTemporaryError):
    """Raised when the model produced no assistant content and no proof-of-change retry remains.

    A semantically exhausted call is one where the request transport succeeded
    (HTTP 200) but the model returned zero content — typically because it spent
    its whole generation on reasoning. Unlike transport faults, re-sending the
    identical payload is very unlikely to help, so the client runs a
    proof-of-change thinking-downgrade ladder (steps reasoning down and ends by
    disabling thinking entirely — up to two rungs from a top tier, e.g.
    ``max`` -> ``high`` -> off) and then raises this error as a terminal receipt
    instead of looping on the transient schedule.

    Subclasses ``LLMTemporaryError`` so existing pause/degrade handlers keep
    working: at the macro level the condition is temporary (a different prompt
    may succeed later), mirroring ``LLMUnreachableAfterRetriesError``.

    Preconditions:
        - ``attempts_used >= 1``; ``payload_fingerprint`` is a stable digest of
          the last request payload sent. The keyword defaults exist only so
          exception-reconstruction protocols (pickle rebuilds via
          ``cls(*self.args)`` then restores ``__dict__``) can round-trip the
          receipt; the client always supplies every field.
    Postconditions / Invariants:
        - ``failure_class`` is always ``"semantic_exhaustion"``.
        - ``retry_thinking_level`` is the thinking value used on the LAST
          proof-of-change rung — a lower level string, or ``False`` once the
          ladder reached thinking-off — or ``None`` when no downgrade ran at all
          (thinking was already off, so no rung was attempted). A ``None`` here
          therefore distinguishes "the ladder was never run" from "the ladder ran
          and ended at thinking-off" (``False``).
        - ``content_bytes_seen`` is True iff any failing attempt produced raw
          content bytes. Those bytes are necessarily whitespace-only: an
          attempt with non-whitespace content succeeds and never contributes
          to this error, so the field distinguishes "model emitted whitespace"
          from "model emitted nothing at all".
        - ``schema_forced`` is True iff this receipt was raised because a
          provider-enforced schema-constrained decoding attempt
          (``complete_json(..., schema=...)`` on a client whose
          ``supports_structured_output()`` is True) starved the content
          channel. On this path there is no thinking-downgrade ladder at all
          — ``retry_thinking_level`` is therefore always ``None`` here, the
          SAME value it takes when no ladder ever ran for an unrelated reason
          (thinking was already off). Callers that treat
          ``retry_thinking_level is None`` as "safe to retry the same input"
          (e.g. ``code_review_agent/mapping.py``) MUST also check
          ``schema_forced`` before drawing that conclusion: a schema-forced
          exhaustion is expected to reproduce on an identical schema-forced
          retry, unlike a genuine one-off stochastic empty.
    """

    failure_class = "semantic_exhaustion"

    def __init__(
        self,
        message: str,
        *,
        attempts_used: int = 0,
        original_thinking_level: "bool | str | None" = None,
        retry_thinking_level: "bool | str | None" = None,
        content_bytes_seen: bool = False,
        payload_fingerprint: str = "",
        finish_reason: str = "",
        schema_forced: bool = False,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, cause=cause)
        self.attempts_used = attempts_used
        self.original_thinking_level = original_thinking_level
        self.retry_thinking_level = retry_thinking_level
        self.content_bytes_seen = content_bytes_seen
        self.payload_fingerprint = payload_fingerprint
        self.finish_reason = finish_reason
        self.schema_forced = schema_forced


class LLMPermanentError(LLMError):
    """Raised for 4xx errors (except 429) or malformed responses. Do not retry."""


class LLMNotConfiguredError(LLMPermanentError):
    """Raised by ``get_client`` when no LLM provider is configured.

    The Postgres-backed provider list is the sole source of LLM resolution (the
    ``dummy`` provider is the only override). When the list is empty — or Postgres
    is unset — and the provider is not ``dummy``, there is no client to build, so
    this is raised. It subclasses :class:`LLMPermanentError` deliberately: it is
    non-retryable, so orchestrator retry loops fail the job immediately (never
    burning the backoff schedule) rather than treating it as transient. The
    operator resolves it by adding a provider in the LLM Provider settings
    (``/llm-config``).

    Invariants:
        - Never raised on the ``dummy`` path (that pre-empts the list).
    """


class LLMJsonParseError(LLMPermanentError):
    """Raised when LLM returned a 200 response but the content is not valid JSON.

    ``response_preview`` is a truncated, log-safe slice (corrective prompts and
    logs). ``raw_response`` is the full untruncated reply when the raise site
    still has it; an empty ``raw_response`` falls back to ``response_preview``
    so callers that only pass a preview still expose a usable body to observers.

    Preconditions:
        - ``message``, ``response_preview``, and ``raw_response`` are strs.
    Postconditions:
        - ``self.response_preview`` is the truncated preview unchanged.
        - ``self.raw_response`` is ``raw_response`` when non-empty, else
          ``response_preview``.
    """

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "json_parse",
        response_preview: str = "",
        raw_response: str = "",
        correction_attempts_used: int = 0,
    ):
        super().__init__(message)
        self.error_kind = error_kind
        self.response_preview = response_preview
        self.raw_response = raw_response if raw_response else response_preview
        self.correction_attempts_used = correction_attempts_used


class LLMSchemaValidationError(LLMPermanentError):
    """Raised when the LLM returned valid JSON that fails Pydantic schema validation.

    Produced by the ``complete_validated`` helper after all corrective retries
    have been exhausted. Wraps the underlying ``pydantic.ValidationError`` so
    callers see a consistent ``LLMPermanentError`` subclass with the same
    ``correction_attempts_used`` shape as ``LLMJsonParseError``.
    """

    def __init__(
        self,
        message: str,
        *,
        response_preview: str = "",
        correction_attempts_used: int = 0,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, cause=cause)
        self.response_preview = response_preview
        self.correction_attempts_used = correction_attempts_used


class LLMTruncatedError(LLMError):
    """Raised when LLM response was truncated due to token limit (finish_reason=length).

    Invariants:
        - ``think_used`` is the thinking wire value of the attempt that
          produced the truncated content (set by the client when known, e.g.
          after an in-call thinking downgrade), or ``None`` when unknown.
          Continuation paths use it so a downgraded call is not silently
          resumed at the original thinking level.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        finish_reason: str = "length",
        think_used: "bool | str | None" = None,
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.finish_reason = finish_reason
        self.think_used = think_used


_complete_json_raw_var: ContextVar[str | None] = ContextVar("complete_json_raw", default=None)
"""Per-call raw JSON text from the most recent ``complete_json`` on this context.

Shared ``LLMClient`` instances are reused across concurrent code-review chunk
and tail-pass calls. Storing the last raw response on the client object would
let one call overwrite another's text before the observer reads it. A
``ContextVar`` is isolated per asyncio task and per thread, matching
``llm_service.attribution``.
"""


def record_complete_json_raw(text: str) -> None:
    """Store the raw JSON response for the current call on this context.

    Preconditions:
        - ``text`` is the provider's response body before JSON parse (may be empty).
    Postconditions:
        - ``take_complete_json_raw`` on the same context returns ``text`` until
          it is taken or overwritten by a later ``record_complete_json_raw``.
    """
    _complete_json_raw_var.set(text)


def take_complete_json_raw() -> str:
    """Return and clear the raw JSON recorded on this context.

    Preconditions:
        - none; missing or empty recordings are treated as no raw text.
    Postconditions:
        - returns the last recorded string, or ``""`` when none was recorded.
        - this context's slot is cleared so a later sequential call on the
          same thread cannot reuse a previous call's raw text.
    """
    raw = _complete_json_raw_var.get()
    _complete_json_raw_var.set(None)
    if not raw:
        return ""
    return raw


_complete_json_turns_var: ContextVar[list[tuple[str, str, float]] | None] = ContextVar(
    "complete_json_turns", default=None
)
"""Per-call ``(prompt, response, started_monotonic)`` turns for a provider
call that continued.

Ollama's truncation path issues extra HTTP requests with continuation prompts
for both ``complete_json`` and text ``complete``. Those inner turns never
return to ``complete_validated`` as separate calls, so the provider records
them here and the observer drains them with :func:`take_complete_json_turns`.
``started_monotonic`` is ``time.monotonic()`` captured immediately before
that turn's HTTP request began.
"""

_observer_turn_started_var: ContextVar[float | None] = ContextVar(
    "complete_json_observer_turn_started", default=None
)


def record_complete_json_turn(
    prompt: str,
    response: str,
    *,
    started_monotonic: float | None = None,
) -> None:
    """Append one inner HTTP turn on this context.

    Preconditions:
        - ``prompt`` is the text that identifies this HTTP turn: the original
          user prompt for the truncated first reply, or a serialization of the
          full ``messages`` conversation sent on a continuation request.
        - ``response`` is that turn's model text (may be a partial fragment).
        - ``started_monotonic``, when given, is ``time.monotonic()`` from
          immediately before this turn's provider request; omitted recordings
          stamp ``time.monotonic()`` at record time.
    Postconditions:
        - :func:`take_complete_json_turns` on the same context includes this
          triple after any previously recorded turns, in record order.
    """
    turns = list(_complete_json_turns_var.get() or [])
    turns.append(
        (prompt, response, time.monotonic() if started_monotonic is None else started_monotonic)
    )
    _complete_json_turns_var.set(turns)


def take_complete_json_turns() -> list[tuple[str, str, float]]:
    """Return and clear inner HTTP turns recorded on this context.

    Preconditions:
        - none; missing recordings are treated as no inner turns.
    Postconditions:
        - returns a new list of ``(prompt, response, started_monotonic)``
          triples in record order, or ``[]`` when none were recorded.
        - this context's slot is cleared so a later sequential call cannot
          reuse a previous call's turns.
    """
    turns = _complete_json_turns_var.get()
    _complete_json_turns_var.set(None)
    if not turns:
        return []
    return list(turns)


def complete_json_turn_count() -> int:
    """How many inner HTTP turns are currently recorded on this context.

    Preconditions:
        none.
    Postconditions:
        Returns ``0`` when none are recorded; does not clear the slot.
    """
    turns = _complete_json_turns_var.get()
    return 0 if not turns else len(turns)


@contextmanager
def observer_turn_started(started_monotonic: float | None) -> Iterator[None]:
    """Bind this continuation turn's start time for transcript writers.

    Preconditions:
        ``started_monotonic`` is the provider-stamped start, or ``None``.
    Postconditions:
        :func:`observer_turn_started_monotonic` returns that value inside the
        ``with`` block and restores the previous binding on exit.
    """
    token = _observer_turn_started_var.set(started_monotonic)
    try:
        yield
    finally:
        _observer_turn_started_var.reset(token)


def observer_turn_started_monotonic() -> float | None:
    """Return the start time bound for the in-flight observer callback.

    Preconditions:
        none.
    Postconditions:
        The monotonic timestamp from :func:`observer_turn_started`, or
        ``None`` when the observer is not inside a recorded inner turn
        (a single outer call with no continuation).
    """
    return _observer_turn_started_var.get()


def reset_complete_json_observer_state() -> None:
    """Drop any raw JSON or continuation turns left on this context.

    Preconditions:
        - none.
    Postconditions:
        - ``take_complete_json_raw`` and ``take_complete_json_turns`` return
          empty until a later ``record_*`` on this context. Call at the start
          of every ``complete_json`` / ``complete`` so a previous call that
          raised after recording a turn cannot leak that turn into the next
          observer.
    """
    _complete_json_raw_var.set(None)
    _complete_json_turns_var.set(None)


# Message used when Ollama 429 indicates weekly usage limit exceeded (for logging and job state)
OLLAMA_WEEKLY_LIMIT_MESSAGE = "Ollama LLM usage limit exceeded for week"


# ---------------------------------------------------------------------------
# LLMClient interface
# ---------------------------------------------------------------------------


class LLMClient(ABC):
    """
    Minimal abstraction around an LLM client.

    Implementations (Ollama, Dummy, future OpenAI/Anthropic) live in llm_service.clients.
    Agents obtain a client via get_client(agent_key?) and call complete_json / complete / get_max_context_tokens.
    """

    @abstractmethod
    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        schema: "Optional[dict | type[BaseModel]]" = None,
        structured_output_model: "Optional[type[BaseModel]]" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Run the model with the given prompt and return a JSON-decoded dict.

        ``objective`` is a short, required, human-readable phrase describing *why*
        this call is made (e.g. ``"rank job candidates"``). It is stamped onto every
        LLM log line and telemetry record so the call can be attributed in the logs.

        Pass ``tools`` (OpenAI-compatible tool definitions) to enable function/tool calling.
        When the model invokes a tool, the returned dict has the key ``__tool_calls__`` whose
        value is a list of tool-call objects (id, type, function.name, function.arguments).
        Optional kwargs may include expected_keys, decomposition_hints for PA-style robust extraction.

        ``think`` controls chain-of-thought / reasoning mode: ``None`` (default)
        resolves to the platform default — the model's max registered thinking
        level when known; ``False`` disables; a string selects a specific level.

        ``schema`` (optional): a JSON Schema ``dict`` or a ``pydantic.BaseModel``
        subclass. When given AND ``self.supports_structured_output()`` is True,
        the implementation MAY request provider-enforced schema-conformant
        decoding on the wire instead of the loose ``json_object`` mode it uses
        by default. Passing ``schema`` to a client whose
        ``supports_structured_output()`` is False is NOT an error — it is
        silently ignored (the shape contract remains prompt-text-only,
        enforced downstream by pydantic, exactly as when ``schema`` is
        omitted). Mutually exclusive with ``tools`` on clients that honor it
        (see ``OllamaLLMClient``).

        ``structured_output_model`` (optional): the exact ``type[BaseModel]``
        subclass passed as ``structured_output=`` to ``build_agent``/Strands,
        forwarded here only when the call originates from Strands'
        ``Model.structured_output()`` bridge. This is deliberately distinct
        from ``schema``: implementations MAY use its presence to select a
        response deterministically (e.g. a stub/test client routing by class
        identity instead of prompt text), but MUST NOT let it alter
        wire-protocol behavior toward a real provider — ``schema`` is the
        parameter for that. Absent (``None``) on any call that doesn't
        originate from that bridge. Unsupporting clients silently ignore it
        via ``**kwargs``.

        ``max_tokens`` (optional): a cap on generated output tokens, forwarded
        by implementations that honor a token limit (e.g. ``OllamaLLMClient``,
        ``ClaudeLLMClient``). ``None`` (default) means no caller-supplied cap
        — the implementation falls back to its own default/resolution logic.

        Preconditions: ``objective`` is a non-empty string. ``DummyLLMClient``
        is a documented exception: it defaults ``objective`` to ``"dummy"``
        on ``complete``/``complete_json``/``chat`` because it makes no real
        LLM call and performs no attribution, so test stubs are not required
        to declare one. Real providers (``OllamaLLMClient``,
        ``ClaudeLLMClient``) enforce the non-empty precondition.
        """
        ...

    def supports_structured_output(self) -> bool:
        """Whether this client can request provider-enforced (decoder-level)
        schema-conformant JSON on the wire, as distinct from the loose
        ``json_object`` wire mode ``complete_json`` uses by default.

        This is a capability FLAG, not a promise about any particular call's
        outcome — a client that returns True may still raise
        ``LLMSemanticExhaustionError(schema_forced=True)`` for a given prompt
        (see ``OllamaLLMClient._complete_json_impl``).

        Preconditions: none.
        Postconditions: synchronous, makes no network call, never raises.
            Returns ``False`` by default (every client that has not opted
            in). Override in a client whose wire protocol supports
            decoder-level schema constraints.
        """
        return False

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
        """
        Run the model and return raw text.

        Override in implementations that support it. Default uses complete_json and extracts text.
        Pass ``tools`` for function/tool calling; tool-call responses are returned as JSON strings.
        ``objective`` (required) — see :meth:`complete_json`.

        ``think`` controls chain-of-thought / reasoning mode (see ``complete_json``).

        ``max_tokens`` is forwarded to ``complete_json()`` — see its docstring.
        """
        result = self.complete_json(
            prompt,
            objective=objective,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            tools=tools,
            think=think,
        )
        if isinstance(result, dict) and len(result) == 1 and "text" in result:
            return str(result["text"])
        return json.dumps(result)

    def get_max_context_tokens(self) -> int:
        """
        Return the model's maximum context size in tokens.

        Used for context_sizing and chunking. Default 16384.
        Override in implementations that can query the model (e.g. Ollama).
        """
        return 16384

    # Alias for SE code that uses complete_text
    def complete_text(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        think: "bool | str | None" = None,
    ) -> str:
        """Alias for complete() for backward compatibility with SE team.

        ``objective`` (required) — see :meth:`complete_json`.
        """
        return self.complete(
            prompt,
            objective=objective,
            temperature=temperature,
            max_tokens=None,
            system_prompt=None,
            think=think,
        )

    def chat(
        self,
        messages: list[Dict[str, Any]],
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

        ``objective`` (required) — see :meth:`complete_json`.

        Returns one of:

        - ``{"__tool_calls__": [...]}`` when the model invokes tools, regardless
          of ``response_format`` (tool invocations always come back as a dict
          envelope).
        - A parsed ``Dict[str, Any]`` when ``response_format="json"`` (the
          default). The wire request includes ``response_format=json_object``
          when no tools are present, and the assistant content is parsed via
          ``_extract_json`` with markdown-fence and repair fallbacks.
        - A raw ``str`` of the assistant content when ``response_format="text"``.
          No forcing on the wire, no parsing — for conversational prose, the
          ``---DRAFT---`` marker pattern, template-based outputs, etc.

        Default implementation: not supported — override in Ollama / Dummy.
        """
        raise LLMPermanentError(f"{type(self).__name__} does not implement chat")
