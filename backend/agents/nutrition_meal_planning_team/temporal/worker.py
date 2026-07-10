"""Temporal worker bootstrap for the Nutrition & Meal Planning team.

Exposes a no-arg ``start_nutrition_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars (see ``docker/docker-compose.yml``), so
the Temporal worker (and its connected client) is ready before uvicorn starts
accepting requests. The API lifespan calls the same function as a
standalone-dev backstop.

Delegates to ``shared_temporal.start_team_worker`` so this team shares the one
cached client/event loop, the gzip payload codec, and — importantly — the
sandbox-passthrough workflow runner (so ``strands``/``boto3``/``httpx`` imports
pulled in transitively survive workflow registration).
"""

from __future__ import annotations

import logging

from nutrition_meal_planning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared_temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_nutrition_temporal_worker_thread() -> bool:
    """Start the nutrition Temporal worker (no-op when disabled).

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already running),
          ``False`` when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).

    Safe to call multiple times — the underlying ``start_team_worker`` is
    idempotent per team.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "nutrition_meal_planning",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
