"""Rate-limit (HTTP 429) backoff policy — the single home of the slow schedule.

A 429 from an LLM provider means the account/budget is exhausted and will not
reset in seconds. Retrying it on the same fast schedule used for transient
5xx/network faults (``LLM_BACKOFF_*``) just burns attempts against an exhausted
budget. This module owns a SEPARATE schedule whose first retry waits tens of
seconds (default 30s), doubling up to a cap (default 120s) for a few retries,
so a rate-limited call fails within a few minutes instead of hanging while the
provider budget stays exhausted.

Both retry layers consume this one policy:
  * ``llm_service.clients.ollama.OllamaLLMClient._ollama_post`` (every team's
    central client path), and
  * ``investment_team.strategy_lab.agents._llm_envelope.invoke_agent`` (the
    Strategy Lab agents that build strands models directly and bypass the
    central client).

The retry/backoff math itself lives in ``shared.http.retry`` (generic, usable
by non-LLM HTTP clients too); this module is a thin LLM-flavored wrapper that
supplies the ``LLM_RATE_LIMIT_*`` env var names and defaults, and preserves the
names both consumers above already import.
"""

from __future__ import annotations

from shared.http.retry import backoff_sleep, parse_retry_env_config, retry_delay

from . import config as llm_config

__all__ = [
    "parse_rate_limit_retry_config",
    "rate_limit_backoff_sleep",
    "rate_limit_retry_delay",
]

# Defaults: first 429 retry at 30s, doubling, capped at 120s, 3 retries (4 total
# attempts) => worst-case ~3.6 min of waiting before raising (hard ceiling
# retries*cap = 6 min). Kept short so a rate-limited call fails fast and lets the
# caller (failover / the review coordinator) take over instead of hanging; a
# provider that truly needs long waits can raise the env overrides below.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_INITIAL_SECONDS = 30.0
_DEFAULT_CAP_SECONDS = 120.0


def parse_rate_limit_retry_config() -> tuple[int, float, float]:
    """Parse the rate-limit backoff env vars.

    Returns ``(max_retries, initial_seconds, cap_seconds)`` for the 429 schedule.
    Delegates to ``shared.http.retry.parse_retry_env_config`` with the
    ``LLM_RATE_LIMIT_*`` env var names and this module's defaults.

    Preconditions: none.
    Postconditions: returns a 3-tuple with ``max_retries >= 0``,
    ``initial_seconds > 0``, ``cap_seconds >= initial_seconds``; never raises.
    """
    return parse_retry_env_config(
        llm_config.ENV_LLM_RATE_LIMIT_MAX_RETRIES,
        llm_config.ENV_LLM_RATE_LIMIT_BACKOFF_INITIAL,
        llm_config.ENV_LLM_RATE_LIMIT_BACKOFF_MAX,
        default_max_retries=_DEFAULT_MAX_RETRIES,
        default_initial_seconds=_DEFAULT_INITIAL_SECONDS,
        default_cap_seconds=_DEFAULT_CAP_SECONDS,
    )


def rate_limit_retry_delay(
    failed_attempt_index: int,
    initial_seconds: float,
    cap_seconds: float,
    retry_after_seconds: float | None = None,
) -> float:
    """Seconds to wait before the next 429 retry.

    Delegates to ``shared.http.retry.retry_delay`` — see that function for the
    full contract (exponential schedule, additive jitter, optional
    ``retry_after_seconds`` floor, capped at ``cap_seconds``).

    Preconditions:
        * ``failed_attempt_index >= 0``.
        * ``initial_seconds > 0``.
        * ``cap_seconds >= initial_seconds``.
    Postconditions: same as ``shared.http.retry.retry_delay``.
    """
    return retry_delay(failed_attempt_index, initial_seconds, cap_seconds, retry_after_seconds)


def rate_limit_backoff_sleep(
    attempt: int,
    max_retries: int,
    initial_seconds: float,
    cap_seconds: float,
    retry_after_seconds: float | None = None,
    *,
    provider: str = "LLM",
    request_id: str = "-",
    context: str = "",
) -> float:
    """Compute the 429 backoff for ``attempt``, log one warning, and sleep it.

    Single home for the "wait, warn, sleep" step shared by the Claude and Ollama
    rate-limit retry loops. Delegates to ``shared.http.retry.backoff_sleep``.

    Preconditions:
        * ``0 <= attempt < max_retries`` — the caller enforces the retry-budget
          check before calling; ``attempt``/``max_retries`` only shape the log line.
        * The caller has already exited any concurrency semaphore / HTTP stream
          context — this sleep can be minutes long and must not hold a shared
          resource.
        * ``initial_seconds > 0``; ``cap_seconds >= initial_seconds``.
    Postconditions: sleeps ``rate_limit_retry_delay(attempt, initial_seconds,
        cap_seconds, retry_after_seconds)`` seconds and returns that value; emits
        exactly one warning. Only raises ``ValueError`` on a
        ``rate_limit_retry_delay`` precondition breach.
    """
    return backoff_sleep(
        attempt,
        max_retries,
        initial_seconds,
        cap_seconds,
        retry_after_seconds,
        provider=provider,
        request_id=request_id,
        context=context,
    )
