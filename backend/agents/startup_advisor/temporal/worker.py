"""Temporal worker bootstrap for the Startup Advisor team.

Exposes a no-arg ``start_startup_advisor_temporal_worker_thread`` that the
generic team_service entrypoint invokes at boot via the
``TEAM_TEMPORAL_WORKER_MODULE``/``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the
Temporal worker (and its connected client) is ready before uvicorn starts
accepting requests. The API lifespan calls the same function as a
standalone-dev backstop.

This shape mirrors ``road_trip_planning_team.temporal.worker.
start_road_trip_temporal_worker_thread`` — same contract, same boot hook in
docker-compose.
"""

from __future__ import annotations

import logging

from shared_temporal import is_temporal_enabled, start_team_worker
from startup_advisor.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS

logger = logging.getLogger(__name__)


def start_startup_advisor_temporal_worker_thread() -> bool:
    """Start the Startup Advisor Temporal worker (no-op when disabled).

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already running),
          ``False`` when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).

    Safe to call multiple times — the underlying ``start_team_worker`` is
    idempotent per team.
    """
    if not is_temporal_enabled():
        return False
    started = start_team_worker("startup_advisor", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)
    logger.info("Startup Advisor Temporal worker start requested: started=%s", started)
    return started
