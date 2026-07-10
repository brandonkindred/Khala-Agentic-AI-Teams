"""Sync dispatch helper for the Strategy Lab batch workflow.

Wraps ``shared_temporal.start_workflow_sync`` for the synchronous FastAPI
dispatch path to call, translating a ``RunStrategyLabRequest`` into the
JSON-shaped ``batch_input`` that ``StrategyLabBatchWorkflow.run`` consumes —
mirroring the config/clamp/exclusion construction ``_strategy_lab_worker`` does
today, so the two entrypoints stay behaviorally aligned.

Not yet called by ``_dispatch_strategy_lab_run`` — repointing dispatch at this
helper (and deleting the coarse ``start_strategy_lab_workflow``) is the Stage 5
cutover. Import-time side-effect-free.
"""

from __future__ import annotations

from typing import Any, Dict


def build_strategy_lab_batch_input(run_id: str, request: Any) -> Dict[str, Any]:
    """Translate a ``RunStrategyLabRequest`` into ``StrategyLabBatchWorkflow`` input.

    Reproduces ``_strategy_lab_worker``'s ``BacktestConfig`` construction
    (``cost_stress=True``), the ``_clamp_max_parallel`` concurrency clamp, the
    ``allowed_asset_classes`` → ``exclude_asset_classes`` translation, and the
    resume-offset rehydration — so the Temporal batch runs with the same inputs
    the thread-mode worker would.

    Preconditions:
        ``request`` is a ``RunStrategyLabRequest``.
    Postconditions:
        Returns a JSON-shaped ``batch_input`` dict with every key
        ``StrategyLabBatchWorkflow.run`` reads (``run_id``, ``config``,
        ``batch_size``/``batch_count``/``max_parallel``, ``benchmark_symbol``,
        ``exclude_asset_classes``, paper-trading flags, ``start_cycle_offset``).
    """
    from investment_team.api.main import (
        _clamp_max_parallel,
        _rehydrate_active_run_offset,
        excluded_for_allowed,
    )
    from investment_team.models import BacktestConfig

    config = BacktestConfig(
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        benchmark_symbol=request.benchmark_symbol,
        transaction_cost_bps=request.transaction_cost_bps,
        slippage_bps=request.slippage_bps,
        cost_stress=True,
    )

    exclude_asset_classes = None
    if request.allowed_asset_classes:
        exclude_asset_classes = excluded_for_allowed(request.allowed_asset_classes) or None

    return {
        "run_id": run_id,
        "config": config.model_dump(mode="json"),
        "batch_size": request.batch_size,
        "batch_count": request.batch_count,
        "max_parallel": _clamp_max_parallel(request.max_parallel),
        "benchmark_symbol": request.benchmark_symbol,
        "exclude_asset_classes": exclude_asset_classes,
        "paper_trading_enabled": request.paper_trading_enabled,
        "paper_trading_lookback_days": request.paper_trading_lookback_days,
        "start_cycle_offset": _rehydrate_active_run_offset(run_id),
    }


def start_strategy_lab_batch_workflow(run_id: str, request: Any) -> None:
    """Start ``StrategyLabBatchWorkflow`` on ``strategy-lab-queue`` for a run.

    Preconditions:
        Temporal is enabled and a worker is serving ``strategy-lab-queue``;
        ``run_id`` is unique for this run.
    Postconditions:
        Submits the batch workflow with a deterministic ``strategy-lab-{run_id}``
        workflow id and returns once it is started (delegates to
        ``start_workflow_sync``). Raises on a start failure, matching the other
        teams' start helpers.
    """
    from investment_team.strategy_lab.temporal import TASK_QUEUE, WORKFLOW_ID_PREFIX
    from investment_team.strategy_lab.temporal.workflows import StrategyLabBatchWorkflow
    from shared_temporal import start_workflow_sync

    start_workflow_sync(
        StrategyLabBatchWorkflow.run,
        build_strategy_lab_batch_input(run_id, request),
        workflow_id=f"{WORKFLOW_ID_PREFIX}{run_id}",
        task_queue=TASK_QUEUE,
    )


__all__ = ["build_strategy_lab_batch_input", "start_strategy_lab_batch_workflow"]
