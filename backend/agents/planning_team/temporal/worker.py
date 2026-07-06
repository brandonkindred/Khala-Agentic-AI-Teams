"""Temporal worker for the Planning team."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker

from planning_team.temporal.activities import run_planning_activity
from planning_team.temporal.client import (
    connect_temporal_client,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from planning_team.temporal.constants import TASK_QUEUE
from planning_team.temporal.workflows import PlanningWorkflow

logger = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
_activity_executor: Optional[ThreadPoolExecutor] = None


def create_planning_worker(client: Optional[object] = None) -> Optional[Worker]:
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    global _activity_executor
    if _activity_executor is None:
        _activity_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="planning-temporal-activity"
        )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PlanningWorkflow],
        activities=[run_planning_activity],
        activity_executor=_activity_executor,
        max_concurrent_activities=2,
    )
    logger.info("Planning Temporal worker created for task queue %s", TASK_QUEUE)
    return worker


async def _run_worker_async() -> None:
    client = await connect_temporal_client()
    if client is None:
        return
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())
    worker = create_planning_worker(client)
    if worker is None:
        return
    logger.info("Planning Temporal worker starting")
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
        logger.exception("Planning Temporal worker failed: %s", e)
    finally:
        set_temporal_client(None)
        set_temporal_loop(None)
        loop.close()


def start_planning_temporal_worker_thread() -> bool:
    global _worker_thread
    if not is_temporal_enabled():
        return False
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_thread = threading.Thread(
        target=_worker_thread_target,
        name="planning-temporal-worker",
        daemon=True,
    )
    _worker_thread.start()
    logger.info("Planning Temporal worker thread started")
    return True


def is_worker_thread_alive() -> bool:
    """Return True if the Temporal worker thread exists and is running.

    Preconditions:
        - None.
    Postconditions:
        - Returns whether a worker thread is currently alive in this process —
          it is either connecting or already connected. False means no worker
          is running here (never started, or died after a failed connect), so
          waiting for the client to appear would be futile.
    """
    return _worker_thread is not None and _worker_thread.is_alive()
