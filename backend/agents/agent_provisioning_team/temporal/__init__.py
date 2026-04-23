"""Temporal workflows and worker for the Agent Provisioning team.

Follows shared_temporal Pattern A: exports ``WORKFLOWS``/``ACTIVITIES`` and
self-boots a worker via ``start_team_worker`` when ``TEMPORAL_ADDRESS`` is
set, so ``shared_temporal.teams_registry.start_all_team_workers`` picks up
this team the same way it picks up every other team.
"""

from agent_provisioning_team.temporal.activities import (
    audit_activity_v2,
    compensate_activity_v2,
    credentials_activity_v2,
    deliver_activity_v2,
    documentation_activity_v2,
    provision_tool_activity,
    run_provisioning_activity,
    setup_activity_v2,
)
from agent_provisioning_team.temporal.client import is_temporal_enabled
from agent_provisioning_team.temporal.constants import TASK_QUEUE
from agent_provisioning_team.temporal.workflows import (
    AgentProvisioningWorkflow,
    AgentProvisioningWorkflowV2,
)

WORKFLOWS = [AgentProvisioningWorkflow, AgentProvisioningWorkflowV2]
ACTIVITIES = [
    run_provisioning_activity,
    setup_activity_v2,
    credentials_activity_v2,
    provision_tool_activity,
    audit_activity_v2,
    documentation_activity_v2,
    deliver_activity_v2,
    compensate_activity_v2,
]

from shared_temporal import start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("agent_provisioning", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)

__all__ = [
    "is_temporal_enabled",
    "TASK_QUEUE",
    "WORKFLOWS",
    "ACTIVITIES",
    "AgentProvisioningWorkflow",
    "AgentProvisioningWorkflowV2",
]
