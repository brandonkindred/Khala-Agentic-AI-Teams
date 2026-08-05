"""Temporal workflow + activities for the user_agent_founder (Testing Personas) team.

The lifecycle is decomposed into per-step, individually-retryable activities
(``activities`` module) coordinated by the deterministic ``UserAgentFounderWorkflow``
(``workflows`` module) — both sandbox-safe (no top-level non-deterministic calls).
Worker startup lives in :mod:`worker` and is invoked by the team_service
entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``)
so the Temporal client is connected before the API serves its first request.

This package ``__init__`` must have NO import-time side effects (no ``os.getenv``,
no worker boot): the temporalio sandbox re-imports it during workflow
registration (guarded by ``tests/test_temporal_bootstrap.py``).
"""

from __future__ import annotations

from agent_team_studio.user_agent_founder.temporal.activities import (
    answer_questions_activity,
    begin_run_activity,
    enter_phase_activity,
    finalize_run_activity,
    generate_spec_activity,
    mark_failed_activity,
    poll_phase_activity,
)
from agent_team_studio.user_agent_founder.temporal.workflows import UserAgentFounderWorkflow

WORKFLOWS = [UserAgentFounderWorkflow]
ACTIVITIES = [
    begin_run_activity,
    generate_spec_activity,
    enter_phase_activity,
    poll_phase_activity,
    answer_questions_activity,
    finalize_run_activity,
    mark_failed_activity,
]
TASK_QUEUE = "user_agent_founder-queue"
WORKFLOW_ID_PREFIX = "user-agent-founder-"

__all__ = [
    "ACTIVITIES",
    "TASK_QUEUE",
    "UserAgentFounderWorkflow",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "answer_questions_activity",
    "begin_run_activity",
    "enter_phase_activity",
    "finalize_run_activity",
    "generate_spec_activity",
    "mark_failed_activity",
    "poll_phase_activity",
]
