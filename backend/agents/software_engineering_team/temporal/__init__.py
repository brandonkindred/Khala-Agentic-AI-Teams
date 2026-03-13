"""
Temporal workflows and activities for the software engineering team.

When TEMPORAL_ADDRESS is set, the SE API starts workflows instead of threads;
a worker in the same process (or standalone) executes activities so jobs are
durable and resumable after server restarts.
"""

from software_engineering_team.temporal.constants import (
    TASK_QUEUE,
    WORKFLOW_RUN_TEAM,
    WORKFLOW_RETRY_FAILED,
    WORKFLOW_STANDALONE_JOB,
    WORKFLOW_ID_PREFIX_RUN_TEAM,
    WORKFLOW_ID_PREFIX_RETRY_FAILED,
    WORKFLOW_ID_PREFIX_STANDALONE,
)

__all__ = [
    "TASK_QUEUE",
    "WORKFLOW_RUN_TEAM",
    "WORKFLOW_RETRY_FAILED",
    "WORKFLOW_STANDALONE_JOB",
    "WORKFLOW_ID_PREFIX_RUN_TEAM",
    "WORKFLOW_ID_PREFIX_RETRY_FAILED",
    "WORKFLOW_ID_PREFIX_STANDALONE",
]
