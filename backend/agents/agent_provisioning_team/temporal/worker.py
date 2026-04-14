"""Temporal worker for the Agent Provisioning team."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from agent_provisioning_team.temporal.activities import (
    compensate_activity_v2,
    credentials_activity_v2,
    provision_tool_activity,
    run_provisioning_activity,
    setup_activity_v2,
)
from agent_provisioning_team.temporal.client import (
    connect_temporal_client,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from agent_provisioning_team.temporal.constants import TASK_QUEUE
from agent_provisioning_team.temporal.workflows import (
    AgentProvisioningWorkflow,
    AgentProvisioningWorkflowV2,
)

logger = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
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
            compensate_activity_v2,
        ],
        activity_executor=_activity_executor,
        max_concurrent_activities=8,
        workflow_runner=SandboxedWorkflowRunner(restrictions=sandbox_restrictions),
    )
    logger.info("Agent Provisioning Temporal worker created for task queue %s", TASK_QUEUE)
    return worker


async def _run_worker_async() -> None:
    client = await connect_temporal_client()
    if client is None:
        return
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())
    worker = create_agent_provisioning_worker(client)
    if worker is None:
        return
    logger.info("Agent Provisioning Temporal worker starting")
    await worker.run()


def _worker_thread_target() -> None:
    global _worker_thread
    if not is_temporal_enabled():
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_worker_async())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("Agent Provisioning Temporal worker failed: %s", e)
    finally:
        set_temporal_client(None)
        set_temporal_loop(None)
        loop.close()


def start_agent_provisioning_temporal_worker_thread() -> bool:
    global _worker_thread
    if not is_temporal_enabled():
        return False
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_thread = threading.Thread(
        target=_worker_thread_target,
        name="agent-provisioning-temporal-worker",
        daemon=True,
    )
    _worker_thread.start()
    logger.info("Agent Provisioning Temporal worker thread started")
    return True
