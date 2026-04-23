"""Temporal worker creation for the Agent Provisioning team.

Worker startup is handled by ``shared_temporal.start_team_worker`` via the
Pattern A auto-boot in ``agent_provisioning_team/temporal/__init__.py``.
This module retains ``create_agent_provisioning_worker`` for tests and
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
