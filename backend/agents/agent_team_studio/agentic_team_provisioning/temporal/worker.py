"""Temporal worker bootstrap for the agentic_team_provisioning team.

Exposes a no-arg ``start_agentic_team_provisioning_temporal_worker_thread`` that the
generic team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the Temporal worker (and its connected
client) is ready before uvicorn starts accepting requests. The API lifespan calls the
same function as a standalone-dev backstop.

This shape mirrors ``sales_team.temporal.worker.start_sales_temporal_worker_thread`` —
same contract, same boot hook in docker-compose.
"""

from __future__ import annotations

import logging

from agent_team_studio.agentic_team_provisioning.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared.temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_agentic_team_provisioning_temporal_worker_thread() -> bool:
    """Start the agentic_team_provisioning Temporal worker (no-op when disabled).

    Preconditions:
        - None (safe to call multiple times — ``start_team_worker`` is idempotent
          per team).

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already running),
          ``False`` when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "agentic_team_provisioning",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
