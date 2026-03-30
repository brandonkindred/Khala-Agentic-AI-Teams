"""Temporal worker for the blogging team. Registers workflows and activities on the blogging task queue."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker

from blogging.temporal.activities import run_full_pipeline_activity
from blogging.temporal.client import (
    connect_temporal_client,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from blogging.temporal.constants import TASK_QUEUE
from blogging.temporal.workflows import BlogFullPipelineWorkflow

logger = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
_activity_executor: Optional[ThreadPoolExecutor] = None
_worker_instance: Optional[Worker] = None
_worker_running_loop: Optional[asyncio.AbstractEventLoop] = None


def create_blogging_worker(client: Optional[object] = None) -> Optional[Worker]:
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    global _activity_executor
    if _activity_executor is None:
        _activity_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="blogging-temporal-activity"
        )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[BlogFullPipelineWorkflow],
        activities=[run_full_pipeline_activity],
        activity_executor=_activity_executor,
        max_concurrent_activities=2,
    )
    logger.info("Blogging Temporal worker created for task queue %s", TASK_QUEUE)
    return worker


async def _run_worker_async() -> None:
    global _worker_instance, _worker_running_loop
    client = await connect_temporal_client()
    if client is None:
        return
    loop = asyncio.get_running_loop()
    set_temporal_client(client)
    set_temporal_loop(loop)
    worker = create_blogging_worker(client)
    if worker is None:
        return
    _worker_running_loop = loop
    _worker_instance = worker
    logger.info("Blogging Temporal worker starting")
    try:
        await worker.run()
    finally:
        _worker_instance = None
        _worker_running_loop = None


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
        logger.exception("Blogging Temporal worker failed: %s", e)
    finally:
        set_temporal_client(None)
        set_temporal_loop(None)
        loop.close()


def start_blogging_temporal_worker_thread() -> bool:
    global _worker_thread
    if not is_temporal_enabled():
        return False
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_thread = threading.Thread(
        target=_worker_thread_target,
        name="blogging-temporal-worker",
        daemon=True,
    )
    _worker_thread.start()
    logger.info("Blogging Temporal worker thread started")
    return True


def shutdown_blogging_temporal_components(*, worker_shutdown_timeout: float = 30.0) -> None:
    """Stop the Temporal worker and activity executor (called from FastAPI lifespan shutdown).

    Avoids leaving non-daemon ThreadPoolExecutor threads alive after Uvicorn exits.
    """
    global _activity_executor, _worker_instance, _worker_running_loop

    worker = _worker_instance
    loop = _worker_running_loop
    if worker is not None and loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(worker.shutdown(), loop)
        try:
            fut.result(timeout=worker_shutdown_timeout)
        except Exception as exc:
            logger.warning(
                "Temporal worker shutdown did not complete within %.1fs: %s",
                worker_shutdown_timeout,
                exc,
            )
    elif worker is not None and loop is not None:
        logger.debug("Temporal worker loop not running; skipping graceful shutdown")

    if _activity_executor is not None:
        try:
            _activity_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.exception("ThreadPoolExecutor shutdown failed")
        _activity_executor = None
