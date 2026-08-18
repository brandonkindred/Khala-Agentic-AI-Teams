"""Temporal worker bootstrap for the Agent Studio team.

Exposes a no-arg ``start_agent_studio_temporal_worker_thread`` that the unified-API
lifespan invokes at boot. Authoring CRUD no longer registers any workflows or
activities, so this entrypoint always returns ``False`` without starting a worker —
Temporal is not required for conversations / clone / save. The lifespan hook is
unchanged (gated on ``UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER``); a false return
there is a fully-functional mode, not a degraded state.
"""

from __future__ import annotations

import logging

from agent_platform.studio.temporal import TASK_QUEUE

logger = logging.getLogger(__name__)


def start_agent_studio_temporal_worker_thread() -> bool:
    """No-op worker starter: authoring CRUD has no Temporal surface.

    Preconditions:
        - None.
    Postconditions:
        - Logs at INFO and returns ``False``. Never starts a worker thread.
          Safe to call multiple times.
    """
    logger.info(
        "Agent Studio has no Temporal authoring workflows; skipping worker on %s",
        TASK_QUEUE,
    )
    return False
