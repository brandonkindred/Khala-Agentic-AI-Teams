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

from personal_assistant_team.temporal import (
    ACTIVITIES,
    MAX_CONCURRENT_ACTIVITIES,
    TASK_QUEUE,
    WORKFLOWS,
)
from shared.temporal import is_temporal_enabled, start_team_worker

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
        # MAX_CONCURRENT_ACTIVITIES is the single source of truth shared with
        # shared.temporal.teams_registry.start_all_team_workers (which reads
        # it off this same package) — see temporal/constants.py's docstring
        # for why this must not be a literal here.
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
    )
