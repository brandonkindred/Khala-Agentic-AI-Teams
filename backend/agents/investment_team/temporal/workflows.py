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
* outcome determination (completed / cancelled / missing / failed) uses the
  worker return value from ``_run_backtest_background``, not a post-run
  job-store read;
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

import asyncio
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

# How often the activity heartbeats, mirroring paper_trading.py's driver. Wide
# enough to avoid heartbeat spam (a network round-trip) over a run that can
# last hours, but short enough that Temporal detects a worker crash well
# within a single retry cycle.
_HEARTBEAT_INTERVAL_S = 30.0
_HEARTBEAT_TIMEOUT = timedelta(seconds=120)

# How often cancellation is polled — deliberately decoupled from, and much
# tighter than, _HEARTBEAT_INTERVAL_S. is_cancelled() is a free in-memory
# flag read (no network round-trip), unlike activity.heartbeat(), so polling
# it on the network-heartbeat cadence would leave a run that finishes before
# the first heartbeat tick (or in the last _HEARTBEAT_INTERVAL_S of any run)
# with no chance to ever observe a mid-run cancellation and persist it as
# cancelled instead of completed. This narrows (does not fully close) that
# tail race — closing it fully would require either an internal checkpoint
# inside the (out-of-scope, non-preemptible) backtest engine itself, or a
# forced thread-cancel exception, which is unsafe here (see
# no_thread_cancel_exception below) — matching the same checkpoint-based,
# best-effort cancellation semantics _run_backtest_background already
# documents for the REST cancel route.
_CANCEL_POLL_INTERVAL_S = 0.25


# no_thread_cancel_exception=True: by default temporalio forcibly raises
# CancelledError inside this thread the instant the server's Cancel task is
# polled — decoupled from heartbeat timing, so it can (and typically does)
# arrive before this activity's own BackgroundHeartbeat-driven _bt_cancel_job
# call lands. CancelledError is Exception-subclassed, so an unguarded forced
# raise would be caught by _run_backtest_background's broad except-Exception
# handler and persist the job as failed instead of cancelled — the opposite
# of "honors cancellation". Disabling the forced raise makes cancellation
# purely cooperative: is_cancelled() still flips immediately, but only the
# _beat()-driven _bt_cancel_job()/checkpoint path can ever mark the job
# cancelled, matching the same checkpoint-based semantics as the existing
# REST cancel route.
@activity.defn(name="investment_run_backtest", no_thread_cancel_exception=True)
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
        - Otherwise two background loops run for the duration of the call: one
          beats on ``activity.heartbeat()`` (``_HEARTBEAT_INTERVAL_S``) so
          Temporal can detect a worker crash; a second, much faster one polls
          ``activity.is_cancelled()`` (``_CANCEL_POLL_INTERVAL_S`` — decoupled
          from the heartbeat cadence since it is a free in-memory check, not a
          network call). ``is_cancelled()`` is set for reasons beyond a real
          user cancel (heartbeat timeout, worker shutdown, pause, reset —
          see ``activity.cancellation_details()``); only when the details
          report ``cancel_requested`` does the poller cancel the job
          (``_bt_cancel_job``) so ``_run_backtest_background``'s own
          cancellation checkpoints see it and return ``cancelled`` at their
          next check rather than overwriting the job to completed/failed. A
          non-``cancel_requested`` reason (e.g. a heartbeat timeout from a
          crashed worker) leaves the job store untouched so a Temporal retry
          can still resume/redo the work instead of being permanently
          short-circuited as cancelled.
        - ``_run_backtest_background`` has run and persisted the job result;
          outcome is taken from the worker's return value (entry short-circuit
          still reads the job store). Raises ``ApplicationError`` if the worker
          returned ``failed`` (so Temporal retries within the bounded policy).
          If the worker returned ``cancelled`` (a user-initiated cancel during
          the run, including one driven by Temporal cancellation via the
          poller above), returns a status dict reporting ``cancelled`` rather
          than ``completed``. If the worker returned ``missing`` (the job row
          was deleted out from under it via ``DELETE /backtests/jobs/{job_id}``),
          returns a status dict reporting ``missing`` — distinct from
          ``cancelled`` since no cancellation actually happened, and there is
          no job row left to retry against. Returns a ``completed`` status
          dict when the worker returned ``completed``. Any other (non-terminal)
          return value is a postcondition violation of
          ``_run_backtest_background`` and raises ``ApplicationError`` rather
          than being silently reported as ``completed``.
    """
    from investment_team.api.main import (
        _BT_JOB_STATUS_CANCELLED,
        _BT_JOB_STATUS_COMPLETED,
        _BT_JOB_STATUS_FAILED,
        _BT_JOB_STATUS_MISSING,
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
        # test) this raises and BackgroundHeartbeat swallows it.
        activity.heartbeat()

    # Resolved at most once: cancellation_details() are "set once and do not
    # change once set" (temporalio), so a single check suffices — no need to
    # keep polling once is_cancelled() has flipped, regardless of the reason.
    _resolved = False

    def _watch_cancellation() -> None:
        nonlocal _resolved
        if _resolved or not activity.is_cancelled():
            return
        _resolved = True
        # is_cancelled() is a single shared flag for cancel_requested,
        # timed_out (heartbeat timeout), worker_shutdown, paused, and reset —
        # only cancel_requested is a genuine user/workflow-initiated cancel.
        # Any other reason must leave the job store alone so a Temporal retry
        # (e.g. after a crashed worker's heartbeat timeout) can still resume
        # the work instead of finding it pre-marked cancelled and bailing out
        # via _run_backtest_background's entry short-circuit.
        details = activity.cancellation_details()
        if details is not None and details.cancel_requested:
            _bt_cancel_job(job_id)

    with (
        BackgroundHeartbeat(
            _beat,
            _HEARTBEAT_INTERVAL_S,
            copy_context=True,
            name=f"backtest-hb-{job_id}",
        ),
        BackgroundHeartbeat(
            _watch_cancellation,
            _CANCEL_POLL_INTERVAL_S,
            copy_context=True,
            beat_first=True,
            name=f"backtest-cancel-{job_id}",
        ),
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
    if final_status == _BT_JOB_STATUS_MISSING:
        return {"job_id": job_id, "status": "missing"}
    if final_status == _BT_JOB_STATUS_COMPLETED:
        return {"job_id": job_id, "status": "completed"}
    raise ApplicationError(
        f"Backtest {job_id} ended in unexpected non-terminal status {final_status!r}",
        type="BacktestNonTerminal",
    )


@activity.defn(name="investment_mark_backtest_cancelled")
def mark_backtest_job_cancelled_activity(job_id: str) -> dict[str, Any]:
    """Persist a cancelled state for a job whose activity attempt never ran.

    Compensation for the race where the *workflow* itself is cancelled (e.g.
    directly via Temporal, independent of anything the REST API wires up)
    while ``run_backtest_activity`` is still merely *scheduled* — a busy task
    queue can leave it queued for a while. That activity never starts, so
    neither its heartbeat, its cancellation poller, nor
    ``_run_backtest_background``'s own checkpoints ever get a chance to
    observe the cancellation, and the job would otherwise sit pending/running
    forever (and keep appearing in ``running_only`` job listings).

    Preconditions:
        - ``job_id`` may or may not exist in the job store, and may already
          be in any status.

    Postconditions:
        - Idempotent and best-effort: cancels the job only if it is still
          pending/running (``_bt_cancel_job`` — an atomic conditional update
          that is a no-op, not an error, on a missing or already-terminal
          job, mirroring the REST cancel route's own semantics). Returns
          ``{"job_id", "status"}`` reflecting the job's status after the call
          (``"unknown"`` if the job row does not exist).
    """
    from investment_team.api.main import _backtest_job_status, _bt_cancel_job

    _bt_cancel_job(job_id)
    return {"job_id": job_id, "status": _backtest_job_status(job_id) or "unknown"}


# Bound, retrying policy for the short compensation activity (it only writes
# the job store, so a transient store error is worth a couple of retries) —
# mirrors paper_trading.py's own stop-compensation policy.
_CANCEL_COMPENSATION_RETRY = RetryPolicy(maximum_attempts=3)
_CANCEL_COMPENSATION_TIMEOUT = timedelta(seconds=30)


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
            - If the workflow itself is cancelled (e.g. directly via Temporal),
              ``execute_activity`` raises ``asyncio.CancelledError`` at this
              await point; runs ``mark_backtest_job_cancelled_activity`` as a
              best-effort compensation (a persistent failure there must not
              mask the original cancellation) before re-raising so the
              workflow still completes as cancelled.
        """
        try:
            return await workflow.execute_activity(
                run_backtest_activity,
                args=[job_id, strategy, config, submitted_by, notes],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            )
        except asyncio.CancelledError:
            try:
                await workflow.execute_activity(
                    mark_backtest_job_cancelled_activity,
                    args=[job_id],
                    start_to_close_timeout=_CANCEL_COMPENSATION_TIMEOUT,
                    retry_policy=_CANCEL_COMPENSATION_RETRY,
                )
            except Exception:
                workflow.logger.warning(
                    "mark_backtest_job_cancelled_activity failed for job %s; "
                    "job may remain non-terminal.",
                    job_id,
                )
            raise
