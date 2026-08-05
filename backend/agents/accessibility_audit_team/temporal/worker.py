"""Temporal worker bootstrap for the accessibility_audit team.

Exposes a no-arg ``start_accessibility_audit_temporal_worker_thread`` that the
generic team_service entrypoint invokes at boot via the
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the
Temporal worker (and its connected client) is ready before uvicorn starts
accepting requests. Same contract as
``agent_team_studio.user_agent_founder.temporal.worker.start_user_agent_founder_temporal_worker_thread``.
"""

from __future__ import annotations

from accessibility_audit_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
from shared.temporal import is_temporal_enabled, start_team_worker


def start_accessibility_audit_temporal_worker_thread() -> bool:
    """Start the accessibility_audit Temporal worker (no-op when disabled).

    Preconditions:
        - None (safe to call regardless of ``TEMPORAL_ADDRESS``).
    Postconditions:
        - Returns True if a worker thread is running (or already running), False
          when Temporal is disabled. Idempotent — ``start_team_worker`` is safe to
          call repeatedly per team.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "accessibility_audit",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
