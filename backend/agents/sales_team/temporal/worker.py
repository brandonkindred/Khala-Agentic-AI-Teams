"""Temporal worker bootstrap for the sales_team.

Exposes a no-arg ``start_sales_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` env vars,
so the Temporal worker (and its connected client) is ready before uvicorn
starts accepting requests.

This shape mirrors ``market_research_team.temporal.worker.
start_market_research_temporal_worker_thread`` — same contract, same boot
hook in docker-compose.
"""

from __future__ import annotations

import logging

from sales_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared.env_config import env_int
from shared.temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)

# Default concurrent-activity ceiling for the sales worker. The pipeline now
# fans each stage out into one activity per prospect, so throughput is bounded
# by this rather than the old in-process thread pool; the default matches
# ``SalesPipelineConfig.pipeline_stage_workers`` (8) so wall-clock is preserved.
_DEFAULT_MAX_CONCURRENT_ACTIVITIES = 8


def _max_concurrent_activities() -> int:
    """Resolve the worker's concurrent-activity ceiling from the environment.

    Preconditions:
        - none (environment may be unset or garbage).
    Postconditions:
        - Returns the parsed env value when set and parseable; unset or
          unparseable → default ``8``; parsed but ``< 1`` → floored to ``1``
          via ``env_int(..., floor=1)`` (which warns on a set-but-unparseable
          value).
    """
    return env_int(
        "SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES",
        _DEFAULT_MAX_CONCURRENT_ACTIVITIES,
        floor=1,
    )


def start_sales_temporal_worker_thread() -> bool:
    """Start the sales_team Temporal worker (no-op when disabled).

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already
          running), ``False`` when Temporal is disabled
          (``TEMPORAL_ADDRESS`` unset).

    Safe to call multiple times — the underlying ``start_team_worker`` is
    idempotent per team.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "sales",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
        max_concurrent_activities=_max_concurrent_activities(),
    )
