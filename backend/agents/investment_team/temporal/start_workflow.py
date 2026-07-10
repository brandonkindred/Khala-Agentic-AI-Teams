"""Start investment Temporal workflows from synchronous API code.

Thin wrappers over ``shared_temporal.start_workflow_sync`` — the shared
sync→async bridge that waits for the worker's connected client, then schedules
``client.start_workflow`` on the worker loop. These helpers do NOT touch the job
store: the API handlers own their own run/job bookkeeping (active-run registry,
``_persist_run_state`` / ``_bt_create_job``).
"""

from __future__ import annotations

import logging
from typing import Any

from investment_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    InvestmentBacktestWorkflow,
)
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)

# The Strategy Lab run is now started via
# ``investment_team.strategy_lab.temporal.start_workflow.start_strategy_lab_batch_workflow``
# (the fine-grained ``StrategyLabBatchWorkflow`` on ``strategy-lab-queue``); the
# old coarse ``start_strategy_lab_workflow`` here has been removed in the cutover.


def start_backtest_workflow(
    job_id: str,
    strategy: Any,
    config: Any,
    submitted_by: str,
    notes: list[str],
) -> None:
    """Start :class:`InvestmentBacktestWorkflow` for a backtest job.

    Preconditions:
        - ``job_id`` is non-empty and already created in the job store.
        - ``strategy`` / ``config`` are ``StrategySpec`` / ``BacktestConfig``
          instances (expose ``model_dump``).
        - Temporal is enabled and the investment worker is running.

    Postconditions:
        - The workflow is started under id ``investment-bt-{job_id}`` on the
          investment task queue. Raises ``RuntimeError`` if the worker client
          never becomes available.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}bt-{job_id}"
    start_workflow_sync(
        InvestmentBacktestWorkflow.run,
        job_id,
        strategy.model_dump(mode="json"),
        config.model_dump(mode="json"),
        submitted_by,
        notes,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started InvestmentBacktestWorkflow id=%s", workflow_id)
