"""Temporal worker bootstrap for the personal assistant team.

Exposes a no-arg ``start_pa_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars (see ``docker/docker-compose.yml`` for
the pa-service), so the Temporal worker (and its connected client) is ready
before uvicorn accepts requests.

Delegates to the shared ``start_team_worker`` so PA gets the same
``SandboxedWorkflowRunner`` passthrough config (pydantic/strands/boto3/httpx)
as every other Temporal team — mirrors
``market_research_team.temporal.worker``.
"""

from __future__ import annotations

import logging

from personal_assistant_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared_temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_pa_temporal_worker_thread() -> bool:
    """Start the personal-assistant Temporal worker (no-op when disabled).

    Preconditions:
        - None.

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already running),
          ``False`` when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).

    Safe to call multiple times — the underlying ``start_team_worker`` is
    idempotent per team.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "personal_assistant",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
        # Pins the pre-migration cap (the hand-rolled worker this replaced
        # used `max_workers=2` / `max_concurrent_activities=2`). Explicit here
        # rather than left to `start_team_worker`'s default of 4, which would
        # silently double PA's concurrent-activity ceiling.
        max_concurrent_activities=2,
    )
