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
instead of duplicating the loop.

Invariants:
    - Exactly one JSON-parse retry policy and one transient-error
      classification rule is defined here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Type

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

    Any other exception ``e`` is first passed through ``unwrap_exception``
    (identity by default; pass a hook to unwrap a framework wrapper such as
    ``strands.types.exceptions.EventLoopException`` before classifying the
    cause). If the unwrapped cause is one of ``transient_exceptions``, it is
    re-raised immediately and unwrapped — never retried locally — so the
    caller's own retry/backoff owns it. Otherwise, ``on_unexpected_error``
    (if given) produces a fallback dict; without it, the unwrapped cause is
    re-raised.

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
          transient or unexpected error never consumes an attempt (it exits
          the loop immediately).
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
        if invoke is None or fresh_agent_per_attempt:
            invoke = agent_factory()
        try:
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
