"""Start the Planning Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge that waits for the worker's connected client + loop, then schedules the
workflow start on the worker loop). We deliberately do NOT use
``shared_temporal.run_team_job`` here: it creates its own job row and marks it
running, which would collide with the API's ``create_job`` and the activity-owned
RUNNING/COMPLETED bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Optional

from planning_team.temporal import TASK_QUEUE, WORKFLOW_ID_PREFIX, PlanningWorkflow
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_planning_workflow(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
    use_product_analysis: bool,
    use_market_research: bool,
) -> None:
    """Start ``PlanningWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a non-empty, unique run id and ``repo_path`` is a non-empty
          workspace path (both enforced below). The Planning Temporal worker is
          running (or starting) in this process.
    Postconditions:
        - A ``PlanningWorkflow`` is started on ``TASK_QUEUE`` with id
          ``WORKFLOW_ID_PREFIX + job_id``; returns once Temporal accepts it.
    Raises:
        - ``ValueError`` if ``job_id`` or ``repo_path`` is blank (a caller-side
          precondition violation; explicit check so it holds under ``python -O``).
        - ``RuntimeError`` if the Temporal client never becomes available within
          the shared bridge's client-ready window (worker not running /
          misconfigured), plus any Temporal client error propagated from the start.
    """
    # Explicit checks (not asserts) so the precondition holds under ``python -O``.
    if not job_id:
        raise ValueError("job_id must be a non-empty run id")
    if not repo_path:
        raise ValueError("repo_path must be a non-empty workspace path")

    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        PlanningWorkflow.run,
        job_id,
        repo_path,
        client_name,
        initial_brief,
        spec_content,
        use_product_analysis,
        use_market_research,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started PlanningWorkflow id=%s", workflow_id)
