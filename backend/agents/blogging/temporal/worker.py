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
    except RuntimeError as e:
        # Common when shutdown_blogging_temporal_components calls loop.stop() while worker.run() is stuck.
        msg = str(e).lower()
        if "event loop stopped" in msg or "loop stopped" in msg:
            logger.info("Temporal worker event loop stopped during shutdown")
        else:
            logger.exception("Blogging Temporal worker failed: %s", e)
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


def shutdown_blogging_temporal_components(*, worker_shutdown_timeout: float = 8.0) -> None:
    """Stop the Temporal worker and activity executor (called from FastAPI lifespan shutdown).

    When Temporal server is already gone, ``Worker.shutdown()`` may never complete because the
    SDK retries gRPC polls indefinitely. In that case we force-stop the worker's asyncio loop
    so the process can exit (Docker stop / compose down).
    """
    global _activity_executor, _worker_instance, _worker_running_loop, _worker_thread

    worker = _worker_instance
    loop = _worker_running_loop
    if loop is not None and loop.is_running():
        if worker is not None:
            fut = asyncio.run_coroutine_threadsafe(worker.shutdown(), loop)
            try:
                fut.result(timeout=worker_shutdown_timeout)
            except Exception as exc:
                logger.warning(
                    "Temporal worker.shutdown() did not finish in %.1fs (%s); forcing loop stop",
                    worker_shutdown_timeout,
                    exc,
                )
                _force_stop_worker_loop(loop)
        else:
            _force_stop_worker_loop(loop)
    elif worker is not None and loop is not None:
        logger.debug("Temporal worker loop not running; skipping graceful shutdown")

    if _worker_thread is not None and _worker_thread.is_alive():
        _worker_thread.join(timeout=5.0)
        if _worker_thread.is_alive():
            logger.warning("Temporal worker thread did not exit within 5s after loop stop")

    if _activity_executor is not None:
        try:
            _activity_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.exception("ThreadPoolExecutor shutdown failed")
        _activity_executor = None


def _force_stop_worker_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Stop the worker thread's event loop from another thread (Temporal unreachable / stuck)."""

    def _stop() -> None:
        if loop.is_running():
            loop.stop()

    try:
        loop.call_soon_threadsafe(_stop)
    except Exception:
        logger.exception("Could not schedule Temporal worker loop stop")
