"""Temporal workflows + activities for the investment team.

Sandbox-safe: the temporalio workflow sandbox re-imports this module to register
the workflow classes, so nothing here may invoke restricted builtins
(``os.getenv``, time/random, …) at module top level. The heavy lifting — running
the team's existing background workers — happens inside the activity bodies,
which execute *outside* the sandbox and may freely use threads/IO.

This module now holds only the ad hoc single-backtest path:

* :class:`InvestmentBacktestWorkflow` → ``_run_backtest_background`` (single backtest)

The Strategy Lab batch run is no longer wrapped as one coarse activity here — it
is driven by the fine-grained ``StrategyLabBatchWorkflow`` /
``StrategyLabCycleWorkflow`` (``investment_team.strategy_lab.temporal``) on the
dedicated ``strategy-lab-queue``, so every cycle/LLM-call/backtest is an
individually-retryable Temporal unit rather than one multi-hour activity.

Durability contract. The backtest activity is written to be safe under Temporal's
retry (which fires on worker crash / start_to_close timeout):

* it rehydrates the in-memory job entry from the durable job store, so progress is
  tracked even when the retry lands in a fresh process after a restart;
* it short-circuits a job that already completed — so a retry does not duplicate
  work;
* a worker-level failure is re-raised as an ``ApplicationError`` so Temporal sees
  the failure (and retries within the bounded policy) instead of the swallowed
  exception being reported as success.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

# Bounded retry: transient infra failures (worker crash, timeout) get a few
# resume attempts; a deterministic failure exhausts them and fails the workflow
# (visible in the Temporal UI) rather than retrying forever. The activities are
# idempotent-on-retry (resume offset / completed short-circuit), so retries do
# not duplicate work.
_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)

# A backtest runs a full walk-forward sweep; give the activity a wide ceiling so
# Temporal does not time it out.
_ACTIVITY_TIMEOUT = timedelta(hours=6)


@activity.defn(name="investment_run_backtest")
def run_backtest_activity(
    job_id: str,
    strategy: dict[str, Any],
    config: dict[str, Any],
    submitted_by: str,
    notes: list[str],
) -> dict[str, Any]:
    """Run one backtest job to completion inside a Temporal activity.

    Preconditions:
        - ``job_id`` names a backtest job already created in the job store.
        - ``strategy`` / ``config`` are JSON-round-tripped ``StrategySpec`` /
          ``BacktestConfig`` payloads.

    Postconditions:
        - Short-circuits (no recompute) when the job already completed, so a
          retry whose predecessor finished does not orphan a duplicate record.
        - Otherwise ``_run_backtest_background`` has run and persisted the job
          result; raises ``ApplicationError`` if the job ended ``failed`` (so
          Temporal retries within the bounded policy). Returns a status dict.
    """
    from investment_team.api.main import (
        _BT_JOB_STATUS_CANCELLED,
        _BT_JOB_STATUS_COMPLETED,
        _BT_JOB_STATUS_FAILED,
        _backtest_job_status,
        _run_backtest_background,
    )
    from investment_team.models import BacktestConfig, StrategySpec
    

    current_status = _backtest_job_status(job_id)
    if current_status == _BT_JOB_STATUS_COMPLETED:
        return {"job_id": job_id, "status": "completed"}
    elif current_status == _BT_JOB_STATUS_CANCELLED:
        return {"job_id": job_id, "status": "cancelled"}

    _run_backtest_background(
        job_id,
        StrategySpec(**strategy),
        BacktestConfig(**config),
        submitted_by,
        notes,
    )
    max_polling_attempts = 30
    attempts = 0
    
    while attempts < max_polling_attempts:
        attempts += 1
        final_status = _backtest_job_status(job_id)
        
        if final_status == _BT_JOB_STATUS_FAILED:
            # This will properly raise the error the test expects
            raise ApplicationError(f"Backtest {job_id} failed", type="BacktestFailed")
            
        elif final_status == _BT_JOB_STATUS_CANCELLED:
            return {"job_id": job_id, "status": "cancelled"}
            
        elif final_status == _BT_JOB_STATUS_COMPLETED:
            return {"job_id": job_id, "status": "completed"}
            
        # Synchronous sleep safely blocks the loop without throwing an async SyntaxError
        time.sleep(1)
        
    # Fail-safe termination if the loop completes without a valid status
    # ✅ ADD THIS


    raise ApplicationError(
        f"Backtest {job_id} polling exceeded deadline",
        type="BacktestPollingTimeout",
        non_retryable=True,
    )


@workflow.defn(name="InvestmentBacktestWorkflow")
class InvestmentBacktestWorkflow:
    """Durable wrapper around a single backtest job."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        strategy: dict[str, Any],
        config: dict[str, Any],
        submitted_by: str,
        notes: list[str],
    ) -> dict[str, Any]:
        """Execute the backtest activity durably.

        Preconditions:
            - Arguments satisfy ``run_backtest_activity``'s preconditions.

        Postconditions:
            - Returns the activity result, retrying per ``_ACTIVITY_RETRY`` on
              failure.
        """
        return await workflow.execute_activity(
            run_backtest_activity,
            args=[job_id, strategy, config, submitted_by, notes],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
