"""Temporal workflows + activities for the investment team.

Sandbox-safe: the temporalio workflow sandbox re-imports this module to register
the workflow classes, so nothing here may invoke restricted builtins
(``os.getenv``, time/random, …) at module top level. The heavy lifting — running
the team's existing background workers — happens inside the activity bodies,
which execute *outside* the sandbox and may freely use threads/IO.

Each workflow wraps one of the investment team's long-running jobs, which are
otherwise dispatched via daemon threads in :mod:`investment_team.api.main`:

* :class:`InvestmentStrategyLabWorkflow` → ``_strategy_lab_worker`` (Strategy Lab batch)
* :class:`InvestmentBacktestWorkflow`     → ``_run_backtest_background`` (single backtest)

Durability contract. The activities are written to be safe under Temporal's
retry (which fires on worker crash / start_to_close timeout):

* they rehydrate the in-memory run entry from the durable job store, so progress
  is tracked even when the retry lands in a fresh process after a restart;
* the Strategy Lab activity resumes from the persisted contiguous-cycle offset
  instead of replaying completed cycles, and the backtest activity short-circuits
  a job that already completed — so a retry does not duplicate work;
* a worker-level failure is re-raised as an ``ApplicationError`` so Temporal sees
  the failure (and retries within the bounded policy) instead of the swallowed
  exception being reported as success.
"""

from __future__ import annotations

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

# Strategy Lab batches run many cycles and backtests run a full walk-forward
# sweep; give the activity a wide ceiling so Temporal does not time it out.
_ACTIVITY_TIMEOUT = timedelta(hours=6)


@activity.defn(name="investment_run_strategy_lab")
def run_strategy_lab_activity(run_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run one Strategy Lab batch to completion inside a Temporal activity.

    Preconditions:
        - ``run_id`` is a non-empty run identifier whose state was persisted to
          the job store before dispatch.
        - ``request`` is a JSON-round-tripped ``RunStrategyLabRequest`` payload.

    Postconditions:
        - ``_strategy_lab_worker`` has run to completion, resuming from the
          persisted contiguous-cycle offset (so a retry does not replay finished
          cycles) with the in-memory run entry rehydrated so progress is tracked.
        - Raises ``ApplicationError`` if the run ended in a hard ``failed`` state
          (so Temporal retries within the bounded policy); otherwise returns a
          small status dict.
    """
    from investment_team.api.main import (
        RunStrategyLabRequest,
        _rehydrate_active_run_offset,
        _strategy_lab_run_failure,
        _strategy_lab_worker,
    )

    offset = _rehydrate_active_run_offset(run_id)
    _strategy_lab_worker(run_id, RunStrategyLabRequest(**request), start_cycle_offset=offset)

    failure = _strategy_lab_run_failure(run_id)
    if failure is not None:
        raise ApplicationError(
            f"Strategy Lab run {run_id} failed: {failure}",
            type="StrategyLabRunFailed",
        )
    return {"run_id": run_id, "status": "completed"}


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
        _BT_JOB_STATUS_COMPLETED,
        _BT_JOB_STATUS_FAILED,
        _backtest_job_status,
        _run_backtest_background,
    )
    from investment_team.models import BacktestConfig, StrategySpec

    if _backtest_job_status(job_id) == _BT_JOB_STATUS_COMPLETED:
        return {"job_id": job_id, "status": "completed"}

    _run_backtest_background(
        job_id,
        StrategySpec(**strategy),
        BacktestConfig(**config),
        submitted_by,
        notes,
    )

    if _backtest_job_status(job_id) == _BT_JOB_STATUS_FAILED:
        raise ApplicationError(f"Backtest {job_id} failed", type="BacktestFailed")
    return {"job_id": job_id, "status": "completed"}


@workflow.defn(name="InvestmentStrategyLabWorkflow")
class InvestmentStrategyLabWorkflow:
    """Durable wrapper around a Strategy Lab batch run."""

    @workflow.run
    async def run(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the Strategy Lab activity durably.

        Preconditions:
            - ``run_id`` / ``request`` satisfy ``run_strategy_lab_activity``'s
              preconditions.

        Postconditions:
            - Returns the activity result, retrying per ``_ACTIVITY_RETRY`` on
              failure.
        """
        return await workflow.execute_activity(
            run_strategy_lab_activity,
            args=[run_id, request],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
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
