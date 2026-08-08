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
  work (entry short-circuit reads the job store);
* outcome determination (completed / cancelled / failed) uses the worker return
  value from ``_run_backtest_background``, not a post-run job-store read;
* a worker-level failure is re-raised as an ``ApplicationError`` so Temporal sees
  the failure (and retries within the bounded policy) instead of the swallowed
  exception being reported as success;
* a background heartbeat (``shared.concurrency.BackgroundHeartbeat``, the same
  driver ``paper_trading.run_paper_trading_activity`` uses) beats for the
  duration of the run so Temporal can detect a worker crash during a run that
  can last hours, and — on Temporal cancellation — cancels the underlying job so
  ``_run_backtest_background``'s own cancellation checkpoints observe and honor
  it.
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

# A backtest runs a full walk-forward sweep; give the activity a wide ceiling so
# Temporal does not time it out.
_ACTIVITY_TIMEOUT = timedelta(hours=6)

# How often the activity heartbeats (and re-checks for Temporal cancellation),
# mirroring paper_trading.py's driver. Wide enough to avoid heartbeat spam over
# a run that can last hours, but short enough that Temporal detects a worker
# crash well within a single retry cycle.
_HEARTBEAT_INTERVAL_S = 30.0
_HEARTBEAT_TIMEOUT = timedelta(seconds=120)


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
        - Otherwise a background heartbeat runs for the duration of the call:
          it beats on ``activity.heartbeat()`` so Temporal can detect a worker
          crash, and, once ``activity.is_cancelled()`` observes a Temporal
          cancellation, cancels the job (``_bt_cancel_job``) so
          ``_run_backtest_background``'s own cancellation checkpoints see it and
          return ``cancelled`` at their next check rather than overwriting the
          job to completed/failed.
        - ``_run_backtest_background`` has run and persisted the job result;
          outcome is taken from the worker's return value (entry short-circuit
          still reads the job store). Raises ``ApplicationError`` if the worker
          returned ``failed`` (so Temporal retries within the bounded policy).
          If the worker returned ``cancelled`` (a user-initiated cancel during
          the run, including one driven by Temporal cancellation via the
          heartbeat above), returns a status dict reporting ``cancelled`` rather
          than ``completed``. Returns a ``completed`` status dict when the
          worker returned ``completed``. Any other (non-terminal) return value
          is a postcondition violation of ``_run_backtest_background`` and
          raises ``ApplicationError`` rather than being silently reported as
          ``completed``.
    """
    from investment_team.api.main import (
        _BT_JOB_STATUS_CANCELLED,
        _BT_JOB_STATUS_COMPLETED,
        _BT_JOB_STATUS_FAILED,
        _backtest_job_status,
        _bt_cancel_job,
        _run_backtest_background,
    )
    from investment_team.models import BacktestConfig, StrategySpec
    from shared.concurrency import BackgroundHeartbeat

    if _backtest_job_status(job_id) == _BT_JOB_STATUS_COMPLETED:
        return {"job_id": job_id, "status": "completed"}

    def _beat() -> None:
        # Best-effort: outside a real activity context (e.g. a direct-call unit
        # test) these raise and BackgroundHeartbeat swallows them.
        activity.heartbeat()
        if activity.is_cancelled():
            _bt_cancel_job(job_id)

    with BackgroundHeartbeat(
        _beat,
        _HEARTBEAT_INTERVAL_S,
        copy_context=True,
        name=f"backtest-hb-{job_id}",
    ):
        final_status = _run_backtest_background(
            job_id,
            StrategySpec(**strategy),
            BacktestConfig(**config),
            submitted_by,
            notes,
        )

    if final_status == _BT_JOB_STATUS_FAILED:
        raise ApplicationError(f"Backtest {job_id} failed", type="BacktestFailed")
    if final_status == _BT_JOB_STATUS_CANCELLED:
        return {"job_id": job_id, "status": "cancelled"}
    if final_status == _BT_JOB_STATUS_COMPLETED:
        return {"job_id": job_id, "status": "completed"}
    raise ApplicationError(
        f"Backtest {job_id} ended in unexpected non-terminal status {final_status!r}",
        type="BacktestNonTerminal",
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
              failure. ``heartbeat_timeout`` is set so Temporal treats a silent
              activity (no heartbeat within the window — e.g. a crashed worker)
              as failed rather than waiting the full ``_ACTIVITY_TIMEOUT``.
        """
        return await workflow.execute_activity(
            run_backtest_activity,
            args=[job_id, strategy, config, submitted_by, notes],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
