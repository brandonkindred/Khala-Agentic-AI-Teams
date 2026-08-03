"""Classify Ollama Cloud 429 bodies into session / weekly / rate limit kinds.

Preconditions: callers pass the raw response body text (may be empty).
Postconditions: returns one of ``LIMIT_KIND_SESSION``, ``LIMIT_KIND_WEEKLY``,
    or ``LIMIT_KIND_RATE``. Never raises.
"""

from __future__ import annotations

import json
from typing import Optional

# Structured limit kinds stored on ``LLMRateLimitError.limit_kind`` and
# ``llm_provider_configs.limit_type``.
LIMIT_KIND_SESSION = "session"
LIMIT_KIND_WEEKLY = "weekly"
LIMIT_KIND_RATE = "rate"

# Phrases observed in Ollama Cloud 429 bodies (case-insensitive match).
OLLAMA_SESSION_USAGE_LIMIT_PHRASE = "session usage limit"
OLLAMA_WEEKLY_USAGE_LIMIT_PHRASE = "weekly usage limit"

_VALID_KINDS = frozenset({LIMIT_KIND_SESSION, LIMIT_KIND_WEEKLY, LIMIT_KIND_RATE})


def _extract_error_text(body: str) -> str:
    """Return the ``error`` field from a JSON body when present, else ``body``.

    Preconditions: ``body`` is a string (may be empty).
    Postconditions: returns a non-None string suitable for phrase matching.
    """
    text = (body or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, str) and err.strip():
                return err
    return text


def classify_ollama_limit_kind(body: str) -> str:
    """Classify an Ollama 429 response body into a limit kind.

    Prefers a JSON ``error`` property when the body is ``{"error": "..."}``.
    Matching is case-insensitive against known Cloud usage-limit phrases.

    Preconditions: ``body`` is a string (may be empty / non-JSON).
    Postconditions: returns ``LIMIT_KIND_SESSION``, ``LIMIT_KIND_WEEKLY``, or
        ``LIMIT_KIND_RATE``. Never raises.
    """
    haystack = _extract_error_text(body).lower()
    if OLLAMA_SESSION_USAGE_LIMIT_PHRASE in haystack:
        return LIMIT_KIND_SESSION
    if OLLAMA_WEEKLY_USAGE_LIMIT_PHRASE in haystack:
        return LIMIT_KIND_WEEKLY
    return LIMIT_KIND_RATE


def resolve_limit_kind(
    *,
    limit_kind: Optional[str],
    message: str,
    weekly_legacy_message: str,
) -> str:
    """Resolve the effective limit kind for failover marking.

    Priority: structured ``limit_kind`` when valid; else legacy weekly message /
    Ollama body phrases in ``message``; else ``rate``.

    Preconditions: ``message`` and ``weekly_legacy_message`` are strings;
        ``limit_kind`` is a string or ``None``.
    Postconditions: returns one of the three limit kinds. Never raises.
    """
    kind = (limit_kind or "").strip().lower()
    if kind in _VALID_KINDS:
        return kind
    combined = message or ""
    if weekly_legacy_message and weekly_legacy_message in combined:
        return LIMIT_KIND_WEEKLY
    return classify_ollama_limit_kind(combined)
