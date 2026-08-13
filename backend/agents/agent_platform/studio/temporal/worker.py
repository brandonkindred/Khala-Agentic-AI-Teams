"""Temporal worker bootstrap for the Agent Studio team.

Exposes a no-arg ``start_agent_studio_temporal_worker_thread`` that the unified-API
lifespan invokes at boot. Agent Studio is an in-process team (it is mounted directly
on the unified API, not run as its own ``team_service`` container), so its worker runs
inside the unified-API process — the same process that serves the HTTP handlers and
that holds the shared :class:`~agent_platform.studio.service.AgentStudioService` singleton the
activities delegate to.

There is no ``is_temporal_enabled`` guard here: Agent Studio assumes Temporal is
always configured (the platform requires ``TEMPORAL_ADDRESS``). ``start_team_worker``
is idempotent per team, so calling this repeatedly (e.g. under uvicorn ``--reload``)
is safe.
"""

from __future__ import annotations

import logging

from agent_platform.studio.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared.temporal import start_team_worker

logger = logging.getLogger(__name__)


def start_agent_studio_temporal_worker_thread() -> bool:
    """Start the Agent Studio Temporal worker.

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already running) on the
          ``agent-studio-queue`` task queue. Safe to call multiple times — the
          underlying ``start_team_worker`` is idempotent per team.
    """
    return start_team_worker(
        "agent_studio",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
