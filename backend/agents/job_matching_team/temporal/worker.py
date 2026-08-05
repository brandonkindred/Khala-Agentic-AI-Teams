"""Temporal worker bootstrap for the job matching team.

Exposes a no-arg ``start_job_matching_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the Temporal worker (and its connected
client) is ready before uvicorn starts accepting requests.

Same contract as ``agent_team_studio.user_agent_founder.temporal.worker.
start_user_agent_founder_temporal_worker_thread`` and its siblings.
"""

from __future__ import annotations

import logging

from job_matching_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared.temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_job_matching_temporal_worker_thread() -> bool:
    """Start the job matching Temporal worker (no-op when disabled).

    Postconditions:
        * Returns True if a worker thread is running (or already running) after
          the call; False when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).
        * Idempotent — the underlying ``start_team_worker`` is per-team, so
          repeated calls do not start duplicate workers.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "job_matching",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
