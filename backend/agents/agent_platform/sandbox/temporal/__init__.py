"""Sandbox-only Temporal packaging: workflows, activities, queue, worker starter.

Preconditions:
    * Importing this package must not start a worker.
Postconditions:
    * ``SANDBOX_WORKFLOWS`` / ``SANDBOX_ACTIVITIES`` / ``SANDBOX_TASK_QUEUE``
      are the canonical lists the unified-API lifespan registers. They are
      never part of the provisioning team's ``WORKFLOWS`` / ``ACTIVITIES``.
"""

from agent_platform.sandbox.temporal.activities import (
    sandbox_acquire_activity,
    sandbox_reap_activity,
    sandbox_teardown_activity,
)
from agent_platform.sandbox.temporal.constants import SANDBOX_TASK_QUEUE
from agent_platform.sandbox.temporal.worker import (
    start_agent_platform_sandbox_temporal_worker_thread,
)
from agent_platform.sandbox.temporal.workflows import (
    SandboxAcquireWorkflow,
    SandboxReaperWorkflow,
    SandboxTeardownWorkflow,
)

SANDBOX_WORKFLOWS = [
    SandboxAcquireWorkflow,
    SandboxTeardownWorkflow,
    SandboxReaperWorkflow,
]
SANDBOX_ACTIVITIES = [
    sandbox_acquire_activity,
    sandbox_teardown_activity,
    sandbox_reap_activity,
]

__all__ = [
    "SANDBOX_TASK_QUEUE",
    "SANDBOX_WORKFLOWS",
    "SANDBOX_ACTIVITIES",
    "SandboxAcquireWorkflow",
    "SandboxTeardownWorkflow",
    "SandboxReaperWorkflow",
    "start_agent_platform_sandbox_temporal_worker_thread",
]
