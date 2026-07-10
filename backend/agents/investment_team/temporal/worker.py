"""Temporal worker bootstrap for the investment team.

Exposes a no-arg ``start_investment_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the Temporal worker (and its
connected client) is ready before uvicorn accepts requests. Mirrors
``user_agent_founder.temporal.worker.start_user_agent_founder_temporal_worker_thread``.
"""

from __future__ import annotations

import logging

from investment_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared_temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_investment_temporal_worker_thread() -> bool:
    """Start the investment Temporal workers (no-op when disabled).

    Boots two workers on distinct queues from this single entrypoint (the one the
    team_service entrypoint / lifespan backstop invokes), so every deployment path
    gets both:

      - the coarse ``investment-queue`` worker (this module's ``WORKFLOWS`` /
        ``ACTIVITIES`` — the ad hoc single-backtest ``InvestmentBacktestWorkflow``);
      - the fine-grained ``strategy-lab-queue`` worker
        (``StrategyLabBatchWorkflow`` + its ``StrategyLabCycleWorkflow`` children
        and every per-step activity), started via the strategy-lab package's own
        ``start_strategy_lab_temporal_worker_thread``.

    Preconditions:
        - None; safe to call unconditionally and repeatedly.

    Postconditions:
        - Returns ``True`` if at least one worker thread is running (or was already
          running — ``start_team_worker`` is idempotent per team), and ``False``
          when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).
    """
    if not is_temporal_enabled():
        return False
    investment_started = start_team_worker(
        "investment",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
    # Boot the Strategy Lab worker on its own queue from the same hook; imported
    # lazily to avoid dragging the strategy-lab graph into this module at import.
    from investment_team.strategy_lab.temporal.worker import (
        start_strategy_lab_temporal_worker_thread,
    )

    strategy_lab_started = start_strategy_lab_temporal_worker_thread()
    return investment_started or strategy_lab_started
