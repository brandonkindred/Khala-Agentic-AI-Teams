"""Strategy Lab concurrency/config constants — the sole source of truth for
``RunStrategyLabRequest``'s schema bounds and the thread-mode/Temporal
concurrency clamp.

Extracted from ``investment_team.api.main`` so ``api.main`` and
``strategy_lab.temporal.start_workflow`` both read the same constants instead
of the Temporal dispatch path importing private names from ``api.main``.
Import-time side-effect-free beyond reading env vars (matches the constants'
prior behavior in ``api.main``).
"""

from __future__ import annotations

import logging

from shared.env_config import env_int

logger = logging.getLogger(__name__)


def env_positive_int(name: str, default: int) -> int:
    """Parse a positive-int env var, falling back to ``default`` on any issue.

    Preconditions:
        ``default`` is a positive int.
    Postconditions:
        Returns the parsed value of env var ``name`` when it is a valid
        integer ``>= 1``; otherwise returns ``default`` (logging a warning
        when the env var was set but invalid). Never raises.
    """
    value = env_int(name, default)
    if value < 1:
        logger.warning("%s=%d is < 1; falling back to default %d", name, value, default)
        return default
    return value


# Upper bound on batch_count for Strategy Lab runs. Evaluated at import time so
# it becomes the Pydantic Field `le=` constraint; operators can override via env.
MAX_BATCH_COUNT = env_positive_int("STRATEGY_LAB_MAX_BATCH_COUNT", 100)

# Upper bound on the request's ``max_parallel`` field (its Pydantic ``le=``).
# Evaluated at import like ``MAX_BATCH_COUNT`` so operators can raise the schema
# ceiling on larger hosts without a code change; kept as a single named constant
# so the concurrency-cap default below cannot silently drift from the schema max.
MAX_PARALLEL = env_positive_int("STRATEGY_LAB_MAX_PARALLEL", 6)

# Upper bound on the request's ``paper_trading_lookback_days`` field (its
# Pydantic ``le=``). Evaluated at import like ``MAX_BATCH_COUNT`` so operators
# can raise the schema ceiling without a code change; the default (10 years)
# is generous enough for any real backtest window while still bounding the
# market-data fetch size a single request can trigger.
MAX_PAPER_TRADING_LOOKBACK_DAYS = env_positive_int(
    "STRATEGY_LAB_MAX_PAPER_TRADING_LOOKBACK_DAYS", 3650
)

# Hard ceiling on how many Strategy Lab cycles run concurrently per wave,
# independent of the request's ``max_parallel``. Each concurrent cycle holds its
# own market data + LLM contexts in the single worker process, so on a
# memory-constrained host this caps the worker's peak footprint (the dominant
# driver of OOM kills). Defaults to ``MAX_PARALLEL`` (the request field's max) so
# by default it adds no extra constraint and request validation stays the primary
# limit; operators opt into tighter caps via env.
MAX_CONCURRENT_CYCLES = env_positive_int("STRATEGY_LAB_MAX_CONCURRENT_CYCLES", MAX_PARALLEL)


def clamp_max_parallel(requested: int) -> int:
    """Clamp a request's ``max_parallel`` to the env-configured concurrency cap.

    Preconditions:
        - ``requested >= 1``.
    Postconditions:
        - Returns ``min(requested, MAX_CONCURRENT_CYCLES)``; logs an INFO only
          when the cap actually lowers the requested value. No other side effects.
    """
    effective = min(requested, MAX_CONCURRENT_CYCLES)
    if effective < requested:
        logger.info(
            "Strategy Lab concurrency capped to %d (requested %d) via "
            "STRATEGY_LAB_MAX_CONCURRENT_CYCLES",
            effective,
            requested,
        )
    return effective


__all__ = [
    "env_positive_int",
    "MAX_BATCH_COUNT",
    "MAX_PARALLEL",
    "MAX_CONCURRENT_CYCLES",
    "MAX_PAPER_TRADING_LOOKBACK_DAYS",
    "clamp_max_parallel",
]
