"""Temporal worker for the Agent Provisioning team.

Worker startup follows shared_temporal Pattern A: the auto-boot in
``agent_provisioning_team/temporal/__init__.py`` calls ``start_team_worker``
on import. ``start_agent_provisioning_temporal_worker_thread`` exposes that
same boot under the no-arg contract the generic team_service entrypoint
invokes via ``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``
(matching ``user_agent_founder`` and ``software_engineering_team``). This
module also retains ``create_agent_provisioning_worker`` for tests and
diagnostics that want to build a ``Worker`` instance directly.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

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

logger = logging.getLogger(__name__)

_activity_executor: Optional[ThreadPoolExecutor] = None


def start_agent_provisioning_temporal_worker_thread() -> bool:
    """Start the Agent Provisioning Temporal worker (no-op when disabled).

    Preconditions:
        - None. Safe to call multiple times — ``start_team_worker`` is
          idempotent per team, so this is a no-op if the Pattern A auto-boot
          (or a prior call) already started the worker.
    Postconditions:
        - Returns True if a worker thread is running (or already running) for
          this team, False when Temporal is disabled.
    """
    from agent_provisioning_team.temporal import ACTIVITIES, WORKFLOWS
    from shared_temporal import start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "agent_provisioning",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )


def create_agent_provisioning_worker(client: Optional[object] = None) -> Optional[Worker]:
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    global _activity_executor
    if _activity_executor is None:
        _activity_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="agent-provisioning-temporal-activity"
        )
    # Pass pydantic through the workflow sandbox so models with
    # datetime fields (DeliverResult.finalized_at, etc.) don't trip
    # pydantic-core's identity-based type check. See the longer
    # explanation in shared_temporal/worker.py:_build_workflow_runner.
    sandbox_restrictions = SandboxRestrictions.default.with_passthrough_modules(
        "pydantic",
        "pydantic_core",
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AgentProvisioningWorkflow, AgentProvisioningWorkflowV2],
        activities=[
            run_provisioning_activity,
            setup_activity_v2,
            credentials_activity_v2,
            provision_tool_activity,
            audit_activity_v2,
            documentation_activity_v2,
            deliver_activity_v2,
            compensate_activity_v2,
        ],
        activity_executor=_activity_executor,
        max_concurrent_activities=8,
        workflow_runner=SandboxedWorkflowRunner(restrictions=sandbox_restrictions),
    )
    logger.info("Agent Provisioning Temporal worker created for task queue %s", TASK_QUEUE)
    return worker
