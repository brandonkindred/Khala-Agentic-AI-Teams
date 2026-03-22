"""Utilities for LLM callers: retries with backoff and JSON extraction."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Callable, Dict, Optional

import httpx

from .interface import (
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMUnreachableAfterRetriesError,
)

# Keys used when trying code-block fallback (optional filter)
_DEFAULT_EXPECTED_KEYS = frozenset({
    "files", "summary", "code", "overview", "issues", "approved", "components",
    "architecture_document", "diagrams", "decisions",
    "tasks", "execution_order",
    "bugs_found", "integration_tests", "unit_tests", "readme_content",
})

logger = logging.getLogger(__name__)


def call_llm_with_retries(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
) -> Any:
    """
    Call fn() up to max_attempts times with exponential backoff on connection/temporary errors.
    On permanent/rate-limit errors, re-raises immediately. After exhausting retries, raises
    LLMUnreachableAfterRetriesError so the caller can return a structured result (e.g. llm_unreachable=True).
    """
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except (LLMPermanentError, LLMRateLimitError, LLMUnreachableAfterRetriesError):
            raise
        except (LLMTemporaryError, httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout, LLMError) as e:
            last_error = e
            if attempt < max_attempts - 1:
                wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. Next step -> Retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "LLM call exhausted. Recovery summary: attempted %d calls with exponential backoff, "
                    "all failed. Final error: %s",
                    max_attempts,
                    e,
                )
                raise LLMUnreachableAfterRetriesError(
                    f"LLM unreachable after {max_attempts} attempts: {e}",
                    cause=e,
                ) from e
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1:
                wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. Next step -> Retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "LLM call exhausted. Recovery summary: attempted %d calls with exponential backoff, "
                    "all failed. Final error: %s",
                    max_attempts,
                    e,
                )
                raise LLMUnreachableAfterRetriesError(
                    f"LLM unreachable after {max_attempts} attempts: {e}",
                    cause=e,
                ) from e
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
        fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.DOTALL | re.IGNORECASE)
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
