"""Temporal worker bootstrap for the user_agent_founder team.

Exposes a no-arg ``start_user_agent_founder_temporal_worker_thread``
that the generic team_service entrypoint can invoke at boot via the
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` env
vars, so the Temporal worker (and its connected client) is ready
before uvicorn starts accepting requests.

This shape mirrors ``software_engineering_team.temporal.worker.
start_se_temporal_worker_thread`` and ``blogging.temporal.worker.
start_blogging_temporal_worker_thread`` — same contract, same boot
hook in docker-compose.
"""

from __future__ import annotations

import logging

from agent_team_studio.user_agent_founder.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared.temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_user_agent_founder_temporal_worker_thread() -> bool:
    """Start the user_agent_founder Temporal worker (no-op when disabled).

    Returns True if a worker thread is running (or already running), False
    when Temporal is disabled. Safe to call multiple times — the underlying
    ``start_team_worker`` is idempotent per team.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "user_agent_founder",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
