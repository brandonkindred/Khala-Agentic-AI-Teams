"""Worker bootstrap for the Strategy Lab Temporal integration.

Starts a Temporal worker on the dedicated ``strategy-lab-queue`` that serves both
``StrategyLabBatchWorkflow`` and its ``StrategyLabCycleWorkflow`` children (plus
every fine-grained activity). Kept off the shared ``teams_registry`` auto-loop
(which would derive ``investment-queue`` from the ``investment`` team key) so this
first-ever child-workflow fan-out gets its own queue with independently tunable
concurrency and blast-radius isolation.

Called from ``investment_team.temporal.worker.start_investment_temporal_worker_thread``,
which boots it alongside the investment team's own ``investment-queue`` and
``investment-advisory-queue`` workers. Kept side-effect-free at import time (all
reads happen inside the function body) so importing the package never trips the
sandbox.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _max_concurrent_activities() -> int:
    """Resolve the per-worker activity concurrency for ``strategy-lab-queue``.

    A batch can run hundreds of cycles × 10+ activities each, so the shared
    default of 4 is a bottleneck; default higher here and let operators tune via
    ``STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES``.

    Postconditions:
        Returns an int ≥ 1 (garbage / out-of-range env → default 8, floored at 1).
    """
    raw = os.environ.get("STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES", "8")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 8


def start_strategy_lab_temporal_worker_thread() -> bool:
    """Start the Strategy Lab Temporal worker on ``strategy-lab-queue``.

    Preconditions:
        None.
    Postconditions:
        Returns ``True`` when a worker was started; ``False`` when Temporal is
        disabled (``TEMPORAL_ADDRESS`` unset). Safe to call alongside the
        investment team's own worker — ``start_team_worker`` supports multiple
        team keys in one process.
    """
    from investment_team.strategy_lab.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from shared.temporal import is_temporal_enabled, start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "investment_strategy_lab",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
        max_concurrent_activities=_max_concurrent_activities(),
    )


__all__ = ["start_strategy_lab_temporal_worker_thread"]
