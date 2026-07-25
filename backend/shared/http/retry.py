"""Generic HTTP retry/backoff policy — jittered exponential schedule with an
optional ``Retry-After``-style floor.

Extracted from ``llm_service.backoff``, which owned the LLM rate-limit (429)
schedule. The math here has no LLM-specific behavior: it takes env var names
and default values as parameters rather than reading them itself, so any HTTP
client (LLM or otherwise, e.g. a GitHub API client) can drive its own retry
loop off this one implementation instead of hand-rolling the same
exponential-backoff-with-jitter formula.

This is a leaf module: stdlib only, so it cannot create an import cycle with
any consumer.
"""

from __future__ import annotations

import logging
import os
import random
import time

logger = logging.getLogger(__name__)

__all__ = [
    "parse_retry_env_config",
    "backoff_sleep",
    "retry_delay",
]


def parse_retry_env_config(
    max_retries_env: str,
    initial_seconds_env: str,
    cap_seconds_env: str,
    *,
    default_max_retries: int,
    default_initial_seconds: float,
    default_cap_seconds: float,
) -> tuple[int, float, float]:
    """Parse a retry/backoff env-var triple into ``(max_retries, initial_seconds,
    cap_seconds)``.

    Each value is read independently and a non-integer/non-float (or empty,
    or unset) env falls back to its caller-supplied default rather than
    raising — env misconfiguration must not crash the caller.

    Preconditions:
        * ``default_max_retries >= 0``, ``default_initial_seconds > 0``,
          ``default_cap_seconds >= default_initial_seconds`` — the caller's
          own defaults must already be internally consistent.
    Postconditions:
        * Returns a 3-tuple with ``max_retries >= 0``, ``initial_seconds > 0``,
          ``cap_seconds >= initial_seconds``; never raises.
    """
    assert default_max_retries >= 0, f"default_max_retries must be >= 0, got {default_max_retries}"
    assert default_initial_seconds > 0, f"default_initial_seconds must be > 0, got {default_initial_seconds}"
    assert default_cap_seconds >= default_initial_seconds, (
        f"default_cap_seconds ({default_cap_seconds}) must be >= default_initial_seconds ({default_initial_seconds})"
    )

    raw_retries = os.environ.get(max_retries_env) or str(default_max_retries)
    raw_initial = os.environ.get(initial_seconds_env) or str(default_initial_seconds)
    raw_cap = os.environ.get(cap_seconds_env) or str(default_cap_seconds)

    try:
        max_retries = max(0, int(raw_retries))
    except ValueError:
        max_retries = default_max_retries

    try:
        initial_seconds = float(raw_initial)
    except ValueError:
        initial_seconds = default_initial_seconds
    if initial_seconds <= 0:
        initial_seconds = default_initial_seconds

    try:
        cap_seconds = float(raw_cap)
    except ValueError:
        cap_seconds = default_cap_seconds
    # A cap below the initial wait would silently shorten the first retry below
    # its floor; clamp it up so the postcondition (cap >= initial) always holds.
    if cap_seconds < initial_seconds:
        cap_seconds = initial_seconds

    return max_retries, initial_seconds, cap_seconds


def retry_delay(
    failed_attempt_index: int,
    initial_seconds: float,
    cap_seconds: float,
    retry_after_seconds: float | None = None,
) -> float:
    """Seconds to wait before the next retry.

    Exponential: ``base = initial_seconds * 2**failed_attempt_index``. Jitter is
    strictly ADDITIVE (``uniform(0, ...)``, capped at 2s) — it can only lengthen
    the wait, so the first retry (``index == 0``) is always ``>= initial_seconds``
    (the initial floor is never violated). When ``retry_after_seconds`` is provided
    and positive (e.g. a provider ``Retry-After`` header), the wait is raised to at
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
        raise ValueError(f"cap_seconds ({cap_seconds}) must be >= initial_seconds ({initial_seconds})")

    base = initial_seconds * (2**failed_attempt_index)
    jitter = random.uniform(0, min(2.0, max(0.25, base * 0.1)))
    computed = base + jitter
    if retry_after_seconds is not None and retry_after_seconds > 0:
        computed = max(computed, retry_after_seconds)
    return min(computed, cap_seconds)


def backoff_sleep(
    attempt: int,
    max_retries: int,
    initial_seconds: float,
    cap_seconds: float,
    retry_after_seconds: float | None = None,
    *,
    provider: str = "HTTP",
    request_id: str = "-",
    context: str = "",
) -> float:
    """Compute the backoff for ``attempt``, log one warning, and sleep it.

    Single home for the "wait, warn, sleep" step, so the schedule, the log
    line, and the sleep have one implementation shared across callers (e.g.
    an LLM client's 429 retries, or a REST API client's rate-limit retries).

    Preconditions:
        * ``0 <= attempt < max_retries`` — the caller enforces the retry-budget
          check before calling; ``attempt``/``max_retries`` only shape the log line.
        * The caller has already exited any concurrency semaphore / HTTP stream
          context — this sleep can be minutes long and must not hold a shared
          resource.
        * ``initial_seconds > 0``; ``cap_seconds >= initial_seconds``.
    Postconditions: sleeps ``retry_delay(attempt, initial_seconds, cap_seconds,
        retry_after_seconds)`` seconds and returns that value; emits exactly one
        warning. Only raises ``ValueError`` on a ``retry_delay`` precondition
        breach.
    """
    wait = retry_delay(attempt, initial_seconds, cap_seconds, retry_after_seconds)
    ctx = f", {context}" if context else ""
    logger.warning(
        "%s rate-limited (rid=%s%s, retry attempt %d/%d). Retrying in %.1fs",
        provider,
        request_id,
        ctx,
        attempt + 1,
        max_retries + 1,
        wait,
    )
    time.sleep(wait)
    return wait
