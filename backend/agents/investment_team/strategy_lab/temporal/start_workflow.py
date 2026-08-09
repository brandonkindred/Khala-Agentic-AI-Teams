"""Sync dispatch helper for the Strategy Lab batch workflow.

Wraps ``shared.temporal.start_workflow_sync`` for the synchronous FastAPI
dispatch path to call, translating a ``RunStrategyLabRequest`` into the
JSON-shaped ``batch_input`` that ``StrategyLabBatchWorkflow.run`` consumes.

Called by ``_dispatch_strategy_lab_run`` (``api/main.py``), the sole,
Temporal-only Strategy Lab dispatch path — the coarse ``start_strategy_lab_workflow``
it replaced has been deleted. Import-time side-effect-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    # ``RunStrategyLabRequest`` is defined in ``investment_team.api.main``; the
    # import is guarded so the type hint carries no runtime coupling (with
    # ``from __future__ import annotations`` the annotation stays a string).
    from investment_team.api.main import RunStrategyLabRequest


def build_strategy_lab_batch_input(
    run_id: str, request: RunStrategyLabRequest, generation: int
) -> Dict[str, Any]:
    """Translate a ``RunStrategyLabRequest`` into ``StrategyLabBatchWorkflow`` input.

    Builds the ``BacktestConfig`` (forcing ``cost_stress=True``), applies the
    ``clamp_max_parallel`` concurrency clamp, translates
    ``allowed_asset_classes`` → ``exclude_asset_classes``, and rehydrates the
    resume offset/seed counters for the batch workflow to consume.

    Preconditions:
        ``request`` is a ``RunStrategyLabRequest``. ``generation`` is the
        caller's own already-known fencing generation for this dispatch —
        NOT independently re-derived here by reading the durable store: a
        transient read failure at dispatch time must never cause the
        dispatched workflow to be tagged with a stale/wrong generation and
        immediately self-fence its own, legitimate writes. See
        ``_dispatch_strategy_lab_run``'s matching precondition.
    Postconditions:
        Returns a JSON-shaped ``batch_input`` dict with every key
        ``StrategyLabBatchWorkflow.run`` reads (``run_id``, ``config``,
        ``batch_size``/``batch_count``/``max_parallel``, ``benchmark_symbol``,
        ``exclude_asset_classes``, paper-trading flags, ``start_cycle_offset``,
        ``generation`` (passed through verbatim), and the resume-seed counters
        ``skipped_cycles``/``errored_cycles``/``errored_details``/
        ``tracker_merge_error_count``/``completed_record_ids``).
    """
    # ``clamp_max_parallel``/``rehydrate_active_run_offset``/
    # ``get_resume_seed_counters`` live in the shared ``strategy_lab.config``/
    # ``strategy_lab.run_state`` modules (also imported by ``api.main``) so
    # this reads the same offset/clamp/counters state without reaching into
    # api.main's private module state.
    from investment_team.models import BacktestConfig
    from investment_team.strategy_lab.config import clamp_max_parallel
    from investment_team.strategy_lab.run_state import (
        get_resume_seed_counters,
        rehydrate_active_run_offset,
    )
    from investment_team.strategy_lab_context import excluded_for_allowed

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
        "max_parallel": clamp_max_parallel(request.max_parallel),
        "benchmark_symbol": request.benchmark_symbol,
        "exclude_asset_classes": exclude_asset_classes,
        "paper_trading_enabled": request.paper_trading_enabled,
        "paper_trading_lookback_days": request.paper_trading_lookback_days,
        "start_cycle_offset": rehydrate_active_run_offset(run_id),
        **get_resume_seed_counters(run_id),
        # Ordered after the counters unpack so the caller-provided fencing
        # generation always wins even if get_resume_seed_counters' return
        # shape ever grows a "generation" key -- silently losing this value
        # to an unpacked counter would break the fencing contract and cause
        # the freshly-dispatched workflow to self-fence its own writes.
        "generation": generation,
    }


def start_strategy_lab_batch_workflow(
    run_id: str, request: RunStrategyLabRequest, generation: int
) -> None:
    """Start ``StrategyLabBatchWorkflow`` on ``strategy-lab-queue`` for a run.

    Preconditions:
        Temporal is enabled and a worker is serving ``strategy-lab-queue``;
        ``run_id`` is unique for this run. ``generation`` is the caller's own
        already-known fencing generation — see
        ``build_strategy_lab_batch_input``'s matching precondition.
    Postconditions:
        Submits the batch workflow with a deterministic ``strategy-lab-{run_id}``
        workflow id and returns once it is started (delegates to
        ``start_workflow_sync``). Raises on a start failure, matching the other
        teams' start helpers.
    """
    from investment_team.strategy_lab.temporal import TASK_QUEUE, WORKFLOW_ID_PREFIX
    from investment_team.strategy_lab.temporal.workflows import StrategyLabBatchWorkflow
    from shared.temporal import start_workflow_sync

    start_workflow_sync(
        StrategyLabBatchWorkflow.run,
        build_strategy_lab_batch_input(run_id, request, generation),
        workflow_id=f"{WORKFLOW_ID_PREFIX}{run_id}",
        task_queue=TASK_QUEUE,
    )


__all__ = ["build_strategy_lab_batch_input", "start_strategy_lab_batch_workflow"]
