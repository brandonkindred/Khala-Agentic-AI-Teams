"""Temporal worker for the Branding team.

Worker startup follows shared_temporal Pattern A: the auto-boot in
``branding_team/temporal/__init__.py`` calls ``start_team_worker`` on import.
``start_branding_temporal_worker_thread`` exposes that same boot under the
no-arg -> bool contract the generic ``team_service`` entrypoint invokes via
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` (matching
``agent_provisioning_team`` and ``user_agent_founder``). Both entry points are
idempotent because ``start_team_worker`` reuses a live worker thread per team.
"""

from __future__ import annotations

import logging

from branding_team.temporal.constants import TASK_QUEUE

logger = logging.getLogger(__name__)


def start_branding_temporal_worker_thread() -> bool:
    """Start the Branding Temporal worker (no-op when disabled).

    Preconditions:
        - None. Safe to call multiple times — ``start_team_worker`` is
          idempotent per team, so this is a no-op if the Pattern A auto-boot
          (or a prior call) already started the worker.
    Postconditions:
        - Returns True if a worker thread is running (or already running) for
          this team, False when Temporal is disabled.
    """
    from branding_team.temporal import ACTIVITIES, WORKFLOWS
    from shared_temporal import is_temporal_enabled, start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "branding",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
