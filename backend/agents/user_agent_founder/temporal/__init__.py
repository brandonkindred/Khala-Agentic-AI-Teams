"""Temporal workflow + activity wrapping the user-agent founder workflow.

The workflow class and activity live in :mod:`workflows` (sandbox-safe —
no top-level non-deterministic calls). Worker startup lives in
:mod:`worker` and is invoked by the team_service entrypoint at boot
(``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``) so the
Temporal client is connected before the API serves its first request.
"""

from __future__ import annotations

from user_agent_founder.temporal.workflows import (
    UserAgentFounderWorkflow,
    run_pipeline_activity,
)

WORKFLOWS = [UserAgentFounderWorkflow]
ACTIVITIES = [run_pipeline_activity]
TASK_QUEUE = "user_agent_founder-queue"
WORKFLOW_ID_PREFIX = "user-agent-founder-"

__all__ = [
    "ACTIVITIES",
    "TASK_QUEUE",
    "UserAgentFounderWorkflow",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "run_pipeline_activity",
]
