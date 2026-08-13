"""Sandbox-only Temporal worker starter.

Preconditions:
    * Must be called only from the unified-API process that owns the sandbox
      ``Lifecycle`` singleton. Never started by package import or by the
      standalone agent-provisioning team container.
Postconditions:
    * Returns True if a worker thread is running (or already running) for the
      ``"agent_provisioning_sandbox"`` team key on ``SANDBOX_TASK_QUEUE``,
      False when Temporal is disabled.
"""

from __future__ import annotations

from shared.temporal.client import is_temporal_enabled


def start_agent_platform_sandbox_temporal_worker_thread() -> bool:
    """Start the sandbox-only Temporal worker on ``SANDBOX_TASK_QUEUE``."""
    from agent_platform.sandbox.temporal import SANDBOX_ACTIVITIES, SANDBOX_WORKFLOWS
    from agent_platform.sandbox.temporal.constants import SANDBOX_TASK_QUEUE
    from shared.temporal import start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "agent_provisioning_sandbox",
        SANDBOX_WORKFLOWS,
        SANDBOX_ACTIVITIES,
        task_queue=SANDBOX_TASK_QUEUE,
    )
