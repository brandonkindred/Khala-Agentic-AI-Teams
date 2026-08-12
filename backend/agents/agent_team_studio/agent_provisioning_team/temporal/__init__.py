"""Temporal workflows and activities for the Agent Provisioning team.

Exports the ``WORKFLOWS``/``ACTIVITIES`` contract (shared Temporal packaging
shape): the worker bootstrap in :mod:`.worker` registers these with
``shared.temporal.start_team_worker``. This package ``__init__`` performs no
worker boot (no import-time side effects); startup is the ``team_service``
entrypoint's job via :func:`start_agent_provisioning_temporal_worker_thread`
(with the API lifespan as a standalone-dev backstop, and
``shared.temporal.teams_registry.start_all_team_workers`` as a
consolidated-process path).

Sandbox workflows/activities are exported separately, as ``SANDBOX_WORKFLOWS``
/``SANDBOX_ACTIVITIES``. They are never served by the main provisioning worker
(see their comment below for why); unified-api lifespan boots that worker
explicitly via
:func:`.worker.start_agent_provisioning_sandbox_temporal_worker_thread`.
"""

from agent_platform.sandbox.temporal.activities import (
    sandbox_acquire_activity,
    sandbox_reap_activity,
    sandbox_teardown_activity,
)
from agent_platform.sandbox.temporal.workflows import (
    SandboxAcquireWorkflow,
    SandboxReaperWorkflow,
    SandboxTeardownWorkflow,
)
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

# Sandbox workflows/activities are deliberately NOT part of WORKFLOWS/ACTIVITIES
# above (and so are never served by the main provisioning worker that team_service
# and start_all_team_workers boot). They run on their own SANDBOX_TASK_QUEUE,
# served only by a worker booted explicitly from unified_api/main.py's lifespan
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
    "SANDBOX_WORKFLOWS",
    "SANDBOX_ACTIVITIES",
    "AgentProvisioningWorkflow",
    "AgentDeprovisioningWorkflow",
    "SandboxAcquireWorkflow",
    "SandboxTeardownWorkflow",
    "SandboxReaperWorkflow",
    "start_agent_provisioning_temporal_worker_thread",
]
