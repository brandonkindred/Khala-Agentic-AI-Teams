"""
Temporal worker for the software engineering team.

Registers all SE workflows and activities on the configured task queue.
Run from unified API lifespan or when SE API runs standalone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker

from software_engineering_team.temporal.activities import (
    execute_coding_team_activity,
    parse_spec_activity,
    plan_project_activity,
    retry_failed_activity,
    run_backend_code_v2_activity,
    run_frontend_code_v2_activity,
    run_orchestrator_activity,
    run_product_analysis_activity,
)
from software_engineering_team.temporal.client import (
    connect_temporal_client,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from software_engineering_team.temporal.constants import TASK_QUEUE
from software_engineering_team.temporal.workflows import (
    RetryFailedWorkflow,
    RunTeamWorkflow,
    RunTeamWorkflowV2,
    StandaloneJobWorkflow,
)

logger = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
_activity_executor: Optional[ThreadPoolExecutor] = None


def create_se_worker(client: Optional[object] = None) -> Optional[Worker]:
    """
    Create a Temporal worker for the SE team workflows and activities.
    Returns None if Temporal is not enabled or client is None.
    Sync activities run in a thread pool so an activity_executor is required.
    """
    global _activity_executor
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    if _activity_executor is None:
        _activity_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="se-temporal-activity"
        )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[RunTeamWorkflow, RunTeamWorkflowV2, RetryFailedWorkflow, StandaloneJobWorkflow],
        activities=[
            run_orchestrator_activity,
            parse_spec_activity,
            plan_project_activity,
            execute_coding_team_activity,
            retry_failed_activity,
            run_frontend_code_v2_activity,
            run_backend_code_v2_activity,
            run_product_analysis_activity,
        ],
        activity_executor=_activity_executor,
        max_concurrent_activities=4,
    )
    logger.info("SE Temporal worker created for task queue %s", TASK_QUEUE)
    return worker


async def _run_worker_async() -> None:
    """Create worker and run it; must run inside event loop so workflow validation sees a running loop."""
    client = await connect_temporal_client()
    if client is None:
        return
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())
    worker = create_se_worker(client)
    if worker is None:
        return
    logger.info("SE Temporal worker starting")
    await worker.run()


def _worker_thread_target() -> None:
    """Run in a dedicated thread: connect client, set globals, create worker, run until shutdown."""
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
        logger.exception("SE Temporal worker failed: %s", e)
    finally:
        set_temporal_client(None)
        set_temporal_loop(None)
        loop.close()


def start_se_temporal_worker_thread() -> bool:
    """
    Start the SE Temporal worker in a daemon thread (if Temporal is enabled).
    Returns True if the thread was started, False otherwise.
    """
    global _worker_thread
    if not is_temporal_enabled():
        return False
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_thread = threading.Thread(
        target=_worker_thread_target,
        name="se-temporal-worker",
        daemon=True,
    )
    _worker_thread.start()
    logger.info("SE Temporal worker thread started")
    return True
