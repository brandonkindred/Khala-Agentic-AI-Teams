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
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="investment_run_strategy_lab")
def run_strategy_lab_activity(run_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run one Strategy Lab batch to completion inside a Temporal activity.

    Preconditions:
        - ``run_id`` is a non-empty run identifier already registered in the API
          process's active-run registry / job store.
        - ``request`` is a JSON-round-tripped ``RunStrategyLabRequest`` payload.

    Postconditions:
        - ``_strategy_lab_worker`` has run to completion (all batches/cycles
          processed and run state persisted). Returns a small status dict.
    """
    from investment_team.api.main import RunStrategyLabRequest, _strategy_lab_worker

    _strategy_lab_worker(run_id, RunStrategyLabRequest(**request))
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
        - ``_run_backtest_background`` has run to completion and persisted the
          job result. Returns a small status dict.
    """
    from investment_team.api.main import _run_backtest_background
    from investment_team.models import BacktestConfig, StrategySpec

    _run_backtest_background(
        job_id,
        StrategySpec(**strategy),
        BacktestConfig(**config),
        submitted_by,
        notes,
    )
    return {"job_id": job_id, "status": "completed"}


@workflow.defn(name="InvestmentStrategyLabWorkflow")
class InvestmentStrategyLabWorkflow:
    """Durable wrapper around a Strategy Lab batch run."""

    @workflow.run
    async def run(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_strategy_lab_activity,
            args=[run_id, request],
            start_to_close_timeout=timedelta(hours=6),
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
        return await workflow.execute_activity(
            run_backtest_activity,
            args=[job_id, strategy, config, submitted_by, notes],
            start_to_close_timeout=timedelta(hours=6),
        )
