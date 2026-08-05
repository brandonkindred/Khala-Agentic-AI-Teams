"""Temporal worker bootstrap for the investment team.

Exposes a no-arg ``start_investment_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the Temporal worker (and its
connected client) is ready before uvicorn accepts requests. Mirrors
``agent_team_studio.user_agent_founder.temporal.worker.start_user_agent_founder_temporal_worker_thread``.
"""

from __future__ import annotations

import logging
import os

from investment_team.temporal import (
    ACTIVITIES,
    ADVISORY_ACTIVITIES,
    ADVISORY_TASK_QUEUE,
    ADVISORY_WORKFLOWS,
    TASK_QUEUE,
    WORKFLOWS,
)
from shared.temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def _max_concurrent_activities() -> int:
    """Resolve the per-worker activity concurrency for ``investment-queue``.

    The shared framework default (4) is sized for short activities. This queue
    now also carries ``run_paper_trading_activity``, which can hold a worker
    thread for hours (up to ``max_hours``) — at the default cap, four
    concurrent live paper-trading sessions (well within the per-strategy
    concurrency guard's headroom) would fully saturate the pool and silently
    queue any backtest or paper-trading dispatch behind them for hours. Default
    higher here and let operators tune via
    ``INVESTMENT_MAX_CONCURRENT_ACTIVITIES``, mirroring
    ``strategy_lab.temporal.worker``'s equivalent knob.

    Postconditions:
        Returns an int ≥ 1 (garbage / out-of-range env → default 8, floored at 1).
    """
    raw = os.environ.get("INVESTMENT_MAX_CONCURRENT_ACTIVITIES", "8")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 8


def start_investment_temporal_worker_thread() -> bool:
    """Start the investment Temporal workers (no-op when disabled).

    Boots three workers on distinct queues from this single entrypoint (the one
    the team_service entrypoint / lifespan backstop invokes), so every deployment
    path gets all of them:

      - the coarse ``investment-queue`` worker (this module's ``WORKFLOWS`` /
        ``ACTIVITIES`` — the ad hoc single-backtest ``InvestmentBacktestWorkflow``
        and the long-running ``PaperTradingWorkflow``);
      - the fine-grained ``strategy-lab-queue`` worker
        (``StrategyLabBatchWorkflow`` + its ``StrategyLabCycleWorkflow`` children
        and every per-step activity), started via the strategy-lab package's own
        ``start_strategy_lab_temporal_worker_thread``;
      - the ``investment-advisory-queue`` worker (the interactive
        proposal/validation/promotion/memo/advisor workflows), on its own team
        key so short calls never queue behind a multi-hour backtest activity.

    Each queue uses a distinct team key because ``start_team_worker`` is
    idempotent *per team key*.

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
        max_concurrent_activities=_max_concurrent_activities(),
    )
    advisory_started = start_team_worker(
        "investment_advisory",
        ADVISORY_WORKFLOWS,
        ADVISORY_ACTIVITIES,
        task_queue=ADVISORY_TASK_QUEUE,
    )
    # Boot the Strategy Lab worker on its own queue from the same hook; imported
    # lazily to avoid dragging the strategy-lab graph into this module at import.
    from investment_team.strategy_lab.temporal.worker import (
        start_strategy_lab_temporal_worker_thread,
    )

    strategy_lab_started = start_strategy_lab_temporal_worker_thread()
    return investment_started or advisory_started or strategy_lab_started
