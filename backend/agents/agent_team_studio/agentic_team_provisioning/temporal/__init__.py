"""Temporal workflow + activities wrapping the agentic pipeline runner.

The workflow class and activities live in :mod:`workflows` (sandbox-safe — no
top-level non-deterministic calls). Worker startup lives in :mod:`worker` and is
invoked by the team_service entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC``), with the API lifespan as a standalone-dev backstop, so
the Temporal client is connected before the API serves its first request. This package
``__init__`` must stay free of import-time side effects (no worker boot, no
``os.getenv``) — the temporalio sandbox replays it during workflow registration.
"""

from __future__ import annotations

from agent_team_studio.agentic_team_provisioning.temporal.workflows import (
    AgenticPipelineWorkflow,
    advance_step_activity,
    cancel_reconcile_activity,
    complete_activity,
    fail_activity,
    run_step_activity,
    wait_finalize_activity,
    wait_setup_activity,
)

WORKFLOWS = [AgenticPipelineWorkflow]
ACTIVITIES = [
    advance_step_activity,
    run_step_activity,
    wait_setup_activity,
    wait_finalize_activity,
    complete_activity,
    cancel_reconcile_activity,
    fail_activity,
]
TASK_QUEUE = "agentic_team_provisioning-queue"
WORKFLOW_ID_PREFIX = "agentic-pipeline-"

__all__ = [
    "ACTIVITIES",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "AgenticPipelineWorkflow",
    "advance_step_activity",
    "cancel_reconcile_activity",
    "complete_activity",
    "fail_activity",
    "run_step_activity",
    "wait_finalize_activity",
    "wait_setup_activity",
]
