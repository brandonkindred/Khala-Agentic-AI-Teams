"""Temporal worker bootstrap for the investment team.

Exposes a no-arg ``start_investment_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the Temporal worker (and its
connected client) is ready before uvicorn accepts requests. Mirrors
``user_agent_founder.temporal.worker.start_user_agent_founder_temporal_worker_thread``.
"""

from __future__ import annotations

import logging

from investment_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared_temporal import is_temporal_enabled, start_team_worker

logger = logging.getLogger(__name__)


def start_investment_temporal_worker_thread() -> bool:
    """Start the investment Temporal worker (no-op when disabled).

    Preconditions:
        - None; safe to call unconditionally and repeatedly.

    Postconditions:
        - Returns ``True`` if a worker thread is running (or was already running
          — ``start_team_worker`` is idempotent per team), and ``False`` when
          Temporal is disabled (``TEMPORAL_ADDRESS`` unset).
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "investment",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
