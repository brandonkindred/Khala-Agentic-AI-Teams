"""
Temporal worker for the software engineering team.

Registers all SE workflows and activities on the configured task queue via
the shared ``start_team_worker`` bootstrap (ThreadPoolExecutor/Worker/daemon
thread plumbing, sandboxed workflow runner, and shared client/loop teardown
guard), the same helper already used by ``code_review_agent`` and
``coding_team``. Run from unified API lifespan or when SE API runs standalone.
"""

from __future__ import annotations

from shared.temporal import start_team_worker
from software_engineering_team.temporal.activities import (
    execute_coding_team_activity,
    parse_spec_activity,
    plan_project_activity,
    retry_failed_activity,
    run_backend_code_v2_activity,
    run_frontend_code_v2_activity,
    run_orchestrator_activity,
    run_product_analysis_activity,
)
from software_engineering_team.temporal.constants import TASK_QUEUE
from software_engineering_team.temporal.workflows import (
    RetryFailedWorkflow,
    RunTeamWorkflowV2,
    StandaloneJobWorkflow,
)

WORKFLOWS = [RunTeamWorkflowV2, RetryFailedWorkflow, StandaloneJobWorkflow]
ACTIVITIES = [
    run_orchestrator_activity,
    parse_spec_activity,
    plan_project_activity,
    execute_coding_team_activity,
    retry_failed_activity,
    run_frontend_code_v2_activity,
    run_backend_code_v2_activity,
    run_product_analysis_activity,
]


def start_se_temporal_worker_thread() -> bool:
    """Start the SE Temporal worker in a daemon thread (if Temporal is enabled).

    Postconditions:
        - Returns False when Temporal is disabled and starts nothing (checked
          by ``start_team_worker`` itself).
        - Otherwise delegates to ``start_team_worker``, which is idempotent
          per team: a second call while the worker thread is alive returns
          True without starting a duplicate thread.
    """
    return start_team_worker("software_engineering", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)
