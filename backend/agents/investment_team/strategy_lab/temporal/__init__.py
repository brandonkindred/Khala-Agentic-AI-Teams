"""Temporal integration for the Strategy Lab (Pattern A).

Re-exports ``WORKFLOWS`` / ``ACTIVITIES`` / ``TASK_QUEUE`` / ``WORKFLOW_ID_PREFIX``
with **zero import-time side effects** (no ``os.getenv``, no worker boot) so the
temporalio sandbox can re-import this package to register workflow classes. The
fine-grained ``StrategyLabCycleWorkflow`` / ``StrategyLabBatchWorkflow`` run on
their own ``strategy-lab-queue`` (see ``workflows.py``), separate from
``investment_team.temporal``'s coarse ``investment-queue``.

Worker boot lives in ``worker.py`` (``start_strategy_lab_temporal_worker_thread``,
called from ``investment_team.temporal.worker.start_investment_temporal_worker_thread``)
and sync dispatch in ``start_workflow.py`` (``start_strategy_lab_batch_workflow``,
called from ``api.main._dispatch_strategy_lab_run``). Strategy Lab dispatch is
Temporal-only — there is no thread-mode fallback. Importing this package must
remain free of those side effects.
"""

from __future__ import annotations

from investment_team.strategy_lab.temporal.workflows import (
    ACTIVITIES,
    TASK_QUEUE,
    WORKFLOWS,
    StrategyLabBatchWorkflow,
    StrategyLabCycleWorkflow,
)

# Prefix for deterministic Temporal workflow ids (e.g. ``strategy-lab-{run_id}``).
WORKFLOW_ID_PREFIX = "strategy-lab-"

__all__ = [
    "ACTIVITIES",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "StrategyLabBatchWorkflow",
    "StrategyLabCycleWorkflow",
]
