"""Temporal worker for the AI systems team."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker

from ai_systems_team.temporal.activities import run_build_activity
from ai_systems_team.temporal.client import (
    connect_temporal_client,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from ai_systems_team.temporal.constants import TASK_QUEUE
from ai_systems_team.temporal.workflows import AISystemsBuildWorkflow

logger = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
_activity_executor: Optional[ThreadPoolExecutor] = None


def create_ai_systems_worker(client: Optional[object] = None) -> Optional[Worker]:
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    global _activity_executor
    if _activity_executor is None:
        _activity_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-systems-temporal-activity")
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AISystemsBuildWorkflow],
        activities=[run_build_activity],
        activity_executor=_activity_executor,
        max_concurrent_activities=2,
    )
    logger.info("AI Systems Temporal worker created for task queue %s", TASK_QUEUE)
    return worker


async def _run_worker_async() -> None:
    client = await connect_temporal_client()
    if client is None:
        return
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())
    worker = create_ai_systems_worker(client)
    if worker is None:
        return
    logger.info("AI Systems Temporal worker starting")
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
        logger.exception("AI Systems Temporal worker failed: %s", e)
    finally:
        set_temporal_client(None)
        set_temporal_loop(None)
        loop.close()


def start_ai_systems_temporal_worker_thread() -> bool:
    global _worker_thread
    if not is_temporal_enabled():
        return False
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_thread = threading.Thread(
        target=_worker_thread_target,
        name="ai-systems-temporal-worker",
        daemon=True,
    )
    _worker_thread.start()
    logger.info("AI Systems Temporal worker thread started")
    return True
