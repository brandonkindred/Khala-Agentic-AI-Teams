"""Rate-limit (HTTP 429) backoff policy — the single home of the slow schedule.

A 429 from an LLM provider means the account/budget is exhausted and will not
reset in seconds. Retrying it on the same fast schedule used for transient
5xx/network faults (``LLM_BACKOFF_*``) just burns attempts against an exhausted
budget. This module owns a SEPARATE, deliberately slow schedule whose first
retry waits minutes (default 300s), doubling up to a cap (default 3600s).

Both retry layers consume this one policy:
  * ``llm_service.clients.ollama.OllamaLLMClient._ollama_post`` (every team's
    central client path), and
  * ``investment_team.strategy_lab.agents._llm_envelope.invoke_agent`` (the
    Strategy Lab agents that build strands models directly and bypass the
    central client).

This is a leaf module: it imports only stdlib + ``llm_service.config`` (which is
itself stdlib-only), so it cannot create an import cycle with either consumer.
"""

from __future__ import annotations

import os
import random

from . import config as llm_config

__all__ = ["parse_rate_limit_retry_config", "rate_limit_retry_delay"]

# Defaults: first 429 retry at 5 minutes, doubling, capped at 1 hour, 5 retries
# (6 total attempts) => worst-case ~2h15m of waiting before raising.
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_INITIAL_SECONDS = 300.0
_DEFAULT_CAP_SECONDS = 3600.0


def parse_rate_limit_retry_config() -> tuple[int, float, float]:
    """Parse the rate-limit backoff env vars.

    Returns ``(max_retries, initial_seconds, cap_seconds)`` for the 429 schedule.
    Mirrors ``ollama._parse_retry_config``'s garbage-tolerant pattern: each value
    is read independently and a non-integer/non-float (or empty) env falls back
    to its documented default rather than raising — env misconfiguration must not
    crash an LLM call.

    Preconditions: none.
    Postconditions: returns a 3-tuple with ``max_retries >= 0``,
    ``initial_seconds > 0``, ``cap_seconds >= initial_seconds``; never raises.
    """
    raw_retries = os.environ.get(llm_config.ENV_LLM_RATE_LIMIT_MAX_RETRIES) or str(
        _DEFAULT_MAX_RETRIES
    )
    raw_initial = os.environ.get(llm_config.ENV_LLM_RATE_LIMIT_BACKOFF_INITIAL) or str(
        _DEFAULT_INITIAL_SECONDS
    )
    raw_cap = os.environ.get(llm_config.ENV_LLM_RATE_LIMIT_BACKOFF_MAX) or str(_DEFAULT_CAP_SECONDS)

    try:
        max_retries = max(0, int(raw_retries))
    except ValueError:
        max_retries = _DEFAULT_MAX_RETRIES

    try:
        initial_seconds = float(raw_initial)
    except ValueError:
        initial_seconds = _DEFAULT_INITIAL_SECONDS
    if initial_seconds <= 0:
        initial_seconds = _DEFAULT_INITIAL_SECONDS

    try:
        cap_seconds = float(raw_cap)
    except ValueError:
        cap_seconds = _DEFAULT_CAP_SECONDS
    # A cap below the initial wait would silently shorten the first retry below
    # its floor; clamp it up so the postcondition (cap >= initial) always holds.
    if cap_seconds < initial_seconds:
        cap_seconds = initial_seconds

    return max_retries, initial_seconds, cap_seconds


def rate_limit_retry_delay(
    failed_attempt_index: int,
    initial_seconds: float,
    cap_seconds: float,
    retry_after_seconds: float | None = None,
) -> float:
    """Seconds to wait before the next 429 retry.

    Exponential: ``base = initial_seconds * 2**failed_attempt_index``. Jitter is
    strictly ADDITIVE (``uniform(0, ...)``, capped at 2s) — it can only lengthen
    the wait, so the first retry (``index == 0``) is always ``>= initial_seconds``
    (the 300s floor is never violated). When ``retry_after_seconds`` is provided
    and positive (a provider ``Retry-After`` header), the wait is raised to at
    least that value. The result is finally capped at ``cap_seconds``.

    Preconditions:
        * ``failed_attempt_index >= 0`` — 0 after the first failure. A negative
          index is a programmer error and raises ``ValueError`` (NOT a silent
          env-style fallback).
        * ``initial_seconds > 0``.
        * ``cap_seconds >= initial_seconds``.
    Postconditions:
        * When ``retry_after_seconds`` is ``None``/non-positive, returns a value
          in ``[initial_seconds, cap_seconds]``.
        * When ``retry_after_seconds`` is positive, returns
          ``min(max(initial_progression + jitter, retry_after_seconds), cap_seconds)``.
        * Pure (no I/O); only raises ``ValueError`` on a precondition breach.
    """
    if failed_attempt_index < 0:
        raise ValueError(f"failed_attempt_index must be >= 0, got {failed_attempt_index}")
    if initial_seconds <= 0:
        raise ValueError(f"initial_seconds must be > 0, got {initial_seconds}")
    if cap_seconds < initial_seconds:
        raise ValueError(
            f"cap_seconds ({cap_seconds}) must be >= initial_seconds ({initial_seconds})"
        )

    base = initial_seconds * (2**failed_attempt_index)
    jitter = random.uniform(0, min(2.0, max(0.25, base * 0.1)))
    computed = base + jitter
    if retry_after_seconds is not None and retry_after_seconds > 0:
        computed = max(computed, retry_after_seconds)
    return min(computed, cap_seconds)
