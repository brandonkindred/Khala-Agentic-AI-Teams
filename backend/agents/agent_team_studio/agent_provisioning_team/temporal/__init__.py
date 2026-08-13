"""Temporal workflows and activities for the Agent Provisioning team.

Exports the ``WORKFLOWS``/``ACTIVITIES`` contract (shared Temporal packaging
shape): the worker bootstrap in :mod:`.worker` registers these with
``shared.temporal.start_team_worker``. This package ``__init__`` performs no
worker boot (no import-time side effects); startup is the ``team_service``
entrypoint's job via :func:`start_agent_provisioning_temporal_worker_thread`
(with the API lifespan as a standalone-dev backstop, and
``shared.temporal.teams_registry.start_all_team_workers`` as a
consolidated-process path).
"""

from agent_team_studio.agent_provisioning_team.temporal.activities import (
    acquire_agent_lock_activity,
    audit_activity,
    check_existing_environment_activity,
    compensate_activity,
    credentials_activity,
    deliver_activity,
    deprovision_activity,
    documentation_activity,
    list_manifest_tools_activity,
    mark_job_failed_activity,
    provision_tool_activity,
    record_account_provisioning_activity,
    release_agent_lock_activity,
    setup_activity,
)
from agent_team_studio.agent_provisioning_team.temporal.constants import TASK_QUEUE
from agent_team_studio.agent_provisioning_team.temporal.workflows import (
    AgentDeprovisioningWorkflow,
    AgentProvisioningWorkflow,
)
from shared.temporal.client import is_temporal_enabled

WORKFLOWS = [
    AgentProvisioningWorkflow,
    AgentDeprovisioningWorkflow,
]
ACTIVITIES = [
    acquire_agent_lock_activity,
    release_agent_lock_activity,
    check_existing_environment_activity,
    setup_activity,
    list_manifest_tools_activity,
    credentials_activity,
    provision_tool_activity,
    record_account_provisioning_activity,
    audit_activity,
    documentation_activity,
    deliver_activity,
    compensate_activity,
    mark_job_failed_activity,
    deprovision_activity,
]

# Sandbox Temporal lives in agent_platform.sandbox.temporal and is not served by this worker.

# Deferred: importing ``worker`` above WORKFLOWS/ACTIVITIES would be fine today
# (worker only imports ``constants`` at module level), but keep the start helper
# after the export lists so readers see packaging first, boot second.
from agent_team_studio.agent_provisioning_team.temporal.worker import (  # noqa: E402
    start_agent_provisioning_temporal_worker_thread,
)

__all__ = [
    "is_temporal_enabled",
    "TASK_QUEUE",
    "WORKFLOWS",
    "ACTIVITIES",
    "AgentProvisioningWorkflow",
    "AgentDeprovisioningWorkflow",
    "start_agent_provisioning_temporal_worker_thread",
]
