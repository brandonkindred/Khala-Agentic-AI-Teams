"""
Shared "call an LLM, parse JSON out of it, retry on failure" helper.

Every blogging agent that needs a structured response from the model
(compliance, fact-check, plan-critic, copy-editor, ghost-writer, writer,
publication) currently hand-rolls its own version of the same loop: invoke
an agent, run ``extract_json_from_response`` on the text, retry once with a
stricter prompt on a parse failure, and re-raise transient LLM errors
unwrapped so the caller (Temporal's activity funnel, or the thread-mode job
runner) owns the retry instead of blocking here. ``call_json_with_retry``
extracts that policy into one parameterized helper, covering every existing
call site's variant (attempt count, a fresh agent per attempt, backoff, and
the copy-editor's extra step of unwrapping a wrapped exception before
classifying its cause) so that callers CAN configure it via parameters
instead of duplicating the loop. ``run_json_gate`` sits one layer above it:
it additionally owns the ``strands.Agent`` construction and the standard
``EventLoopException`` unwrap that most call sites re-implement as a local
``_agent_factory``/``_unwrap`` closure pair (a pair some call sites already
omit or drift from — see the epic this helper was extracted for), and wires
a caller-supplied fallback builder to ``on_exhausted``/``on_unexpected_error``.

Invariants:
    - Exactly one JSON-parse retry policy and one transient-error
      classification rule is defined here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Type

from strands import Agent
from strands.types.exceptions import EventLoopException

from llm_service import LLMJsonParseError, LLMRateLimitError, LLMTemporaryError
from llm_service.util import extract_json_from_response

AgentInvoker = Callable[[str], Any]
"""A callable that runs a single LLM turn, e.g. a ``strands.Agent`` instance: ``agent(prompt) -> result``."""

AgentFactory = Callable[[], AgentInvoker]
"""A zero-argument callable that builds/returns an :data:`AgentInvoker`."""

_DEFAULT_STRICT_JSON_SUFFIX = (
    "\n\nRespond with a single JSON object only (no markdown, no code fence)."
)

_logger = logging.getLogger(__name__)


def call_json_with_retry(
    agent_factory: AgentFactory,
    prompt: str,
    *,
    max_attempts: int = 2,
    expected_keys: Optional[Sequence[str]] = None,
    strict_json_suffix: str = _DEFAULT_STRICT_JSON_SUFFIX,
    fresh_agent_per_attempt: bool = False,
    transient_exceptions: Tuple[Type[Exception], ...] = (LLMRateLimitError, LLMTemporaryError),
    unwrap_exception: Callable[[Exception], Exception] = lambda e: e,
    backoff_seconds: Optional[Callable[[int], float]] = None,
    on_exhausted: Optional[Callable[[LLMJsonParseError], Dict[str, Any]]] = None,
    on_unexpected_error: Optional[Callable[[Exception], Dict[str, Any]]] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Invoke an agent and parse a JSON dict from its response, retrying on parse failure.

    On each ``LLMJsonParseError`` with attempts remaining, the prompt is
    resent with ``strict_json_suffix`` appended (this repeats on every
    subsequent failed attempt, not just the first). Once ``max_attempts`` is
    exhausted, ``on_exhausted`` (if given) is called with the last error to
    produce a fallback dict; otherwise the last ``LLMJsonParseError`` is
    re-raised. ``backoff_seconds``, if given, is a callback taking the
    zero-based attempt index and returning the number of seconds to sleep
    before that retry; omit it to skip sleeping between retries.

    Any other exception ``e`` — including one raised by ``agent_factory()`` —
    is first passed through ``unwrap_exception`` (identity by default; pass a
    hook to unwrap a framework wrapper such as
    ``strands.types.exceptions.EventLoopException`` before classifying the
    cause). If the unwrapped cause is one of ``transient_exceptions``, it is
    re-raised immediately and unwrapped — never retried locally — so the
    caller's own retry/backoff owns it. Otherwise, ``on_unexpected_error``
    (if given) produces a fallback dict; without it, the unwrapped cause is
    re-raised. ``agent_factory()`` runs inside the same exception boundary as
    the invoke so a construction failure (e.g. rejected model config) follows
    the same fallback path as an invoke-time unexpected error.

    Preconditions:
        - ``max_attempts >= 1``.
        - ``prompt`` is a non-empty string.
        - ``agent_factory()`` returns a callable accepting a single string
          argument and returning a value convertible to ``str``.

    Postconditions:
        - Returns a ``dict`` on a successful parse, or via ``on_exhausted``/
          ``on_unexpected_error`` when one is supplied for the failure that
          occurred; never returns ``None``.
        - Otherwise raises the (possibly unwrapped) triggering exception —
          no failure is silently swallowed without an explicit fallback hook.
        - Consumes at most one retry attempt per JSON-parse failure; a
          transient or unexpected error (including ``agent_factory`` failure)
          never consumes an attempt (it exits the loop immediately).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not prompt:
        raise ValueError("prompt must be non-empty")
    log = logger or _logger
    keys = frozenset(expected_keys) if expected_keys is not None else None

    invoke: Optional[AgentInvoker] = None
    last_json_error: Optional[LLMJsonParseError] = None
    working_prompt = prompt

    for attempt in range(max_attempts):
        try:
            if invoke is None or fresh_agent_per_attempt:
                invoke = agent_factory()
            result = invoke(working_prompt)
            return extract_json_from_response(str(result).strip(), expected_keys=keys)
        except LLMJsonParseError as e:
            last_json_error = e
            attempts_left = max_attempts - attempt - 1
            if attempts_left > 0:
                log.warning(
                    "call_json_with_retry: JSON parse failed (attempt %d/%d), retrying: %s",
                    attempt + 1,
                    max_attempts,
                    e,
                )
                if backoff_seconds is not None:
                    time.sleep(backoff_seconds(attempt))
                working_prompt = prompt + strict_json_suffix
                continue
            log.warning(
                "call_json_with_retry: JSON parse failed after %d attempt(s): %s",
                max_attempts,
                e,
            )
            if on_exhausted is not None:
                return on_exhausted(e)
            raise
        except Exception as e:
            cause = unwrap_exception(e)
            if isinstance(cause, transient_exceptions):
                log.warning(
                    "call_json_with_retry: transient LLM error, re-raising for caller retry: %s",
                    cause,
                )
                raise cause
            log.exception("call_json_with_retry: unexpected error: %s", cause)
            if on_unexpected_error is not None:
                return on_unexpected_error(cause)
            raise cause

    # Unreachable: the loop above always returns or raises before falling through.
    assert last_json_error is not None  # pragma: no cover
    raise last_json_error  # pragma: no cover


def _unwrap_event_loop_exception(exc: Exception) -> Exception:
    """Unwrap a strands ``EventLoopException`` to its underlying cause, if wrapped."""
    return exc.original_exception if isinstance(exc, EventLoopException) else exc


def run_json_gate(
    model: Any,
    system_prompt: str,
    prompt: str,
    *,
    fallback_builder: Optional[Callable[[Exception], Dict[str, Any]]] = None,
    on_exhausted: Optional[Callable[[LLMJsonParseError], Dict[str, Any]]] = None,
    on_unexpected_error: Optional[Callable[[Exception], Dict[str, Any]]] = None,
    max_attempts: int = 2,
    expected_keys: Optional[Sequence[str]] = None,
    strict_json_suffix: str = _DEFAULT_STRICT_JSON_SUFFIX,
    fresh_agent_per_attempt: bool = False,
    agent_kwargs: Optional[Dict[str, Any]] = None,
    transient_exceptions: Tuple[Type[Exception], ...] = (LLMRateLimitError, LLMTemporaryError),
    unwrap_exception: Callable[[Exception], Exception] = _unwrap_event_loop_exception,
    backoff_seconds: Optional[Callable[[int], float]] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Invoke a fresh ``strands.Agent`` and parse a JSON dict from its response, retrying on parse failure.

    A thin wrapper over :func:`call_json_with_retry` that additionally owns
    ``Agent`` construction (``Agent(model=model, system_prompt=system_prompt,
    **agent_kwargs)``, rebuilt on each attempt when ``fresh_agent_per_attempt``
    is set) and defaults ``unwrap_exception`` to unwrapping a strands
    ``EventLoopException`` before classifying its cause, so callers no longer
    need to hand-write that closure (or risk omitting it, as some call sites
    that predate this helper did).

    ``fallback_builder``, if given, is used for both ``on_exhausted`` and
    ``on_unexpected_error`` unless the caller overrides one or both
    explicitly — an explicit ``on_exhausted``/``on_unexpected_error`` always
    wins over ``fallback_builder`` for that hook. This covers a single
    shared fallback (the common case), two distinct fallbacks per hook, or
    a fallback for only one hook (e.g. ``on_exhausted`` alone, leaving
    unexpected errors to propagate for the caller's own wrapping).

    All other parameters (``max_attempts``, ``expected_keys``,
    ``strict_json_suffix``, ``transient_exceptions``, ``backoff_seconds``,
    ``logger``) are passed through unchanged to :func:`call_json_with_retry`;
    see its docstring for their semantics.

    Preconditions:
        - ``model is not None``.
        - ``system_prompt`` and ``prompt`` are non-empty strings.
        - ``max_attempts >= 1``.

    Postconditions:
        - Returns a ``dict`` on a successful parse, or via the resolved
          ``on_exhausted``/``on_unexpected_error`` fallback for the failure
          that occurred; never returns ``None``.
        - Otherwise raises the (possibly unwrapped) triggering exception —
          matches :func:`call_json_with_retry`'s no-silent-swallow contract.
        - A transient cause wrapped in an ``EventLoopException`` is still
          classified as transient and re-raised for the caller's own retry,
          even when the caller supplies no ``unwrap_exception`` override.
    """
    assert model is not None, "model is required"
    assert system_prompt, "system_prompt must be non-empty"

    def _agent_factory() -> Agent:
        return Agent(model=model, system_prompt=system_prompt, **(agent_kwargs or {}))

    return call_json_with_retry(
        _agent_factory,
        prompt,
        max_attempts=max_attempts,
        expected_keys=expected_keys,
        strict_json_suffix=strict_json_suffix,
        fresh_agent_per_attempt=fresh_agent_per_attempt,
        transient_exceptions=transient_exceptions,
        unwrap_exception=unwrap_exception,
        backoff_seconds=backoff_seconds,
        on_exhausted=on_exhausted if on_exhausted is not None else fallback_builder,
        on_unexpected_error=on_unexpected_error
        if on_unexpected_error is not None
        else fallback_builder,
        logger=logger,
    )
