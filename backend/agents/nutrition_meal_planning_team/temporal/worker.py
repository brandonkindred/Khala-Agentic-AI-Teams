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

from nutrition_meal_planning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared_temporal import is_temporal_enabled, start_team_worker

# Matches the team's original hand-rolled worker's concurrency cap (a single
# ThreadPoolExecutor(max_workers=2)). Kept explicit rather than left to
# shared_temporal.start_team_worker's default of 4: this one worker/queue now
# serves three LLM-heavy job kinds (nutrition plan, regenerate, meal plan)
# sharing the same LLM provider rate limits, so the cap is a deliberate
# capacity choice, not an accidental omission.
MAX_CONCURRENT_ACTIVITIES = 2


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
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
    )
