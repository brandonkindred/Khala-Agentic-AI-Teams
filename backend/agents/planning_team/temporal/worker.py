"""Temporal worker bootstrap for the Planning team.

Exposes a no-arg ``start_planning_temporal_worker_thread`` that the generic
``team_service`` entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars (with the API lifespan as a
standalone-dev backstop), so the Temporal worker — and its connected client — is
ready before uvicorn starts accepting requests.

Delegates to ``shared_temporal.start_team_worker``, which owns the daemon-thread
lifecycle, the process-wide client/loop slots, and the sandboxed workflow runner
with the ``pydantic``/``httpx`` passthrough modules the workflow registration
needs.
"""

from __future__ import annotations

import logging

from planning_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared_temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_planning_temporal_worker_thread() -> bool:
    """Start the Planning Temporal worker on a daemon thread (no-op when disabled).

    Postconditions:
        - Returns ``False`` when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).
        - Otherwise registers ``WORKFLOWS``/``ACTIVITIES`` on ``TASK_QUEUE`` via the
          shared, idempotent ``start_team_worker`` and returns ``True`` when a
          worker thread is running (or already running). Safe to call repeatedly.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "planning",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
