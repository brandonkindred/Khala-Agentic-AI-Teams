"""Utilities for LLM callers: retries with backoff and JSON extraction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from .interface import (
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSemanticExhaustionError,
    LLMUnreachableAfterRetriesError,
)

# Keys used when trying code-block fallback (optional filter)
_DEFAULT_EXPECTED_KEYS = frozenset(
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
    }
)

logger = logging.getLogger(__name__)


def _flatten_system_prompt_content(system_prompt_content: Optional[list] = None) -> str:
    """Flatten Strands ``system_prompt_content`` blocks into a single string.

    Shared by ``clients.dummy`` and ``strands_adapter`` — both accept Strands'
    structured system-prompt form (a list of content blocks, e.g.
    ``[{"text": "..."}]``) alongside a plain ``system_prompt`` string.

    Preconditions:
        - ``system_prompt_content`` is ``None`` or a list of content blocks.

    Postconditions:
        - Returns the concatenated block text (``""`` when absent/empty).
    """
    if not system_prompt_content:
        return ""
    parts: list = []
    for block in system_prompt_content:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "") or ""))
        else:
            parts.append(str(block))
    return "".join(parts)


def sha256_fingerprint(text: str, *, length: int = 16) -> str:
    """Short, stable sha256 hex digest of ``text`` for log/receipt correlation.

    Single home for the truncated-digest pattern so all fingerprints across
    the LLM service share one algorithm and can be cross-referenced.

    Preconditions:
        - ``text`` is a str; ``1 <= length <= 64``.
    Postconditions:
        - Returns the first ``length`` hex chars of sha256(utf-8 of ``text``);
          deterministic, never raises.
    """
    assert 1 <= length <= 64, f"length must be in 1..64, got {length}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


# Errors that must short-circuit the retry loop (re-raised as-is, never retried).
_NON_RETRYABLE_ERRORS = (
    LLMPermanentError,
    LLMRateLimitError,
    LLMSemanticExhaustionError,
    LLMUnreachableAfterRetriesError,
)


def _notify_retry(
    on_retry: Optional[Callable[[int, int, float, Exception], None]],
    failed_attempt: int,
    max_attempts: int,
    wait: float,
    error: Exception,
) -> None:
    """Invoke the retry-progress hook, swallowing any error it raises.

    A retry-progress hook is observability and must never abort the retry loop
    it reports on.
    """
    if on_retry is None:
        return
    try:
        on_retry(failed_attempt, max_attempts, wait, error)
    except Exception as hook_error:  # noqa: BLE001 — hook must not abort the retry loop
        logger.warning("on_retry hook failed (ignored): %s", hook_error)


def _handle_retryable_failure(
    error: Exception,
    attempt: int,
    max_attempts: int,
    backoff_base: float,
    backoff_max: float,
    on_retry: Optional[Callable[[int, int, float, Exception], None]],
) -> float:
    """Shared post-failure step for the sync and async retry loops.

    Logs the failure, notifies ``on_retry``, and returns the number of seconds
    the caller should sleep before the next attempt.  On the final attempt
    (``attempt == max_attempts - 1``) logs exhaustion and raises
    ``LLMUnreachableAfterRetriesError`` instead of returning.

    Postconditions:
        - Returns a non-negative wait when another attempt remains.
        - Raises ``LLMUnreachableAfterRetriesError`` (chained from ``error``)
          when no attempts remain; ``on_retry`` is NOT invoked in that case.
    """
    if attempt < max_attempts - 1:
        wait = min(backoff_base**attempt + random.uniform(0, 1), backoff_max)
        logger.warning(
            "LLM call failed (attempt %d/%d): %s. Next step -> Retrying in %.1fs",
            attempt + 1,
            max_attempts,
            error,
            wait,
        )
        _notify_retry(on_retry, attempt + 1, max_attempts, wait, error)
        return wait
    logger.error(
        "LLM call exhausted. Recovery summary: attempted %d calls with exponential backoff, "
        "all failed. Final error: %s",
        max_attempts,
        error,
    )
    raise LLMUnreachableAfterRetriesError(
        f"LLM unreachable after {max_attempts} attempts: {error}",
        cause=error,
    ) from error


def call_llm_with_retries(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
    on_retry: Optional[Callable[[int, int, float, Exception], None]] = None,
) -> Any:
    """
    Call fn() up to max_attempts times with exponential backoff on connection/temporary errors.
    On permanent/rate-limit errors, re-raises immediately. After exhausting retries, raises
    LLMUnreachableAfterRetriesError so the caller can return a structured result (e.g. llm_unreachable=True).

    Preconditions:
        - on_retry, if provided, accepts (failed_attempt_number, max_attempts,
          wait_seconds, exception). It should not raise; if it does anyway, the
          exception is logged and swallowed — a retry-progress hook is
          observability and must never abort the retry loop it reports on.

    Postconditions:
        - on_retry is invoked exactly once per retried attempt, immediately before the
          backoff sleep; never on success and never after the final attempt.
    """
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except _NON_RETRYABLE_ERRORS:
            raise
        except Exception as e:
            last_error = e
            wait = _handle_retryable_failure(
                e, attempt, max_attempts, backoff_base, backoff_max, on_retry
            )
            time.sleep(wait)
    if last_error:
        raise LLMUnreachableAfterRetriesError(
            f"LLM unreachable after {max_attempts} attempts: {last_error}",
            cause=last_error,
        ) from last_error
    raise LLMUnreachableAfterRetriesError(f"LLM unreachable after {max_attempts} attempts")


async def call_llm_with_retries_async(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
    on_retry: Optional[Callable[[int, int, float, Exception], None]] = None,
) -> Any:
    """Async counterpart of :func:`call_llm_with_retries`.

    Awaits ``fn()`` and uses ``await asyncio.sleep`` for backoff, so retries on
    an async path never block the event loop.  Error classification, backoff
    schedule, ``on_retry`` semantics, and the final
    ``LLMUnreachableAfterRetriesError`` are identical to the sync version (the
    two share :func:`_handle_retryable_failure`).

    Preconditions:
        - ``fn`` is a zero-arg coroutine function (its result is awaited).
        - ``on_retry`` follows the same contract as in the sync version.

    Postconditions:
        - on_retry is invoked exactly once per retried attempt, immediately before
          the backoff sleep; never on success and never after the final attempt.
    """
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except _NON_RETRYABLE_ERRORS:
            raise
        except Exception as e:
            last_error = e
            wait = _handle_retryable_failure(
                e, attempt, max_attempts, backoff_base, backoff_max, on_retry
            )
            await asyncio.sleep(wait)
    if last_error:
        raise LLMUnreachableAfterRetriesError(
            f"LLM unreachable after {max_attempts} attempts: {last_error}",
            cause=last_error,
        ) from last_error
    raise LLMUnreachableAfterRetriesError(f"LLM unreachable after {max_attempts} attempts")


def _repair_json(s: str) -> str:
    """Attempt tolerant JSON repair for common LLM output issues."""
    return re.sub(r",\s*([}\]])", r"\1", s)


def extract_json_from_response(
    text: str,
    *,
    expected_keys: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """
    Extract a single JSON object from model output (e.g. after continuation).
    Raises LLMJsonParseError on failure.
    """
    if expected_keys is None:
        expected_keys = _DEFAULT_EXPECTED_KEYS
    if "---DRAFT---" in text:
        parts = text.split("---DRAFT---", 1)
        if len(parts) == 2 and parts[1].strip():
            return {"content": parts[1].strip()}
    json_block_match = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
    if json_block_match:
        text = json_block_match.group(1).strip()
    else:
        fenced_match = re.search(
            r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.DOTALL | re.IGNORECASE
        )
        if fenced_match:
            block_content = fenced_match.group(1).strip()
            if block_content.lstrip().startswith(("{", "[")):
                text = block_content
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = _repair_json(text)
    try:
        return json.loads(repaired)
    except (json.JSONDecodeError, ValueError):
        pass
    obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if obj_match:
        raw = obj_match.group(0)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            try:
                return json.loads(_repair_json(raw))
            except (json.JSONDecodeError, ValueError):
                pass
    stripped = text.strip()
    for pattern in (
        r"^(?:Here(?:'s| is) (?:the )?JSON:?)\s*",
        r"^(?:The (?:response|output|result) is:?)\s*",
        r"^(?:JSON:?)\s*",
        r"^\s*```(?:json)?\s*",
        r"\s*```\s*$",
    ):
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE).strip()
    if stripped != text.strip():
        obj_match2 = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if obj_match2:
            try:
                return json.loads(obj_match2.group(0))
            except (json.JSONDecodeError, ValueError):
                pass
    for block_match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
        block = block_match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and expected_keys & set(parsed.keys()):
                return parsed
        except (json.JSONDecodeError, ValueError):
            try:
                parsed = json.loads(_repair_json(block))
                if isinstance(parsed, dict) and expected_keys & set(parsed.keys()):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
    raise LLMJsonParseError(
        "Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. "
        f"Response preview: {text[:500]!r}...",
        error_kind="json_parse",
        response_preview=text[:500],
    )
