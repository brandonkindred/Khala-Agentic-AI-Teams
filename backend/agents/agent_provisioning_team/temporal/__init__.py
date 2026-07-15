"""Temporal workflows and worker for the Agent Provisioning team.

Follows shared_temporal Pattern A: exports ``WORKFLOWS``/``ACTIVITIES`` and
self-boots a worker via ``start_team_worker`` when ``TEMPORAL_ADDRESS`` is
set, so ``shared_temporal.teams_registry.start_all_team_workers`` picks up
this team the same way it picks up every other team.

Sandbox workflows/activities are exported separately, as ``SANDBOX_WORKFLOWS``
/``SANDBOX_ACTIVITIES`` — this module never auto-boots a worker for them (see
their docstring below for why).
"""

from agent_provisioning_team.temporal.activities import (
    audit_activity,
    compensate_activity,
    credentials_activity,
    deliver_activity,
    deprovision_activity,
    documentation_activity,
    mark_job_failed_activity,
    provision_tool_activity,
    setup_activity,
)
from agent_provisioning_team.temporal.client import is_temporal_enabled
from agent_provisioning_team.temporal.constants import TASK_QUEUE
from agent_provisioning_team.temporal.sandbox_activities import (
    sandbox_acquire_activity,
    sandbox_reap_activity,
    sandbox_teardown_activity,
)
from agent_provisioning_team.temporal.sandbox_workflows import (
    SandboxAcquireWorkflow,
    SandboxReaperWorkflow,
    SandboxTeardownWorkflow,
)
from agent_provisioning_team.temporal.workflows import (
    AgentDeprovisioningWorkflow,
    AgentProvisioningWorkflow,
)

WORKFLOWS = [
    AgentProvisioningWorkflow,
    AgentDeprovisioningWorkflow,
]
ACTIVITIES = [
    setup_activity,
    credentials_activity,
    provision_tool_activity,
    audit_activity,
    documentation_activity,
    deliver_activity,
    compensate_activity,
    mark_job_failed_activity,
    deprovision_activity,
]

# Sandbox workflows/activities are deliberately NOT part of WORKFLOWS/ACTIVITIES
# above (and so are never served by this Pattern-A auto-boot, which also fires
# inside the standalone agent-provisioning-service team container). They run
# on their own SANDBOX_TASK_QUEUE, served only by a worker booted explicitly
# from unified_api/main.py's own lifespan
# (temporal/worker.py::start_agent_provisioning_sandbox_temporal_worker_thread)
# — see SANDBOX_TASK_QUEUE's docstring in temporal/constants.py for why this
# separation exists (the sandbox Lifecycle singleton is process-local state).
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

from shared_temporal import start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("agent_provisioning", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)

__all__ = [
    "is_temporal_enabled",
    "TASK_QUEUE",
    "WORKFLOWS",
    "ACTIVITIES",
    "SANDBOX_WORKFLOWS",
    "SANDBOX_ACTIVITIES",
    "AgentProvisioningWorkflow",
    "AgentDeprovisioningWorkflow",
    "SandboxAcquireWorkflow",
    "SandboxTeardownWorkflow",
    "SandboxReaperWorkflow",
]
